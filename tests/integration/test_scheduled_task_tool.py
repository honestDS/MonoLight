import json
from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.api.v1.profile import get_profile_tool_options
from app.core.constants import (
    ERR_SCHEDULED_TASK_ARGUMENTS_INVALID,
    ERR_SCHEDULED_TASK_NOT_FOUND,
    ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND,
    ERR_SESSION_NOT_FOUND,
    ERR_TOOL_NOT_ENABLED,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    MSG_SCHEDULED_TASK_CREATED,
    MSG_SCHEDULED_TASK_DELETED,
    MSG_SCHEDULED_TASK_LIST_SUCCESS,
)
from app.core.dispatch_context import DispatchContext, DispatchMode
from app.core.i18n import get_current_locale, set_current_locale, t
from app.core.i18n.context import reset_current_locale
from app.core.tools import get_tools_for_profile
from app.core.tools.scheduled_tasks import ManageScheduledTasksExecutor
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round, process_single_tool
from app.models.message import InternalToolCall, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.user import User


def _profile_configs() -> dict:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["memory"]["precheck_enabled"] = False
    return configs


@pytest_asyncio.fixture
async def scheduled_tool_session_factory(
    database_factory,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        foreign_keys=True,
        name="scheduled-task-tool.sqlite3",
    ) as session_factory:
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid="owner", username="owner"),
                    User(uid="peer", username="peer"),
                    Profile(
                        id=1,
                        uid="owner",
                        name="Owner default",
                        is_default=True,
                        configs=_profile_configs(),
                    ),
                    Profile(
                        id=2,
                        uid="owner",
                        name="Owner override",
                        is_default=False,
                        configs=_profile_configs(),
                    ),
                    Profile(
                        id=3,
                        uid="peer",
                        name="Peer default",
                        is_default=True,
                        configs=_profile_configs(),
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    ChatSession(session_id="owner-main", uid="owner", profile_id=1),
                    ChatSession(session_id="owner-other", uid="owner", profile_id=1),
                    ChatSession(session_id="peer-main", uid="peer", profile_id=3),
                ]
            )
            await db.commit()

        yield session_factory


@pytest_asyncio.fixture
async def db_session(
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with scheduled_tool_session_factory() as db:
        yield db


async def _executor(
    db: AsyncSession | None,
    uid: str = "owner",
    session_id: str = "owner-main",
    profile_id: int | None = 1,
    mode: DispatchMode = "interactive",
) -> ManageScheduledTasksExecutor:
    profile = await db.get(Profile, profile_id) if db is not None and profile_id is not None else None
    executor = ManageScheduledTasksExecutor(project_root=".", uid=uid)
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode=mode,
            source="integration-test",
            uid=uid,
            session_id=session_id,
            profile=profile,
            db=db,
        )
    )
    return executor


async def _snapshot(session_factory: async_sessionmaker[AsyncSession]) -> list[tuple]:
    async with session_factory() as db:
        result = await db.execute(select(ScheduledTask).order_by(ScheduledTask.id))
        tasks = result.scalars().all()
        return [
            (
                task.id,
                task.name,
                task.uid,
                task.session_id,
                task.profile_id,
                task.message,
                task.interval_seconds,
                task.status,
                task.next_run_at,
                task.last_run_at,
                task.last_message_id,
                task.run_count,
                task.created_at,
                task.updated_at,
            )
            for task in tasks
        ]


@pytest.mark.parametrize("mode", ["interactive", "scheduled"])
async def test_create_persists_scheduled_task_for_runtime_context(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    mode: DispatchMode,
) -> None:
    executor = await _executor(db_session, mode=mode)

    response = json.loads(
        await executor.execute(
            "create",
            name="  Daily reminder  ",
            message="  Review the deployment  ",
            interval_seconds=60,
        )
    )

    assert response["status"] == "success"
    assert response["operation"] == "create"
    assert response["message"] == t(MSG_SCHEDULED_TASK_CREATED)
    assert {
        "id",
        "uid",
        "session_id",
        "profile_id",
        "interval_seconds",
        "status",
        "run_count",
        "next_run_at",
    } <= response["task"].keys()

    async with scheduled_tool_session_factory() as reader:
        task = await reader.get(ScheduledTask, response["task"]["id"])

    assert task is not None
    assert task.name == "Daily reminder"
    assert task.message == "Review the deployment"
    assert task.uid == "owner"
    assert task.session_id == "owner-main"
    assert task.profile_id == 1
    assert task.interval_seconds == 60
    assert task.status == "enabled"
    assert task.run_count == 0
    assert task.last_run_at is None
    assert 55 <= (task.next_run_at - task.created_at).total_seconds() <= 65


