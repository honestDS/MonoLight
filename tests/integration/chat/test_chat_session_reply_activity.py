import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageResponse, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.chat.test_chat_session_workflow import (
    _build_app,
    _patch_http_submission,
    _seed_profiles,
)
from tests.integration.chat.test_chat_session_workflow import chat_session_database as _chat_session_database

chat_session_database = _chat_session_database


@pytest.mark.asyncio
async def test_http_completion_submission_is_deterministic_idempotent_and_non_streaming(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    _patch_http_submission(monkeypatch, primary_profile)
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    request_id = "http-request-1"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={"message": "hello", "request_id": request_id},
        )
        assert created.status_code == 200
        created_payload = created.json()
        expected_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"monolight:http:user-1:{request_id}"))
        session_id = created_payload["choices"][0]["message"]["content"]
        assert session_id == expected_session_id
        assert created_payload["choices"][0]["finish_reason"] == "new_session"

        persisted_session = await chat_session_database.get(ChatSession, session_id)
        assert persisted_session is not None
        assert persisted_session.uid == "user-1"
        assert persisted_session.source == "http"

        submission = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello from HTTP",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert submission.status_code == 200
        submission_data = submission.json()["data"]
        assert submission_data["session_id"] == session_id
        assert submission_data["request_id"] == request_id
        assert isinstance(submission_data["work_id"], int)
        assert submission_data["submission_status"] == "accepted"

        work_id = submission_data["work_id"]
        work = await chat_session_database.get(SessionReplyWorkItem, work_id)
        assert work is not None
        assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert work.execution_state["stream_requested"] is False
        assert work.execution_state["message_source"] == "http"

        messages = list((await chat_session_database.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        assert len(messages) == 1
        assert messages[0].content == "hello from HTTP"

        duplicate = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello from HTTP",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["data"]["work_id"] == work_id
        assert duplicate.json()["data"]["submission_status"] == "accepted"

        works_after_duplicate = list((await chat_session_database.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        messages_after_duplicate = list((await chat_session_database.execute(select(Message).where(Message.session_id == session_id))).scalars().all())
        assert [item.id for item in works_after_duplicate] == [work_id]
        assert [message.id for message in messages_after_duplicate] == [messages[0].id]

        conflict = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "different body",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == 400
        assert conflict.json()["message"] == t("ERR_CHAT_REQUEST_ID_CONFLICT")

        stream_rejected = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "stream body",
                "session_id": session_id,
                "request_id": "http-stream-request",
                "stream": True,
            },
        )
        assert stream_rejected.status_code == 200
        assert stream_rejected.json()["code"] == 400
        assert stream_rejected.json()["message"] == t("ERR_CHAT_STREAM_NOT_SUPPORTED")

        await chat_session_database.delete(work)
        await chat_session_database.commit()

        unavailable = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello from HTTP",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert unavailable.status_code == 400
        assert unavailable.json()["code"] == 400
        assert unavailable.json()["message"] == t("ERR_CHAT_REQUEST_WORK_UNAVAILABLE")

        final_messages = list((await chat_session_database.execute(select(Message).where(Message.session_id == session_id))).scalars().all())
        final_works = list((await chat_session_database.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        assert len(final_messages) == 1
        assert len(final_works) == 0


@pytest.mark.asyncio
async def test_http_session_recovers_in_progress_work_through_list_and_status(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    _patch_http_submission(monkeypatch, primary_profile)
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={"message": "create status session", "request_id": "status-session-request"},
        )
        session_id = created.json()["choices"][0]["message"]["content"]
        active_submission = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "active work",
                "session_id": session_id,
                "request_id": "active-request",
            },
        )
        active_work_id = active_submission.json()["data"]["work_id"]

        input_message = (await chat_session_database.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.desc()))).scalars().first()
        assert input_message is not None and input_message.id is not None

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_data = listed.json()["data"]
        assert len(listed_data) == 1
        session_row = listed_data[0]
        assert session_row["session_id"] == session_id
        assert session_row["uid"] == "user-1"
        assert session_row["username"] == "alice"
        assert session_row["latest_message_id"] == input_message.id
        assert session_row["is_loading"] is True
        assert session_row["reply_works"] == [
            {
                "work_id": active_work_id,
                "status": SessionReplyWorkStatus.READY_FOR_LLM.value,
                "request_ids": ["active-request"],
                "result_message_id": None,
                "merged_into_id": None,
                "sequence_no": 1,
            }
        ]

        active = await client.get(f"/api/v1/chat/reply-works/{active_work_id}")
        assert active.status_code == 200
        active_data = active.json()["data"]
        assert active_data["status"] == SessionReplyWorkStatus.READY_FOR_LLM.value
        assert active_data["response"] is None
        assert active_data["error"] is None

        active_work = await chat_session_database.get(SessionReplyWorkItem, active_work_id)
        assert active_work is not None
        success_message = Message(
            session_id=session_id,
            uid="user-1",
            profile_id=primary_profile.id,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="completed reply",
            is_processed=True,
        )
        await message_crud.persist(chat_session_database, message=success_message, commit=False)
        assert success_message.id is not None
        active_work.status = SessionReplyWorkStatus.SUCCEEDED
        active_work.result_message_id = success_message.id
        active_work.execution_state = {
            **active_work.execution_state,
            "response": {"content": "completed reply", "history": []},
        }
        await chat_session_database.commit()

        completed_list = await client.get("/api/v1/chat/sessions/list")
        assert completed_list.status_code == 200
        completed_session = completed_list.json()["data"][0]
        assert completed_session["is_loading"] is False
        assert completed_session["latest_message_id"] == success_message.id
        assert completed_session["reply_works"][0]["work_id"] == active_work_id
        assert completed_session["reply_works"][0]["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert completed_session["reply_works"][0]["result_message_id"] == success_message.id

        succeeded = await client.get(f"/api/v1/chat/reply-works/{active_work_id}")
        assert succeeded.status_code == 200
        succeeded_data = succeeded.json()["data"]
        assert succeeded_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert succeeded_data["result_message_id"] == success_message.id
        assert succeeded_data["response"]["content"] == "completed reply"
        assert succeeded_data["response"]["work_id"] == active_work_id
        assert succeeded_data["response"]["session_todo"] == {"revision": 0, "todos": []}
        assert succeeded_data["error"] is None

        failed_submission = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "failed work",
                "session_id": session_id,
                "request_id": "failed-request",
            },
        )
        failed_work_id = failed_submission.json()["data"]["work_id"]
        failed_work = await chat_session_database.get(SessionReplyWorkItem, failed_work_id)
        assert failed_work is not None
        failure_message = Message(
            session_id=session_id,
            uid="user-1",
            profile_id=primary_profile.id,
            role=MessageRole.ERR,
            type=MessageType.TEXT,
            content="worker failed",
            is_processed=True,
        )
        await message_crud.persist(chat_session_database, message=failure_message, commit=False)
        assert failure_message.id is not None
        failed_work.status = SessionReplyWorkStatus.FAILED
        failed_work.result_message_id = failure_message.id
        await chat_session_database.commit()

        failed = await client.get(f"/api/v1/chat/reply-works/{failed_work_id}")
        assert failed.status_code == 200
        failed_data = failed.json()["data"]
        assert failed_data["status"] == SessionReplyWorkStatus.FAILED.value
        assert failed_data["result_message_id"] == failure_message.id
        assert failed_data["response"] is None
        assert failed_data["error"] == "worker failed"

        auth_state["uid"] = "user-2"
        other_owner = await client.get("/api/v1/chat/sessions/list")
        assert other_owner.status_code == 200
        assert other_owner.json()["data"] == []

        forbidden = await client.get(f"/api/v1/chat/reply-works/{active_work_id}")
        assert forbidden.status_code == 404
        assert forbidden.json()["code"] == 404
        assert forbidden.json()["message"] == t("ERR_SESSION_REPLY_WORK_NOT_FOUND")


