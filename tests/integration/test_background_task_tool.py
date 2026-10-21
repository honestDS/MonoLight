import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import (
    ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_TOOL_NOT_ENABLED,
    MANAGE_BACKGROUND_TASKS_TOOL_NAME,
)
from app.core.i18n import t
from app.core.tools import get_registered_tool_names, get_tools_for_profile
from app.core.tools.background_tasks import ManageBackgroundTasksExecutor
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round, process_single_tool
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalToolCall, MessageRole
from app.models.profile import Profile, ProfileConfig

OLD_TOOL_NAMES = (
    "firecrawl_search",
    "firecrawl_scrape",
    "list_background_tasks",
    "cancel_background_task",
    "write_file",
)


def _task(
    *,
    task_id: int,
    uid: str,
    session_id: str,
    status: BackgroundTaskStatus,
    created_at: datetime,
) -> BackgroundTask:
    task = BackgroundTask(
        id=task_id,
        uid=uid,
        session_id=session_id,
        profile_id=1,
        tool_call_id=f"call-{task_id}",
        tool_name="send_file_to_user",
        status=status,
        arguments={"task": task_id},
        auto_reply=False,
        created_at=created_at,
    )
    if status == BackgroundTaskStatus.RUNNING:
        task.locked_by = "worker"
        task.lock_until = 4_000_000_000
    if status in {
        BackgroundTaskStatus.SUCCEEDED,
        BackgroundTaskStatus.FAILED,
        BackgroundTaskStatus.CANCELLED,
    }:
        task.finished_at = created_at + timedelta(seconds=1)
    return task


@pytest_asyncio.fixture
async def background_task_session_factory(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=[BackgroundTask.__table__],
        name="background-task-tool.sqlite3",
    ) as session_factory:
        created_at = datetime(2024, 1, 1)
        async with session_factory() as db:
            db.add_all(
                [
                    _task(task_id=1, uid="owner", session_id="owner-main", status=BackgroundTaskStatus.PENDING, created_at=created_at),
                    _task(task_id=2, uid="owner", session_id="owner-main", status=BackgroundTaskStatus.RUNNING, created_at=created_at + timedelta(seconds=1)),
                    _task(task_id=3, uid="owner", session_id="owner-main", status=BackgroundTaskStatus.SUCCEEDED, created_at=created_at + timedelta(seconds=2)),
                    _task(task_id=4, uid="owner", session_id="owner-main", status=BackgroundTaskStatus.FAILED, created_at=created_at + timedelta(seconds=3)),
                    _task(task_id=5, uid="owner", session_id="owner-main", status=BackgroundTaskStatus.CANCELLED, created_at=created_at + timedelta(seconds=4)),
                    _task(task_id=6, uid="owner", session_id="owner-other", status=BackgroundTaskStatus.PENDING, created_at=created_at + timedelta(seconds=5)),
                    _task(task_id=7, uid="peer", session_id="owner-main", status=BackgroundTaskStatus.PENDING, created_at=created_at + timedelta(seconds=6)),
                    _task(task_id=8, uid="peer", session_id="owner-other", status=BackgroundTaskStatus.RUNNING, created_at=created_at + timedelta(seconds=7)),
                ]
            )
            await db.commit()
        yield session_factory


@pytest_asyncio.fixture
async def db_session(
    background_task_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with background_task_session_factory() as db:
        yield db


def _executor(db: AsyncSession | None, *, uid: str = "owner", session_id: str = "owner-main") -> ManageBackgroundTasksExecutor:
    executor = ManageBackgroundTasksExecutor(project_root=".", uid=uid)
    executor.set_runtime_context(db=db, session_id=session_id)
    return executor


def _profile_with_tools(enabled_tools: list[str]) -> Profile:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["tool"]["enabled_tools"] = enabled_tools
    return Profile(
        id=None,
        uid="owner",
        name="Background task tool test",
        configs=configs,
    )


async def _snapshot(session_factory: async_sessionmaker[AsyncSession]) -> list[tuple]:
    async with session_factory() as db:
        result = await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))
        return [(task.id, task.uid, task.session_id, task.status, task.locked_by, task.lock_until, task.finished_at) for task in result.scalars().all()]