async def test_create_uses_dispatch_context_owner_and_runtime_profile(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    executor = await _executor(db_session, profile_id=2)
    executor.uid = "peer"

    response = json.loads(
        await executor.execute(
            "create",
            name="Context profile",
            message="Use the selected profile",
            interval_seconds=60,
        )
    )

    assert response["status"] == "success"
    async with scheduled_tool_session_factory() as reader:
        task = await reader.get(ScheduledTask, response["task"]["id"])
        session = await reader.get(ChatSession, "owner-main")

    assert task is not None
    assert session is not None
    assert task.uid == "owner"
    assert task.session_id == "owner-main"
    assert task.profile_id == 2
    assert session.profile_id == 1


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"message": "message", "interval_seconds": 60}, id="missing-name"),
        pytest.param({"name": "name", "interval_seconds": 60}, id="missing-message"),
        pytest.param({"name": "name", "message": "message"}, id="missing-interval"),
        pytest.param({"name": "  ", "message": "message", "interval_seconds": 60}, id="blank-name"),
        pytest.param({"name": "name", "message": "  ", "interval_seconds": 60}, id="blank-message"),
        pytest.param({"name": "n" * 101, "message": "message", "interval_seconds": 60}, id="name-too-long"),
        pytest.param({"name": "name", "message": "message", "interval_seconds": 59}, id="interval-too-small"),
        pytest.param({"name": "name", "message": "message", "interval_seconds": True}, id="interval-bool"),
        pytest.param({"name": "name", "message": "message", "interval_seconds": 60.0}, id="interval-float"),
        pytest.param({"name": "name", "message": "message", "interval_seconds": "60"}, id="interval-string"),
        pytest.param(
            {"name": "name", "message": "message", "interval_seconds": 2147483648},
            id="interval-too-large",
        ),
        pytest.param(
            {"name": "name", "message": "message", "interval_seconds": 60, "uid": "peer"},
            id="extra-uid",
        ),
        pytest.param(
            {"name": "name", "message": "message", "interval_seconds": 60, "session_id": "peer-main"},
            id="extra-session-id",
        ),
        pytest.param(
            {"name": "name", "message": "message", "interval_seconds": 60, "profile_id": 3},
            id="extra-profile-id",
        ),
        pytest.param(
            {"name": "name", "message": "message", "interval_seconds": 60, "is_admin": True},
            id="extra-is-admin",
        ),
    ],
)
async def test_create_rejects_invalid_arguments_without_changing_tasks(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    arguments: dict,
) -> None:
    baseline = json.loads(
        await (await _executor(db_session)).execute(
            "create",
            name="Existing task",
            message="Existing message",
            interval_seconds=60,
        )
    )
    assert baseline["status"] == "success"
    before = await _snapshot(scheduled_tool_session_factory)

    response = json.loads(await (await _executor(db_session)).execute("create", **arguments))

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SCHEDULED_TASK_ARGUMENTS_INVALID)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    ("context_case", "expected_error"),
    [
        pytest.param("no-db", ERR_TOOL_RUNTIME_CONTEXT_MISSING, id="no-db"),
        pytest.param("empty-uid", ERR_TOOL_RUNTIME_CONTEXT_MISSING, id="empty-uid"),
        pytest.param("empty-session", ERR_TOOL_RUNTIME_CONTEXT_MISSING, id="empty-session"),
    ],
)
async def test_create_requires_runtime_database_and_identity(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    context_case: str,
    expected_error: str,
) -> None:
    if context_case == "no-db":
        executor = await _executor(None, profile_id=None)
    elif context_case == "empty-uid":
        executor = await _executor(db_session, uid=" ")
    else:
        executor = await _executor(db_session, session_id="")
    before = await _snapshot(scheduled_tool_session_factory)

    response = json.loads(
        await executor.execute(
            "create",
            name="Invalid context",
            message="Must not persist",
            interval_seconds=60,
        )
    )

    assert response["status"] == "failed"
    assert response["error"] == t(expected_error)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    "session_id",
    [pytest.param("missing-session", id="missing-session"), pytest.param("peer-main", id="wrong-owner")],
)
async def test_create_requires_owned_session(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> None:
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, session_id=session_id)

    response = json.loads(
        await executor.execute(
            "create",
            name="Invalid session",
            message="Must not persist",
            interval_seconds=60,
        )
    )

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SESSION_NOT_FOUND)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    "profile_id",
    [pytest.param(3, id="profile-owned-by-peer"), pytest.param(999, id="unknown-profile")],
)
async def test_create_requires_profile_owned_by_runtime_user(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    profile_id: int,
) -> None:
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, profile_id=profile_id)

    response = json.loads(
        await executor.execute(
            "create",
            name="Invalid profile",
            message="Must not persist",
            interval_seconds=60,
        )
    )

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)
    assert await _snapshot(scheduled_tool_session_factory) == before