@pytest.mark.asyncio
async def test_http_reply_work_status_resolves_merged_work_and_enforces_owner(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="merged-status-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    target_work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        sequence_no=2,
        work_type="foreground_reply",
        source_type="user_message",
        source_id="target-source",
        dedupe_key="merged-target-work",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={
            "message_source": "http",
            "request_ids": ["one", "two"],
            "response": {"content": "joined", "history": []},
        },
    )
    chat_session_database.add_all([session, target_work])
    await chat_session_database.flush()
    assert target_work.id is not None
    original_work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        sequence_no=1,
        work_type="foreground_reply",
        source_type="user_message",
        source_id="original-source",
        dedupe_key="merged-original-work",
        status=SessionReplyWorkStatus.MERGED,
        merged_into_id=target_work.id,
    )
    chat_session_database.add(original_work)
    await chat_session_database.commit()
    await chat_session_database.refresh(original_work)
    assert original_work.id is not None

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resolved = await client.get(f"/api/v1/chat/reply-works/{original_work.id}")
        assert resolved.status_code == 200
        resolved_data = resolved.json()["data"]
        assert resolved_data["work_id"] == original_work.id
        assert resolved_data["resolved_work_id"] == target_work.id
        assert resolved_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert resolved_data["request_ids"] == ["one", "two"]
        assert resolved_data["response"]["content"] == "joined"
        assert resolved_data["response"]["history"] == []
        assert resolved_data["response"]["work_id"] == target_work.id

        auth_state["uid"] = "user-2"
        forbidden = await client.get(f"/api/v1/chat/reply-works/{original_work.id}")
        assert forbidden.status_code == 404
        assert forbidden.json()["code"] == 404
        assert forbidden.json()["message"] == t("ERR_SESSION_REPLY_WORK_NOT_FOUND")