async def _stored_status(session_factory: async_sessionmaker[AsyncSession], task_id: int) -> BackgroundTaskStatus:
    async with session_factory() as db:
        task = await db.get(BackgroundTask, task_id)
        assert task is not None
        return task.status


async def test_list_returns_only_owned_active_tasks_with_session_filter_and_pagination(
    db_session: AsyncSession,
) -> None:
    executor = _executor(db_session)

    default_page = json.loads(await executor.execute("list"))
    assert default_page["status"] == "success"
    assert default_page["operation"] == "list"
    assert default_page["session_id"] == "owner-main"
    assert default_page["page"] == 1
    assert default_page["size"] == 20
    assert [task["id"] for task in default_page["tasks"]] == [2, 1]
    assert [task["status"] for task in default_page["tasks"]] == ["running", "pending"]

    first_page = json.loads(await executor.execute("list", page=1, size=1))
    second_page = json.loads(await executor.execute("list", page=2, size=1))
    empty_page = json.loads(await executor.execute("list", page=3, size=1))
    assert (first_page["page"], first_page["size"], [task["id"] for task in first_page["tasks"]]) == (1, 1, [2])
    assert (second_page["page"], second_page["size"], [task["id"] for task in second_page["tasks"]]) == (2, 1, [1])
    assert (empty_page["page"], empty_page["size"], empty_page["tasks"]) == (3, 1, [])

    other_session = json.loads(await executor.execute("list", session_id="owner-other"))
    assert other_session["session_id"] == "owner-other"
    assert [task["id"] for task in other_session["tasks"]] == [6]
    assert all(task["uid"] == "owner" for task in default_page["tasks"] + other_session["tasks"])


@pytest.mark.parametrize(
    ("task_id", "previous_status"),
    [
        pytest.param(1, BackgroundTaskStatus.PENDING, id="pending"),
        pytest.param(2, BackgroundTaskStatus.RUNNING, id="running"),
    ],
)
async def test_cancel_persists_and_repeated_cancel_is_idempotent(
    db_session: AsyncSession,
    background_task_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
    previous_status: BackgroundTaskStatus,
) -> None:
    assert await _stored_status(background_task_session_factory, task_id) == previous_status
    executor = _executor(db_session)

    response = json.loads(await executor.execute("cancel", task_id=task_id))
    assert response["status"] == "success"
    assert response["operation"] == "cancel"
    assert response["task"]["id"] == task_id
    assert response["task"]["status"] == "cancelled"
    assert await _stored_status(background_task_session_factory, task_id) == BackgroundTaskStatus.CANCELLED

    repeated_response = json.loads(await executor.execute("cancel", task_id=task_id))
    assert repeated_response["status"] == "success"
    assert repeated_response["task"]["status"] == "cancelled"
    assert await _stored_status(background_task_session_factory, task_id) == BackgroundTaskStatus.CANCELLED


@pytest.mark.parametrize(
    ("task_id", "status"),
    [
        pytest.param(3, BackgroundTaskStatus.SUCCEEDED, id="succeeded"),
        pytest.param(4, BackgroundTaskStatus.FAILED, id="failed"),
        pytest.param(5, BackgroundTaskStatus.CANCELLED, id="cancelled"),
    ],
)
async def test_cancel_keeps_existing_terminal_status(
    db_session: AsyncSession,
    background_task_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
    status: BackgroundTaskStatus,
) -> None:
    before = await _snapshot(background_task_session_factory)

    response = json.loads(await _executor(db_session).execute("cancel", task_id=task_id))

    assert response["status"] == "success"
    assert response["task"]["status"] == status.value
    assert await _stored_status(background_task_session_factory, task_id) == status
    assert await _snapshot(background_task_session_factory) == before


