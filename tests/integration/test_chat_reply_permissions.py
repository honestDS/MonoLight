import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_SESSION_REPLY_WORK_NOT_FOUND
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from tests.integration.test_chat_session_workflow import (
    _build_app,
    _seed_profiles,
    chat_session_database,
)

__all__ = ["chat_session_database"]


def _work(
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    sequence_no: int,
    dedupe_key: str,
    status: SessionReplyWorkStatus,
    request_ids: list[str] | None = None,
    response: dict[str, Any] | None = None,
    merged_into_id: int | None = None,
    result_message_id: int | None = None,
    error: str | None = None,
) -> SessionReplyWorkItem:
    execution_state: dict[str, Any] = {}
    if request_ids is not None:
        execution_state["request_ids"] = request_ids
    if response is not None:
        execution_state["response"] = response
    return SessionReplyWorkItem(
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        sequence_no=sequence_no,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=f"source-{dedupe_key}",
        dedupe_key=dedupe_key,
        status=status,
        merged_into_id=merged_into_id,
        result_message_id=result_message_id,
        execution_state=execution_state,
        error=error,
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


async def _database_snapshot(db: AsyncSession) -> dict[str, tuple]:
    sessions = list((await db.execute(select(ChatSession).order_by(ChatSession.session_id))).scalars().all())
    works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
    messages = list((await db.execute(select(Message).order_by(Message.id))).scalars().all())
    plans = list((await db.execute(select(SessionTodoPlan).order_by(SessionTodoPlan.session_id))).scalars().all())
    tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
    return {
        "sessions": tuple(
            (
                row.session_id,
                row.uid,
                row.profile_id,
                row.profile_override_id,
                row.source,
                row.reply_target_source,
                row.latest_message_id,
            )
            for row in sessions
        ),
        "works": tuple(
            (
                row.id,
                row.uid,
                row.session_id,
                row.profile_id,
                row.sequence_no,
                row.status.value,
                row.merged_into_id,
                row.result_message_id,
                row.error,
                _json(row.execution_state),
            )
            for row in works
        ),
        "messages": tuple(
            (
                row.id,
                row.uid,
                row.session_id,
                row.profile_id,
                row.role.value,
                row.type.value,
                row.content,
            )
            for row in messages
        ),
        "plans": tuple((row.session_id, row.uid, row.revision, _json(row.todos)) for row in plans),
        "background_tasks": tuple(
            (
                row.id,
                row.uid,
                row.session_id,
                row.profile_id,
                row.status.value,
                row.reply_status.value,
                row.auto_reply,
                _json(row.arguments),
            )
            for row in tasks
        ),
    }


async def _assert_not_found(client: httpx.AsyncClient, work_id: int) -> None:
    response = await client.get(f"/api/v1/chat/reply-works/{work_id}")
    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_SESSION_REPLY_WORK_NOT_FOUND)
    assert payload["data"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        pytest.param(SessionReplyWorkStatus.RUNNING, None, id="running"),
        pytest.param(SessionReplyWorkStatus.CANCELLED, "cancelled by test", id="cancelled"),
    ],
)
async def test_reply_work_status_owner_admin_and_non_owner_boundaries(
    chat_session_database: AsyncSession,
    status: SessionReplyWorkStatus,
    error: str | None,
) -> None:
    owner_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert owner_profile.id is not None
    session = ChatSession(
        session_id=f"reply-status-{status.value}",
        uid="user-1",
        profile_id=owner_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add(session)
    await chat_session_database.flush()
    work = _work(
        uid=session.uid,
        session_id=session.session_id,
        profile_id=owner_profile.id,
        sequence_no=1,
        dedupe_key=f"reply-status-{status.value}",
        status=status,
        request_ids=[f"{status.value}-request"],
        error=error,
    )
    chat_session_database.add(work)
    await chat_session_database.commit()
    await chat_session_database.refresh(work)
    assert work.id is not None
    before = await _database_snapshot(chat_session_database)
    expected = {
        "work_id": work.id,
        "resolved_work_id": work.id,
        "work_sequence_no": work.sequence_no,
        "session_id": session.session_id,
        "status": status.value,
        "request_ids": [f"{status.value}-request"],
        "result_message_id": None,
        "response": None,
        "error": error,
    }

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner = await client.get(f"/api/v1/chat/reply-works/{work.id}")
        assert owner.status_code == 200
        assert owner.json()["data"] == expected

        auth_state.update(uid="admin", is_superuser=True)
        admin = await client.get(f"/api/v1/chat/reply-works/{work.id}")
        assert admin.status_code == 200
        assert admin.json()["data"] == expected

        auth_state.update(uid="user-2", is_superuser=False)
        await _assert_not_found(client, work.id)

        auth_state.update(uid="user-1", is_superuser=False)
        owner_again = await client.get(f"/api/v1/chat/reply-works/{work.id}")
        assert owner_again.status_code == 200
        assert owner_again.json()["data"] == expected
        await _assert_not_found(client, 999_999)

        auth_state.update(uid="admin", is_superuser=True)
        await _assert_not_found(client, 999_999)

    assert await _database_snapshot(chat_session_database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("with_owner_activity", [True, False], ids=["owner-activity", "empty-owner-state"])
async def test_succeeded_reply_projects_owner_todo_and_background_state(
    chat_session_database: AsyncSession,
    with_owner_activity: bool,
) -> None:
    owner_profile, _alternate_profile, admin_profile = await _seed_profiles(chat_session_database)
    assert owner_profile.id is not None
    assert admin_profile.id is not None
    owner_session = ChatSession(
        session_id=f"reply-success-owner-{with_owner_activity}",
        uid="user-1",
        profile_id=owner_profile.id,
        source="http",
        reply_target_source="http",
    )
    admin_session = ChatSession(
        session_id=f"reply-success-admin-{with_owner_activity}",
        uid="admin",
        profile_id=admin_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add_all([owner_session, admin_session])
    await chat_session_database.flush()

    result_message = Message(
        session_id=owner_session.session_id,
        uid=owner_session.uid,
        profile_id=owner_profile.id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="owner reply",
        is_processed=True,
    )
    await message_crud.persist(chat_session_database, message=result_message, commit=False)
    assert result_message.id is not None
    work = _work(
        uid=owner_session.uid,
        session_id=owner_session.session_id,
        profile_id=owner_profile.id,
        sequence_no=1,
        dedupe_key=f"reply-success-{with_owner_activity}",
        status=SessionReplyWorkStatus.SUCCEEDED,
        request_ids=["owner-request"],
        response={"content": "owner reply", "history": []},
        result_message_id=result_message.id,
    )
    chat_session_database.add(work)

    owner_todos = [
        {"content": "finish owner task", "status": "in_progress"},
        {"content": "review owner reply", "status": "pending"},
    ]
    admin_todos = [{"content": "admin-only task", "status": "in_progress"}]
    chat_session_database.add(
        SessionTodoPlan(
            session_id=admin_session.session_id,
            uid=admin_session.uid,
            revision=99,
            todos=admin_todos,
        )
    )
    if with_owner_activity:
        chat_session_database.add(
            SessionTodoPlan(
                session_id=owner_session.session_id,
                uid=owner_session.uid,
                revision=4,
                todos=owner_todos,
            )
        )
        chat_session_database.add(
            BackgroundTask(
                uid=owner_session.uid,
                session_id=owner_session.session_id,
                profile_id=owner_profile.id,
                tool_call_id="owner-pending",
                tool_name="owner_tool",
                status=BackgroundTaskStatus.PENDING,
                arguments={},
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.PENDING,
            )
        )
    # BackgroundTask has no owner foreign key; retain an admin-owned row on the
    # owner's session to prove the projection never follows the requester uid.
    chat_session_database.add(
        BackgroundTask(
            uid=admin_session.uid,
            session_id=owner_session.session_id,
            profile_id=admin_profile.id,
            tool_call_id="admin-pending",
            tool_name="admin_tool",
            status=BackgroundTaskStatus.PENDING,
            arguments={},
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.PENDING,
        )
    )
    await chat_session_database.commit()
    await chat_session_database.refresh(work)
    assert work.id is not None
    before = await _database_snapshot(chat_session_database)

    expected_todo = {
        "revision": 4 if with_owner_activity else 0,
        "todos": owner_todos if with_owner_activity else [],
    }
    expected_response = {
        "content": "owner reply",
        "history": [],
        "work_id": work.id,
        "work_sequence_no": work.sequence_no,
        "message_id": result_message.id,
        "session_todo": expected_todo,
    }
    expected_data = {
        "work_id": work.id,
        "resolved_work_id": work.id,
        "work_sequence_no": work.sequence_no,
        "session_id": owner_session.session_id,
        "status": SessionReplyWorkStatus.SUCCEEDED.value,
        "request_ids": ["owner-request"],
        "result_message_id": result_message.id,
        "response": expected_response,
        "error": None,
    }
    if with_owner_activity:
        expected_response["has_background_tasks"] = True
        expected_response["background_task_poll_interval"] = 2

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner = await client.get(f"/api/v1/chat/reply-works/{work.id}")
        assert owner.status_code == 200
        assert owner.json()["data"] == expected_data

        auth_state.update(uid="admin", is_superuser=True)
        admin = await client.get(f"/api/v1/chat/reply-works/{work.id}")
        assert admin.status_code == 200
        assert admin.json()["data"] == expected_data

        auth_state.update(uid="user-2", is_superuser=False)
        await _assert_not_found(client, work.id)

        auth_state.update(uid="user-1", is_superuser=False)
        owner_again = await client.get(f"/api/v1/chat/reply-works/{work.id}")
        assert owner_again.status_code == 200
        assert owner_again.json()["data"] == expected_data

    assert await _database_snapshot(chat_session_database) == before


@pytest.mark.asyncio
async def test_failed_reply_and_same_session_merged_reply_preserve_owner_projection(
    chat_session_database: AsyncSession,
) -> None:
    owner_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert owner_profile.id is not None
    session = ChatSession(
        session_id="reply-failed-and-merged-session",
        uid="user-1",
        profile_id=owner_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add(session)
    await chat_session_database.flush()

    error_message = Message(
        session_id=session.session_id,
        uid=session.uid,
        profile_id=owner_profile.id,
        role=MessageRole.ERR,
        type=MessageType.TEXT,
        content="existing worker error",
        is_processed=True,
    )
    await message_crud.persist(chat_session_database, message=error_message, commit=False)
    assert error_message.id is not None
    failed = _work(
        uid=session.uid,
        session_id=session.session_id,
        profile_id=owner_profile.id,
        sequence_no=1,
        dedupe_key="reply-failed-work",
        status=SessionReplyWorkStatus.FAILED,
        request_ids=["failed-request"],
        result_message_id=error_message.id,
    )
    chat_session_database.add(failed)

    merged_message = Message(
        session_id=session.session_id,
        uid=session.uid,
        profile_id=owner_profile.id,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="merged reply",
        is_processed=True,
    )
    await message_crud.persist(chat_session_database, message=merged_message, commit=False)
    assert merged_message.id is not None
    target = _work(
        uid=session.uid,
        session_id=session.session_id,
        profile_id=owner_profile.id,
        sequence_no=3,
        dedupe_key="reply-merged-target",
        status=SessionReplyWorkStatus.SUCCEEDED,
        request_ids=["merged-target-request"],
        response={"content": "merged reply", "history": []},
        result_message_id=merged_message.id,
    )
    chat_session_database.add(target)
    await chat_session_database.flush()
    assert target.id is not None
    merged = _work(
        uid=session.uid,
        session_id=session.session_id,
        profile_id=owner_profile.id,
        sequence_no=2,
        dedupe_key="reply-merged-original",
        status=SessionReplyWorkStatus.MERGED,
        request_ids=["merged-original-request"],
        merged_into_id=target.id,
    )
    chat_session_database.add(merged)
    await chat_session_database.commit()
    await chat_session_database.refresh(failed)
    await chat_session_database.refresh(merged)
    assert failed.id is not None
    assert merged.id is not None
    before = await _database_snapshot(chat_session_database)

    expected_failed = {
        "work_id": failed.id,
        "resolved_work_id": failed.id,
        "work_sequence_no": failed.sequence_no,
        "session_id": session.session_id,
        "status": SessionReplyWorkStatus.FAILED.value,
        "request_ids": ["failed-request"],
        "result_message_id": error_message.id,
        "response": None,
        "error": "existing worker error",
    }
    expected_merged = {
        "work_id": merged.id,
        "resolved_work_id": target.id,
        "work_sequence_no": target.sequence_no,
        "session_id": session.session_id,
        "status": SessionReplyWorkStatus.SUCCEEDED.value,
        "request_ids": ["merged-target-request"],
        "result_message_id": merged_message.id,
        "response": {
            "content": "merged reply",
            "history": [],
            "work_id": target.id,
            "work_sequence_no": target.sequence_no,
            "message_id": merged_message.id,
            "session_todo": {"revision": 0, "todos": []},
        },
        "error": None,
    }

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for work_id, expected in ((failed.id, expected_failed), (merged.id, expected_merged)):
            owner = await client.get(f"/api/v1/chat/reply-works/{work_id}")
            assert owner.status_code == 200
            assert owner.json()["data"] == expected

        auth_state.update(uid="admin", is_superuser=True)
        for work_id, expected in ((failed.id, expected_failed), (merged.id, expected_merged)):
            admin = await client.get(f"/api/v1/chat/reply-works/{work_id}")
            assert admin.status_code == 200
            assert admin.json()["data"] == expected

        auth_state.update(uid="user-2", is_superuser=False)
        await _assert_not_found(client, failed.id)
        await _assert_not_found(client, merged.id)
        await _assert_not_found(client, 999_999)

    assert await _database_snapshot(chat_session_database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "is_superuser"),
    [
        pytest.param("user-1", False, id="ordinary-owner"),
        pytest.param("admin", True, id="superuser"),
    ],
)
async def test_invalid_merged_links_are_hidden_across_uid_and_session(
    chat_session_database: AsyncSession,
    uid: str,
    is_superuser: bool,
) -> None:
    owner_profile, _alternate_profile, other_profile = await _seed_profiles(chat_session_database)
    assert owner_profile.id is not None
    assert other_profile.id is not None
    owner_session = ChatSession(
        session_id="reply-merged-invalid-owner",
        uid="user-1",
        profile_id=owner_profile.id,
        source="http",
        reply_target_source="http",
    )
    cross_uid_session = ChatSession(
        session_id="reply-merged-invalid-cross-uid",
        uid="user-2",
        profile_id=other_profile.id,
        source="http",
        reply_target_source="http",
    )
    other_session = ChatSession(
        session_id="reply-merged-invalid-other-session",
        uid="user-1",
        profile_id=owner_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add_all([owner_session, cross_uid_session, other_session])
    await chat_session_database.flush()

    cross_uid_target = _work(
        uid=cross_uid_session.uid,
        session_id=cross_uid_session.session_id,
        profile_id=other_profile.id,
        sequence_no=1,
        dedupe_key="reply-invalid-cross-uid-target",
        status=SessionReplyWorkStatus.SUCCEEDED,
        request_ids=["cross-uid-target"],
        response={"content": "cross uid secret", "history": []},
    )
    other_session_target = _work(
        uid=other_session.uid,
        session_id=other_session.session_id,
        profile_id=owner_profile.id,
        sequence_no=1,
        dedupe_key="reply-invalid-other-session-target",
        status=SessionReplyWorkStatus.SUCCEEDED,
        request_ids=["other-session-target"],
        response={"content": "other session secret", "history": []},
    )
    chat_session_database.add_all([cross_uid_target, other_session_target])
    await chat_session_database.flush()
    assert cross_uid_target.id is not None
    assert other_session_target.id is not None

    cross_uid_original = _work(
        uid=owner_session.uid,
        session_id=owner_session.session_id,
        profile_id=owner_profile.id,
        sequence_no=1,
        dedupe_key="reply-invalid-cross-uid-original",
        status=SessionReplyWorkStatus.MERGED,
        request_ids=["cross-uid-original"],
        merged_into_id=cross_uid_target.id,
    )
    other_session_original = _work(
        uid=owner_session.uid,
        session_id=owner_session.session_id,
        profile_id=owner_profile.id,
        sequence_no=2,
        dedupe_key="reply-invalid-other-session-original",
        status=SessionReplyWorkStatus.MERGED,
        request_ids=["other-session-original"],
        merged_into_id=other_session_target.id,
    )
    chat_session_database.add_all([cross_uid_original, other_session_original])
    await chat_session_database.commit()
    await chat_session_database.refresh(cross_uid_original)
    await chat_session_database.refresh(other_session_original)
    assert cross_uid_original.id is not None
    assert other_session_original.id is not None
    before = await _database_snapshot(chat_session_database)

    auth_state: dict[str, object] = {"uid": uid, "is_superuser": is_superuser}
    app = _build_app(chat_session_database, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await _assert_not_found(client, cross_uid_original.id)
        await _assert_not_found(client, other_session_original.id)
        await _assert_not_found(client, 999_999)

    assert await _database_snapshot(chat_session_database) == before