@pytest.mark.asyncio
async def test_session_history_supports_forward_id_cursor_for_incremental_recovery(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="incremental-history-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
        show_tool_calls=True,
    )
    chat_session_database.add(session)
    await chat_session_database.flush()

    messages = []
    for index in range(45):
        message = Message(
            session_id=session.session_id,
            uid="user-1",
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content=f"message-{index + 1}",
            profile_id=primary_profile.id,
            is_processed=True,
        )
        chat_session_database.add(message)
        messages.append(message)
    await chat_session_database.commit()
    first_message_id = messages[0].id
    assert first_message_id is not None

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        legacy_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "page": 1,
                "size": 20,
            },
        )
        assert legacy_page.status_code == 200
        assert [item["id"] for item in legacy_page.json()["data"]] == [message.id for message in messages[25:45]]

        first_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "after_id": first_message_id,
                "size": 20,
            },
        )
        assert first_page.status_code == 200
        first_page_data = first_page.json()["data"]
        assert [item["id"] for item in first_page_data] == [message.id for message in messages[1:21]]

        second_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "after_id": first_page_data[-1]["id"],
                "size": 20,
            },
        )
        assert [item["id"] for item in second_page.json()["data"]] == [message.id for message in messages[21:41]]

        third_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "after_id": second_page.json()["data"][-1]["id"],
                "size": 20,
            },
        )
        assert [item["id"] for item in third_page.json()["data"]] == [message.id for message in messages[41:45]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "is_superuser"),
    [
        pytest.param("user-1", False, id="owner"),
        pytest.param("user-2", True, id="admin"),
    ],
)
async def test_background_task_pending_activity_is_not_limited_by_task_history_page(
    chat_session_database: AsyncSession,
    uid: str,
    is_superuser: bool,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="background-activity-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add(session)
    base_time = get_local_time() - timedelta(minutes=5)
    pending_task = BackgroundTask(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        tool_call_id="pending-task",
        tool_name="test_tool",
        status=BackgroundTaskStatus.RUNNING,
        arguments={},
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.PENDING,
        created_at=base_time,
    )
    chat_session_database.add(pending_task)
    for index in range(25):
        chat_session_database.add(
            BackgroundTask(
                uid="user-1",
                session_id=session.session_id,
                profile_id=primary_profile.id,
                tool_call_id=f"finished-{index}",
                tool_name="test_tool",
                status=BackgroundTaskStatus.SUCCEEDED,
                arguments={},
                result={"ok": True},
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                created_at=base_time + timedelta(seconds=index + 1),
            )
        )
    await chat_session_database.commit()
    await chat_session_database.refresh(pending_task)

    auth_state: dict[str, object] = {"uid": uid, "is_superuser": is_superuser}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_page = await client.get(
            "/api/v1/chat/background-tasks",
            params={"session_id": session.session_id, "page": 1, "size": 20},
        )
        assert first_page.status_code == 200
        assert pending_task.id not in {item["id"] for item in first_page.json()["data"]}

        active = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": session.session_id},
        )
        assert active.status_code == 200
        assert active.json()["data"] == {"has_pending_activity": True}

        pending_task.status = BackgroundTaskStatus.SUCCEEDED
        pending_task.reply_status = BackgroundTaskReplyStatus.RUNNING
        chat_session_database.add(pending_task)
        await chat_session_database.commit()

        pending_reply = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": session.session_id},
        )
        assert pending_reply.json()["data"] == {"has_pending_activity": True}

        pending_task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED
        chat_session_database.add(pending_task)
        await chat_session_database.commit()

        completed = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": session.session_id},
        )
        assert completed.json()["data"] == {"has_pending_activity": False}