async def _seed_list_tasks(db: AsyncSession) -> datetime:
    created_at = datetime(2030, 1, 2, 3, 4, 5)
    db.add_all(
        [
            ScheduledTask(
                id=1,
                name="owner-main-oldest",
                uid="owner",
                session_id="owner-main",
                profile_id=1,
                message="Oldest owner-main task",
                interval_seconds=60,
                status=ScheduledTaskStatus.ENABLED,
                next_run_at=created_at + timedelta(seconds=60),
                created_at=created_at,
                updated_at=created_at,
            ),
            ScheduledTask(
                id=2,
                name="owner-other-only",
                uid="owner",
                session_id="owner-other",
                profile_id=1,
                message="Different owner session",
                interval_seconds=60,
                status=ScheduledTaskStatus.ENABLED,
                next_run_at=created_at + timedelta(seconds=60),
                created_at=created_at,
                updated_at=created_at,
            ),
            ScheduledTask(
                id=3,
                name="peer-main-only",
                uid="peer",
                session_id="peer-main",
                profile_id=3,
                message="Different owner",
                interval_seconds=60,
                status=ScheduledTaskStatus.ENABLED,
                next_run_at=created_at + timedelta(seconds=60),
                created_at=created_at,
                updated_at=created_at,
            ),
            ScheduledTask(
                id=4,
                name="owner-main-disabled",
                uid="owner",
                session_id="owner-main",
                profile_id=1,
                message="Disabled owner-main task",
                interval_seconds=120,
                status=ScheduledTaskStatus.DISABLED,
                next_run_at=created_at + timedelta(seconds=120),
                created_at=created_at,
                updated_at=created_at,
            ),
            ScheduledTask(
                id=5,
                name="owner-main-newest",
                uid="owner",
                session_id="owner-main",
                profile_id=1,
                message="Newest task message",
                interval_seconds=180,
                status=ScheduledTaskStatus.ENABLED,
                next_run_at=created_at + timedelta(seconds=180),
                last_run_at=created_at + timedelta(seconds=60),
                last_message_id=42,
                run_count=2,
                created_at=created_at,
                updated_at=created_at + timedelta(seconds=60),
            ),
        ]
    )
    await db.commit()
    return created_at


async def test_list_paginates_current_session_tasks_without_mutation(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created_at = await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, profile_id=None)

    default_response = json.loads(await executor.execute("list"))

    assert default_response["status"] == "success"
    assert default_response["operation"] == "list"
    assert default_response["message"] == t(MSG_SCHEDULED_TASK_LIST_SUCCESS)
    assert default_response["page"] == 1
    assert default_response["size"] == 20
    assert default_response["total"] == 3
    assert [task["id"] for task in default_response["tasks"]] == [5, 4, 1]
    assert [task["status"] for task in default_response["tasks"]] == ["enabled", "disabled", "enabled"]
    assert default_response["tasks"][0] == {
        "id": 5,
        "name": "owner-main-newest",
        "uid": "owner",
        "session_id": "owner-main",
        "profile_id": 1,
        "message": "Newest task message",
        "interval_seconds": 180,
        "status": "enabled",
        "next_run_at": (created_at + timedelta(seconds=180)).isoformat(),
        "last_run_at": (created_at + timedelta(seconds=60)).isoformat(),
        "last_message_id": 42,
        "run_count": 2,
        "created_at": created_at.isoformat(),
        "updated_at": (created_at + timedelta(seconds=60)).isoformat(),
        "username": None,
    }
    assert all(set(task) == set(default_response["tasks"][0]) for task in default_response["tasks"])

    paged_tasks = []
    for page in (1, 2, 3):
        response = json.loads(await executor.execute("list", page=page, size=1))
        assert response["page"] == page
        assert response["size"] == 1
        assert response["total"] == 3
        assert len(response["tasks"]) == 1
        paged_tasks.extend(response["tasks"])

    assert [task["id"] for task in paged_tasks] == [5, 4, 1]
    assert len({task["id"] for task in paged_tasks}) == len(paged_tasks)

    last_page = json.loads(await executor.execute("list", page=4, size=1))
    assert last_page["page"] == 4
    assert last_page["size"] == 1
    assert last_page["total"] == 3
    assert last_page["tasks"] == []
    assert await _snapshot(scheduled_tool_session_factory) == before