@pytest.mark.parametrize(
    "task_id",
    [pytest.param(7, id="other-user-task"), pytest.param(999, id="missing-task")],
)
async def test_cancel_rejects_unowned_or_missing_task_without_mutation(
    db_session: AsyncSession,
    background_task_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
) -> None:
    before = await _snapshot(background_task_session_factory)

    response = json.loads(await _executor(db_session).execute("cancel", task_id=task_id))

    assert response["status"] == "failed"
    assert response["operation"] == "cancel"
    assert response["error"] == t(ERR_BACKGROUND_TASK_NOT_FOUND)
    assert await _snapshot(background_task_session_factory) == before


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        pytest.param("cancel", {}, id="missing-task-id"),
        pytest.param("cancel", {"task_id": 0}, id="task-id-zero"),
        pytest.param("cancel", {"task_id": -1}, id="task-id-negative"),
        pytest.param("cancel", {"task_id": True}, id="task-id-bool"),
        pytest.param("cancel", {"task_id": 1.0}, id="task-id-float"),
        pytest.param("cancel", {"task_id": "1"}, id="task-id-string"),
        pytest.param("cancel", {"task_id": 1, "page": 1}, id="cancel-with-list-field"),
        pytest.param("cancel", {"task_id": 1, "session_id": "owner-main"}, id="cancel-with-session-field"),
        pytest.param("list", {"page": 0}, id="page-zero"),
        pytest.param("list", {"page": True}, id="page-bool"),
        pytest.param("list", {"size": 101}, id="size-too-large"),
        pytest.param("list", {"size": "1"}, id="size-string"),
        pytest.param("list", {"task_id": 1}, id="list-with-cancel-field"),
        pytest.param("list", {"session_id": 1}, id="session-id-type"),
        pytest.param("purge", {"task_id": 1}, id="unknown-operation"),
    ],
)
async def test_invalid_arguments_and_operations_fail_without_mutation(
    db_session: AsyncSession,
    background_task_session_factory: async_sessionmaker[AsyncSession],
    operation: str,
    arguments: dict,
) -> None:
    before = await _snapshot(background_task_session_factory)

    response = json.loads(await _executor(db_session).execute(operation, **arguments))

    assert response["status"] == "failed"
    assert response["operation"] == operation
    assert await _snapshot(background_task_session_factory) == before


async def test_manage_tool_fails_without_database_context() -> None:
    response = json.loads(await _executor(None).execute("list"))

    assert response["status"] == "failed"
    assert response["operation"] == "list"
    assert response["error"] == t(ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE)