@pytest.mark.asyncio
async def test_background_task_pending_activity_enforces_session_scope(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    assert other_profile.id is not None
    owner_session = ChatSession(
        session_id="background-pending-owner-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    other_session = ChatSession(
        session_id="background-pending-other-session",
        uid="user-2",
        profile_id=other_profile.id,
        source="http",
        reply_target_source="http",
    )
    task = BackgroundTask(
        uid="user-1",
        session_id=owner_session.session_id,
        profile_id=primary_profile.id,
        tool_call_id="background-pending-owner-task",
        tool_name="test_tool",
        status=BackgroundTaskStatus.RUNNING,
        arguments={},
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.PENDING,
        created_at=get_local_time(),
    )
    chat_session_database.add_all([owner_session, other_session, task])
    await chat_session_database.commit()
    await chat_session_database.refresh(owner_session)
    await chat_session_database.refresh(other_session)
    await chat_session_database.refresh(task)
    before_snapshots = {
        "owner_session": owner_session.model_dump(),
        "other_session": other_session.model_dump(),
        "task": task.model_dump(),
    }

    auth_state: dict[str, object] = {"uid": "user-2", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        forbidden = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": owner_session.session_id},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == 403
        assert forbidden.json()["data"] is None

        own = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": other_session.session_id},
        )
        assert own.status_code == 200
        assert own.json()["data"] == {"has_pending_activity": False}

        unknown = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": "background-pending-unknown-session"},
        )
        assert unknown.status_code == 200
        assert unknown.json()["data"] == {"has_pending_activity": False}

        auth_state["is_superuser"] = True

        admin_owner = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": owner_session.session_id},
        )
        assert admin_owner.status_code == 200
        assert admin_owner.json()["data"] == {"has_pending_activity": True}

        admin_unknown = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": "background-pending-unknown-session"},
        )
        assert admin_unknown.status_code == 200
        assert admin_unknown.json()["data"] == {"has_pending_activity": False}

    await chat_session_database.refresh(owner_session)
    await chat_session_database.refresh(other_session)
    await chat_session_database.refresh(task)
    assert {
        "owner_session": owner_session.model_dump(),
        "other_session": other_session.model_dump(),
        "task": task.model_dump(),
    } == before_snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws"])
