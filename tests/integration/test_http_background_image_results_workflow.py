import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from app.api.v1.chat import router
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.database import get_db

UID = "http-image-user"
SESSION_ID = "http-image-session"


async def _create_registered_tables(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    await connection.run_sync(SQLModel.metadata.create_all)


async def _seed_reply_work(
    db_session: AsyncSession,
    *,
    task_status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
) -> tuple[Profile, ChatSession, SessionReplyWorkItem, BackgroundTask]:
    profile = Profile(uid=UID, name="http image profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    assert profile.id is not None

    session = ChatSession(
        session_id=SESSION_ID,
        uid=UID,
        profile_id=profile.id,
        source="http",
        show_tool_calls=False,
    )
    work = SessionReplyWorkItem(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=profile.id,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="http-image-foreground-work",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={
            "response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "已提交生图",
                        },
                        "finish_reason": True,
                    }
                ],
                "history": [],
            }
        },
    )
    task = BackgroundTask(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=profile.id,
        tool_call_id="generate-image-call",
        tool_name="generate_image",
        status=task_status,
        arguments={"prompt": "test apple"},
        auto_reply=True,
        reply_status=reply_status,
    )
    db_session.add_all([session, work, task])
    await db_session.commit()
    await db_session.refresh(work)
    await db_session.refresh(task)
    assert work.id is not None
    assert task.id is not None
    return profile, session, work, task


def _build_app(db_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    def override_get_current_user():
        return SimpleNamespace(uid=UID)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "reply_status"),
    [
        (BackgroundTaskStatus.PENDING, BackgroundTaskReplyStatus.PENDING),
        (BackgroundTaskStatus.RUNNING, BackgroundTaskReplyStatus.PENDING),
        (BackgroundTaskStatus.SUCCEEDED, BackgroundTaskReplyStatus.RUNNING),
    ],
)
async def test_http_reply_work_status_exposes_hidden_background_image_activity(
    db_session: AsyncSession,
    task_status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
) -> None:
    await _create_registered_tables(db_session)
    _profile, _session, work, _task = await _seed_reply_work(
        db_session,
        task_status=task_status,
        reply_status=reply_status,
    )
    app = _build_app(db_session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/chat/reply-works/{work.id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert data["response"]["history"] == []
    assert data["response"]["has_background_tasks"] is True
    assert data["response"]["background_task_poll_interval"] == 2


@pytest.mark.asyncio
async def test_completed_background_image_message_is_returned_by_http_history(
    db_session: AsyncSession,
) -> None:
    await _create_registered_tables(db_session)
    profile, session, work, _task = await _seed_reply_work(
        db_session,
        task_status=BackgroundTaskStatus.SUCCEEDED,
        reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
    )
    file_payload = {
        "id": "test-image-id",
        "name": "test-apple.png",
        "mime_type": "image/png",
        "size": 4,
        "download_url": "/test-downloads/test-apple.png",
        "previewable": True,
    }
    assistant_files = {
        "type": "assistant_files",
        "text": "",
        "files": [file_payload],
    }
    message = Message(
        session_id=session.session_id,
        uid=UID,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content=json.dumps(assistant_files, ensure_ascii=False),
        profile_id=profile.id,
    )
    db_session.add(message)
    await db_session.flush()
    assert message.id is not None
    work.result_message_id = message.id
    db_session.add(work)
    await db_session.commit()
    app = _build_app(db_session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pending_activity = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": SESSION_ID},
        )
        history = await client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": SESSION_ID},
        )

    assert pending_activity.status_code == 200
    assert pending_activity.json()["data"] == {"has_pending_activity": False}
    assert history.status_code == 200
    history_items = history.json()["data"]
    assert len(history_items) == 1
    assert history_items[0]["role"] == MessageRole.ASSISTANT.value
    assert history_items[0]["type"] == MessageType.TEXT.value
    assert json.loads(history_items[0]["content"]) == assistant_files