async def test_list_allows_owner_session_without_runtime_profile(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, profile_id=None)

    response = json.loads(await executor.execute("list"))

    assert response["status"] == "success"
    assert response["total"] == 0
    assert response["tasks"] == []
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"page": 0}, id="page-zero"),
        pytest.param({"page": True}, id="page-bool"),
        pytest.param({"page": "1"}, id="page-string"),
        pytest.param({"size": 0}, id="size-zero"),
        pytest.param({"size": 101}, id="size-too-large"),
        pytest.param({"size": True}, id="size-bool"),
        pytest.param({"size": 20.0}, id="size-float"),
        pytest.param({"name": "create argument"}, id="extra-name"),
        pytest.param({"message": "create argument"}, id="extra-message"),
        pytest.param({"interval_seconds": 60}, id="extra-interval"),
        pytest.param({"task_id": 1}, id="extra-task-id"),
        pytest.param({"uid": "peer"}, id="extra-uid"),
        pytest.param({"session_id": "peer-main"}, id="extra-session-id"),
        pytest.param({"is_admin": True}, id="extra-is-admin"),
    ],
)
async def test_list_rejects_invalid_arguments_without_changing_tasks(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    arguments: dict,
) -> None:
    await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)

    response = json.loads(await (await _executor(db_session)).execute("list", **arguments))

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SCHEDULED_TASK_ARGUMENTS_INVALID)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    "session_id",
    [pytest.param("missing-session", id="missing-session"), pytest.param("peer-main", id="wrong-owner")],
)
async def test_list_requires_owned_session(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> None:
    await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, session_id=session_id)

    response = json.loads(await executor.execute("list"))

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SESSION_NOT_FOUND)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize("task_id", [pytest.param(1, id="enabled-task"), pytest.param(4, id="disabled-task")])
async def test_delete_removes_current_session_task_once(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
) -> None:
    await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, profile_id=None)

    response = json.loads(await executor.execute("delete", task_id=task_id))

    assert response == {
        "status": "success",
        "operation": "delete",
        "message": t(MSG_SCHEDULED_TASK_DELETED),
        "task_id": task_id,
    }
    expected_remaining = [task for task in before if task[0] != task_id]
    async with scheduled_tool_session_factory() as reader:
        assert await reader.get(ScheduledTask, task_id) is None
    assert await _snapshot(scheduled_tool_session_factory) == expected_remaining

    repeated_response = json.loads(await executor.execute("delete", task_id=task_id))

    assert repeated_response["status"] == "failed"
    assert repeated_response["operation"] == "delete"
    assert repeated_response["error"] == t(ERR_SCHEDULED_TASK_NOT_FOUND)
    assert await _snapshot(scheduled_tool_session_factory) == expected_remaining