@pytest.mark.parametrize("work_type", list(SessionReplyWorkType))
@pytest.mark.parametrize("status", list(SessionReplyWorkStatus))
async def test_session_list_loading_and_reply_running_follow_work_type_and_status(
    chat_session_database: AsyncSession,
    source: str,
    work_type: SessionReplyWorkType,
    status: SessionReplyWorkStatus,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    source_type_by_work_type = {
        SessionReplyWorkType.FOREGROUND_REPLY: SessionReplySourceType.USER_MESSAGE,
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION: SessionReplySourceType.AUDIT_RECORD,
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY: SessionReplySourceType.BACKGROUND_TASK,
        SessionReplyWorkType.BACKGROUND_TASK_RESUME: SessionReplySourceType.BACKGROUND_TASK,
        SessionReplyWorkType.AGENT_WORK: SessionReplySourceType.SCHEDULED_TASK_RUN,
    }
    interactive_work_types = {
        SessionReplyWorkType.FOREGROUND_REPLY,
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
    }
    session_id = f"session-list-state-{source}-{work_type.value}-{status.value}"
    work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session_id,
        profile_id=primary_profile.id,
        sequence_no=1,
        work_type=work_type,
        source_type=source_type_by_work_type[work_type],
        source_id="source-1",
        dedupe_key=f"{session_id}-work",
        status=status,
        execution_state={"message_source": source, "request_ids": [f"{session_id}-request"]},
    )
    chat_session_database.add_all(
        [
            ChatSession(
                session_id=session_id,
                uid="user-1",
                profile_id=primary_profile.id,
                source=source,
                reply_target_source=source,
            ),
            work,
        ]
    )
    await chat_session_database.commit()

    is_active = status in SESSION_REPLY_ACTIVE_STATUSES
    is_interactive = work_type in interactive_work_types
    should_cancel = is_active
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["is_loading"] is is_active
        assert listed_session["is_reply_running"] is (is_active and is_interactive)

        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"] == {
            "session_id": session_id,
            "cancelled_count": int(should_cancel),
        }

        await chat_session_database.refresh(work)
        assert work.status == (SessionReplyWorkStatus.CANCELLED if should_cancel else status)

        listed_after_stop = await client.get("/api/v1/chat/sessions/list")
        assert listed_after_stop.status_code == 200
        listed_session_after_stop = next(item for item in listed_after_stop.json()["data"] if item["session_id"] == session_id)
        assert listed_session_after_stop["is_reply_running"] is False
        assert listed_session_after_stop["is_loading"] is (is_active and not should_cancel)


@pytest.mark.asyncio
async def test_http_structured_voice_input_persists_message_parts_and_history_shape(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    _patch_http_submission(monkeypatch, primary_profile)
    session = ChatSession(
        session_id="http-structured-voice-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add(session)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    submitted_parts = [
        {"type": "text", "text": "语音转写内容", "input_source": "voice"},
        {"type": "text", "text": "键入补充"},
    ]
    request_id = "http-structured-voice-request"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": submitted_parts,
                "session_id": session.session_id,
                "request_id": request_id,
            },
        )

    assert response.status_code == 200
    response_data = response.json()["data"]
    assert response_data["session_id"] == session.session_id
    assert response_data["request_id"] == request_id
    work_id = response_data["work_id"]
    assert isinstance(work_id, int)

    work = await chat_session_database.get(SessionReplyWorkItem, work_id)
    assert work is not None
    assert work.source_type == SessionReplySourceType.USER_MESSAGE
    stored_message = await chat_session_database.get(Message, int(work.source_id))
    assert stored_message is not None
    assert stored_message.content is not None
    assert json.loads(stored_message.content) == submitted_parts
    history_message = MessageResponse.model_validate(stored_message)
    assert history_message.content == submitted_parts