async def test_enabled_manage_tool_is_exposed_prevalidated_and_dispatched(
    db_session: AsyncSession,
    background_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = _profile_with_tools([MANAGE_BACKGROUND_TASKS_TOOL_NAME])
    cfg = ProfileConfig.model_validate(profile.configs)
    tools, _ = await get_tools_for_profile(db_session, profile)
    exposed = [tool for tool in tools if tool["function"]["name"] == MANAGE_BACKGROUND_TASKS_TOOL_NAME]

    assert len(exposed) == 1
    parameters = exposed[0]["function"]["parameters"]
    assert parameters["required"] == ["operation"]
    assert parameters["properties"]["operation"]["enum"] == ["list", "cancel"]
    assert {"session_id", "page", "size", "task_id"} <= parameters["properties"].keys()

    list_call = InternalToolCall(
        id="manage-list",
        name=MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        arguments={"operation": "list"},
    )
    assert prevalidate_tool_round([list_call], cfg, tool_schemas=tools) == {}
    list_result = await process_single_tool(
        list_call,
        db_session,
        profile,
        cfg,
        messages=[],
        username="owner",
        session_id="owner-main",
        turn=1,
        uid="owner",
        tool_result_round_budget_tokens=4096,
    )
    list_payload = json.loads(list_result.content)
    assert list_result.role == MessageRole.TOOL
    assert list_result.tool_execution_status == "succeeded"
    assert list_payload["status"] == "success"
    assert [task["id"] for task in list_payload["tasks"]] == [2, 1]

    cancel_call = InternalToolCall(
        id="manage-cancel",
        name=MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        arguments={"operation": "cancel", "task_id": 1},
    )
    assert prevalidate_tool_round([cancel_call], cfg, tool_schemas=tools) == {}
    cancel_result = await process_single_tool(
        cancel_call,
        db_session,
        profile,
        cfg,
        messages=[],
        username="owner",
        session_id="owner-main",
        turn=2,
        uid="owner",
        tool_result_round_budget_tokens=4096,
    )
    assert cancel_result.role == MessageRole.TOOL
    assert cancel_result.tool_execution_status == "succeeded"
    assert json.loads(cancel_result.content)["task"]["status"] == "cancelled"
    assert await _stored_status(background_task_session_factory, 1) == BackgroundTaskStatus.CANCELLED

    missing_operation = InternalToolCall(
        id="manage-missing-operation",
        name=MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        arguments={},
    )
    missing_errors = prevalidate_tool_round([missing_operation], cfg, tool_schemas=tools)
    assert json.loads(missing_errors[missing_operation.id])["missing_arguments"] == ["operation"]
    before = await _snapshot(background_task_session_factory)
    missing_result = await process_single_tool(
        missing_operation,
        db_session,
        profile,
        cfg,
        messages=[],
        username="owner",
        session_id="owner-main",
        turn=3,
        uid="owner",
        tool_result_round_budget_tokens=4096,
    )
    assert missing_result.tool_execution_status == "failed"
    assert json.loads(missing_result.content)["missing_arguments"] == ["operation"]
    assert await _snapshot(background_task_session_factory) == before

    unknown_operation = InternalToolCall(
        id="manage-unknown-operation",
        name=MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        arguments={"operation": "purge"},
    )
    assert prevalidate_tool_round([unknown_operation], cfg, tool_schemas=tools)
    unknown_result = await process_single_tool(
        unknown_operation,
        db_session,
        profile,
        cfg,
        messages=[],
        username="owner",
        session_id="owner-main",
        turn=4,
        uid="owner",
        tool_result_round_budget_tokens=4096,
    )
    assert unknown_result.tool_execution_status == "failed"
    assert json.loads(unknown_result.content)["status"] == "failed"
    assert await _snapshot(background_task_session_factory) == before


async def test_disabled_manage_tool_is_hidden_and_dispatch_is_rejected(
    db_session: AsyncSession,
    background_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = _profile_with_tools([])
    cfg = ProfileConfig.model_validate(profile.configs)
    tools, _ = await get_tools_for_profile(db_session, profile)
    assert all(tool["function"]["name"] != MANAGE_BACKGROUND_TASKS_TOOL_NAME for tool in tools)

    call = InternalToolCall(
        id="manage-disabled",
        name=MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        arguments={"operation": "cancel", "task_id": 1},
    )
    expected_error = t(ERR_TOOL_NOT_ENABLED, tool_name=MANAGE_BACKGROUND_TASKS_TOOL_NAME)
    prevalidation_errors = prevalidate_tool_round([call], cfg, tool_schemas=tools)
    assert json.loads(prevalidation_errors[call.id])["error"] == expected_error
    before = await _snapshot(background_task_session_factory)

    result = await process_single_tool(
        call,
        db_session,
        profile,
        cfg,
        messages=[],
        username="owner",
        session_id="owner-main",
        turn=1,
        uid="owner",
        tool_result_round_budget_tokens=4096,
    )

    assert result.role == MessageRole.TOOL
    assert result.tool_execution_status == "failed"
    assert json.loads(result.content)["error"] == expected_error
    assert await _snapshot(background_task_session_factory) == before


def test_legacy_tool_names_are_not_registered() -> None:
    assert set(get_registered_tool_names()).isdisjoint(OLD_TOOL_NAMES)


@pytest.mark.parametrize("legacy_name", OLD_TOOL_NAMES)
async def test_legacy_tool_names_are_not_exposed_or_aliases_for_manage_tool(
    db_session: AsyncSession,
    legacy_name: str,
) -> None:
    # 直接使用原始配置，避免断言依赖配置迁移行为。
    profile = _profile_with_tools([legacy_name])
    profile.configs["tool"]["enabled_tools"] = [legacy_name]
    tools, _ = await get_tools_for_profile(db_session, profile)
    exposed_names = {tool["function"]["name"] for tool in tools}

    assert legacy_name not in exposed_names
    assert MANAGE_BACKGROUND_TASKS_TOOL_NAME not in exposed_names