@pytest.mark.parametrize(
    "task_id",
    [
        pytest.param(2, id="same-owner-other-session"),
        pytest.param(3, id="different-owner"),
        pytest.param(999, id="missing-task"),
    ],
)
async def test_delete_does_not_disclose_or_remove_unowned_task(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
) -> None:
    await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, profile_id=None)

    response = json.loads(await executor.execute("delete", task_id=task_id))

    assert response["status"] == "failed"
    assert response["operation"] == "delete"
    assert response["error"] == t(ERR_SCHEDULED_TASK_NOT_FOUND)
    assert set(response) == {"status", "operation", "error"}
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="missing-task-id"),
        pytest.param({"task_id": 0}, id="task-id-zero"),
        pytest.param({"task_id": -1}, id="task-id-negative"),
        pytest.param({"task_id": True}, id="task-id-bool"),
        pytest.param({"task_id": 1.0}, id="task-id-float"),
        pytest.param({"task_id": "1"}, id="task-id-string"),
        pytest.param({"task_id": 1, "name": "create argument"}, id="extra-name"),
        pytest.param({"task_id": 1, "page": 1}, id="extra-page"),
        pytest.param({"task_id": 1, "uid": "peer"}, id="extra-uid"),
        pytest.param({"task_id": 1, "session_id": "peer-main"}, id="extra-session-id"),
        pytest.param({"task_id": 1, "is_admin": True}, id="extra-is-admin"),
    ],
)
async def test_delete_rejects_invalid_arguments_without_changing_tasks(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    arguments: dict,
) -> None:
    await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)

    response = json.loads(await (await _executor(db_session, profile_id=None)).execute("delete", **arguments))

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SCHEDULED_TASK_ARGUMENTS_INVALID)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    "session_id",
    [pytest.param("missing-session", id="missing-session"), pytest.param("peer-main", id="wrong-owner")],
)
async def test_delete_requires_owned_session(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> None:
    await _seed_list_tasks(db_session)
    before = await _snapshot(scheduled_tool_session_factory)
    executor = await _executor(db_session, session_id=session_id, profile_id=None)

    response = json.loads(await executor.execute("delete", task_id=1))

    assert response["status"] == "failed"
    assert response["error"] == t(ERR_SESSION_NOT_FOUND)
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize("allow_background", [True, False])
async def test_default_profile_exposes_and_executes_scheduled_task_tool(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    allow_background: bool,
) -> None:
    profile = await db_session.get(Profile, 1)
    assert profile is not None
    cfg = ProfileConfig.model_validate(profile.configs)
    tools, _ = await get_tools_for_profile(db_session, profile, allow_background=allow_background)
    tool_schemas = [tool for tool in tools if tool["function"]["name"] == "manage_scheduled_tasks"]

    assert len(tool_schemas) == 1
    parameters = tool_schemas[0]["function"]["parameters"]
    assert parameters["properties"]["operation"]["enum"] == ["create", "list", "delete"]
    assert {"name", "message", "interval_seconds", "page", "size", "task_id"} <= parameters["properties"].keys()
    assert not {"uid", "session_id", "profile_id", "is_admin", "run_in_background"} & parameters["properties"].keys()

    tool_call = InternalToolCall(
        id="scheduled-create-visible-profile",
        name="manage_scheduled_tasks",
        arguments={
            "operation": "create",
            "name": "Profile enabled task",
            "message": "Created through the standard tool executor",
            "interval_seconds": 60,
        },
    )
    result = await process_single_tool(
        tool_call,
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
    assert result.tool_call_id == tool_call.id
    assert result.tool_execution_status == "succeeded"
    assert json.loads(result.content)["status"] == "success"
    async with scheduled_tool_session_factory() as reader:
        tasks = (await reader.execute(select(ScheduledTask))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].name == "Profile enabled task"
    assert tasks[0].message == "Created through the standard tool executor"
    assert tasks[0].uid == "owner"
    assert tasks[0].session_id == "owner-main"


@pytest.mark.parametrize("enabled_tools", [pytest.param([], id="empty-list"), pytest.param(["file_tool"], id="other-tool-only")])
async def test_disabled_profile_hides_and_rejects_scheduled_task_tool(
    db_session: AsyncSession,
    scheduled_tool_session_factory: async_sessionmaker[AsyncSession],
    enabled_tools: list[str],
) -> None:
    profile = await db_session.get(Profile, 1)
    assert profile is not None
    profile_config = deepcopy(profile.configs)
    profile_config["tool"]["enabled_tools"] = enabled_tools
    profile.configs = profile_config
    cfg = ProfileConfig.model_validate(profile_config)
    tools, _ = await get_tools_for_profile(db_session, profile)
    assert all(tool["function"]["name"] != "manage_scheduled_tasks" for tool in tools)

    tool_call = InternalToolCall(
        id="scheduled-create-disabled-profile",
        name="manage_scheduled_tasks",
        arguments={
            "operation": "create",
            "name": "Must not be created",
            "message": "Disabled tool call",
            "interval_seconds": 60,
        },
    )
    expected_error = t(ERR_TOOL_NOT_ENABLED, tool_name="manage_scheduled_tasks")
    before = await _snapshot(scheduled_tool_session_factory)

    prevalidation_errors = prevalidate_tool_round([tool_call], cfg)
    assert json.loads(prevalidation_errors[tool_call.id])["error"] == expected_error

    result = await process_single_tool(
        tool_call,
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
    assert result.tool_call_id == tool_call.id
    assert result.tool_execution_status == "failed"
    assert json.loads(result.content)["error"] == expected_error
    assert await _snapshot(scheduled_tool_session_factory) == before


@pytest.mark.parametrize(
    ("locale", "expected_label"),
    [pytest.param("zh", "管理定时任务", id="chinese-label"), pytest.param("en", "Manage Scheduled Tasks", id="english-label")],
)
def test_profile_tool_options_localize_scheduled_task_label(locale: str, expected_label: str) -> None:
    original_locale = get_current_locale()
    token = set_current_locale(locale)
    try:
        options = get_profile_tool_options()
        option = next(item for item in options if item["value"] == "manage_scheduled_tasks")
        assert option["label"] == expected_label
    finally:
        reset_current_locale(token)

    assert get_current_locale() == original_locale