@pytest.mark.asyncio
async def test_background_task_list_enforces_owner_scope_admin_scope_and_public_page_shape(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    assert other_profile.id is not None

    owner_session = ChatSession(
        session_id="background-list-owner-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    owner_secondary_session = ChatSession(
        session_id="background-list-owner-secondary-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    other_session = ChatSession(
        session_id="background-list-other-session",
        uid="user-2",
        profile_id=other_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add_all([owner_session, owner_secondary_session, other_session])

    base_time = get_local_time().replace(microsecond=0)
    tasks = [
        BackgroundTask(
            uid="user-1",
            session_id=owner_session.session_id,
            profile_id=primary_profile.id,
            tool_call_id="background-list-owner-old",
            tool_name="test_tool",
            status=BackgroundTaskStatus.SUCCEEDED,
            arguments={"secret": "owner-old-arguments"},
            result={"value": "owner-old"},
            error=None,
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
            locked_by="worker-owner-old",
            lock_until=1001,
            reply_locked_by="reply-owner-old",
            reply_lock_until=2001,
            attempt_count=1,
            extra={"internal": "owner-old-extra"},
            audit_record_id=3001,
            audit_execution_record_id=4001,
            created_at=base_time + timedelta(seconds=1),
        ),
        BackgroundTask(
            uid="user-1",
            session_id=owner_session.session_id,
            profile_id=primary_profile.id,
            tool_call_id="background-list-owner-new",
            tool_name="test_tool",
            status=BackgroundTaskStatus.FAILED,
            arguments={"secret": "owner-new-arguments"},
            result={"value": "owner-new"},
            error="owner task failed",
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.FAILED,
            locked_by="worker-owner-new",
            lock_until=1002,
            reply_locked_by="reply-owner-new",
            reply_lock_until=2002,
            attempt_count=2,
            extra={"internal": "owner-new-extra"},
            audit_record_id=3002,
            audit_execution_record_id=4002,
            created_at=base_time + timedelta(seconds=4),
        ),
        BackgroundTask(
            uid="user-1",
            session_id=owner_secondary_session.session_id,
            profile_id=primary_profile.id,
            tool_call_id="background-list-owner-secondary",
            tool_name="test_tool",
            status=BackgroundTaskStatus.RUNNING,
            arguments={"secret": "owner-secondary-arguments"},
            result=None,
            error=None,
            auto_reply=True,
            reply_status=BackgroundTaskReplyStatus.RUNNING,
            locked_by="worker-owner-secondary",
            lock_until=1003,
            reply_locked_by="reply-owner-secondary",
            reply_lock_until=2003,
            attempt_count=3,
            extra={"internal": "owner-secondary-extra"},
            audit_record_id=3003,
            audit_execution_record_id=4003,
            created_at=base_time + timedelta(seconds=2),
        ),
        BackgroundTask(
            uid="user-2",
            session_id=other_session.session_id,
            profile_id=other_profile.id,
            tool_call_id="background-list-other-owner",
            tool_name="test_tool",
            status=BackgroundTaskStatus.CANCELLED,
            arguments={"secret": "other-owner-arguments"},
            result={"value": "other-owner"},
            error="cancelled",
            auto_reply=False,
            reply_status=BackgroundTaskReplyStatus.NONE,
            locked_by="worker-other-owner",
            lock_until=1004,
            reply_locked_by="reply-other-owner",
            reply_lock_until=2004,
            attempt_count=4,
            extra={"internal": "other-owner-extra"},
            audit_record_id=3004,
            audit_execution_record_id=4004,
            created_at=base_time + timedelta(seconds=3),
        ),
    ]
    chat_session_database.add_all(tasks)
    await chat_session_database.commit()
    assert all(task.id is not None for task in tasks)

    for task in tasks:
        await chat_session_database.refresh(task)
    before_snapshots = {task.id: task.model_dump() for task in tasks}
    public_fields = {
        "id",
        "uid",
        "session_id",
        "profile_id",
        "tool_call_id",
        "tool_name",
        "status",
        "result",
        "error",
        "auto_reply",
        "reply_status",
        "attempt_count",
        "created_at",
        "started_at",
        "finished_at",
    }
    internal_fields = {
        "arguments",
        "extra",
        "locked_by",
        "lock_until",
        "reply_locked_by",
        "reply_lock_until",
        "audit_record_id",
        "audit_execution_record_id",
    }

    def assert_public_rows(rows: list[dict[str, object]], expected_tasks: list[BackgroundTask]) -> None:
        assert [row["id"] for row in rows] == [task.id for task in expected_tasks]
        for row, task in zip(rows, expected_tasks):
            assert public_fields <= row.keys()
            assert internal_fields.isdisjoint(row.keys())
            assert row["uid"] == task.uid
            assert row["session_id"] == task.session_id
            assert row["result"] == task.result
            assert row["status"] == task.status.value

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    owner_tasks_desc = sorted(
        (task for task in tasks if task.uid == "user-1"),
        key=lambda task: task.created_at,
        reverse=True,
    )
    all_tasks_desc = sorted(tasks, key=lambda task: task.created_at, reverse=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner_list = await client.get("/api/v1/chat/background-tasks")
        assert owner_list.status_code == 200
        assert_public_rows(owner_list.json()["data"], owner_tasks_desc)

        owner_other_session = await client.get(
            "/api/v1/chat/background-tasks",
            params={"session_id": other_session.session_id},
        )
        assert owner_other_session.status_code == 200
        assert owner_other_session.json()["data"] == []

        auth_state["is_superuser"] = True

        admin_page_one = await client.get(
            "/api/v1/chat/background-tasks",
            params={"page": 1, "size": 2},
        )
        assert admin_page_one.status_code == 200
        assert_public_rows(admin_page_one.json()["data"], all_tasks_desc[:2])

        admin_page_two = await client.get(
            "/api/v1/chat/background-tasks",
            params={"page": 2, "size": 2},
        )
        assert admin_page_two.status_code == 200
        assert_public_rows(admin_page_two.json()["data"], all_tasks_desc[2:])

        admin_session = await client.get(
            "/api/v1/chat/background-tasks",
            params={"session_id": other_session.session_id},
        )
        assert admin_session.status_code == 200
        assert_public_rows(
            admin_session.json()["data"],
            [task for task in all_tasks_desc if task.session_id == other_session.session_id],
        )

    for task in tasks:
        await chat_session_database.refresh(task)
    assert {task.id: task.model_dump() for task in tasks} == before_snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "is_superuser", "can_read"),
    [
        pytest.param("user-1", False, True, id="owner"),
        pytest.param("user-2", False, False, id="other-user"),
        pytest.param("user-2", True, True, id="superuser"),
    ],
)
async def test_background_task_detail_enforces_owner_scope_and_public_shape(
    chat_session_database: AsyncSession,
    uid: str,
    is_superuser: bool,
    can_read: bool,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="background-detail-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    task = BackgroundTask(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        tool_call_id="background-detail-task",
        tool_name="test_tool",
        status=BackgroundTaskStatus.FAILED,
        arguments={"secret": "task-arguments"},
        result={"value": "task-result"},
        error="task failed",
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.FAILED,
        locked_by="worker-detail",
        lock_until=1001,
        reply_locked_by="reply-detail",
        reply_lock_until=2001,
        attempt_count=2,
        extra={"internal": "task-extra"},
        audit_record_id=3001,
        audit_execution_record_id=4001,
        created_at=get_local_time().replace(microsecond=0),
    )
    chat_session_database.add_all([session, task])
    await chat_session_database.commit()
    await chat_session_database.refresh(task)
    before_snapshot = task.model_dump()

    auth_state: dict[str, object] = {"uid": uid, "is_superuser": is_superuser}
    app = _build_app(chat_session_database, auth_state)
    internal_fields = {
        "arguments",
        "extra",
        "locked_by",
        "lock_until",
        "reply_locked_by",
        "reply_lock_until",
        "audit_record_id",
        "audit_execution_record_id",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        detail = await client.get(f"/api/v1/chat/background-tasks/{task.id}")
        assert detail.status_code == 200
        detail_payload = detail.json()
        if can_read:
            assert detail_payload["code"] == 200
            detail_data = detail_payload["data"]
            assert detail_data["uid"] == "user-1"
            assert detail_data["session_id"] == session.session_id
            assert detail_data["result"] == task.result
            assert detail_data["error"] == task.error
            assert detail_data["status"] == task.status.value
            assert internal_fields.isdisjoint(detail_data)
        else:
            assert detail_payload["code"] == 404
            assert detail_payload["data"] is None

        missing = await client.get("/api/v1/chat/background-tasks/999999")
        assert missing.status_code == 200
        missing_payload = missing.json()
        assert missing_payload["code"] == 404
        assert missing_payload["data"] is None

    await chat_session_database.refresh(task)
    assert task.model_dump() == before_snapshot
