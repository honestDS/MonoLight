from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.chat import router
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.security import get_current_user
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.database import get_db

ACTIVITY_FIELDS = {
    "session_id",
    "latest_message_id",
    "last_active",
    "source",
    "is_reply_running",
    "is_loading",
}
INTERACTIVE_WORK_TYPES = (
    SessionReplyWorkType.FOREGROUND_REPLY,
    SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
)
STATUS_CASES = [(work_type, status, work_type in INTERACTIVE_WORK_TYPES and status in SESSION_REPLY_ACTIVE_STATUSES) for work_type in SessionReplyWorkType for status in SessionReplyWorkStatus]


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as session:
            yield session


def _build_app(db: AsyncSession, auth_state: dict[str, object]) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        yield db

    def override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(
            uid=auth_state["uid"],
            is_superuser=auth_state["is_superuser"],
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


@pytest_asyncio.fixture
async def api_context(db_session: AsyncSession):
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield db_session, auth_state, client


def _make_session(
    session_id: str,
    uid: str,
    *,
    created_at: datetime,
    source: str = "http",
) -> ChatSession:
    return ChatSession(
        session_id=session_id,
        uid=uid,
        source=source,
        created_at=created_at,
    )


async def _persist_message(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    created_at: datetime,
    role: MessageRole = MessageRole.USER,
    content: str = "message",
) -> Message:
    message = Message(
        session_id=session_id,
        uid=uid,
        role=role,
        type=MessageType.TEXT,
        content=content,
        profile_id=1,
        created_at=created_at,
    )
    await message_crud.persist(db, message=message, commit=False)
    return message


async def _add_work(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    work_type: SessionReplyWorkType,
    status: SessionReplyWorkStatus,
    sequence_no: int = 1,
    source_type: SessionReplySourceType = SessionReplySourceType.USER_MESSAGE,
    source_id: str = "source",
) -> SessionReplyWorkItem:
    work = SessionReplyWorkItem(
        uid=uid,
        session_id=session_id,
        profile_id=1,
        sequence_no=sequence_no,
        work_type=work_type,
        source_type=source_type,
        source_id=source_id,
        dedupe_key=f"{session_id}:{sequence_no}:{work_type.value}:{status.value}",
        status=status,
        execution_state={"message_source": "http"},
        locked_by="worker" if status == SessionReplyWorkStatus.RUNNING else None,
        lock_until=2_000_000_000 if status == SessionReplyWorkStatus.RUNNING else None,
        available_at=0,
    )
    db.add(work)
    await db.commit()
    return work


async def _add_users(db: AsyncSession, *uids: str) -> None:
    db.add_all([User(uid=uid, username=uid) for uid in uids])
    await db.commit()


@pytest.mark.asyncio
async def test_activity_returns_all_owned_sessions_with_exact_projection_and_isolation(api_context) -> None:
    db, auth_state, client = api_context
    base = datetime(2026, 2, 1, 1, 2, 3)
    empty_session = _make_session("empty-session", "user-1", created_at=base, source="")
    message_session = _make_session("message-session", "user-1", created_at=base, source="websocket")
    other_session = _make_session("other-session", "user-2", created_at=base + timedelta(days=1))
    db.add_all([empty_session, message_session, other_session])
    await db.commit()
    message = await _persist_message(
        db,
        session_id=message_session.session_id,
        uid=message_session.uid,
        created_at=datetime(2026, 2, 2, 3, 4, 5),
    )
    await db.commit()

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == [
        {
            "session_id": "message-session",
            "latest_message_id": message.id,
            "last_active": "2026-02-02 03:04:05",
            "source": "websocket",
            "is_reply_running": False,
            "is_loading": False,
        },
        {
            "session_id": "empty-session",
            "latest_message_id": None,
            "last_active": "2026-02-01 01:02:03",
            "source": "http",
            "is_reply_running": False,
            "is_loading": False,
        },
    ]
    assert all(set(row) == ACTIVITY_FIELDS for row in data)

    auth_state["is_superuser"] = True
    admin_response = await client.get("/api/v1/chat/sessions/activity")
    assert [row["session_id"] for row in admin_response.json()["data"]] == [
        "message-session",
        "other-session",
        "empty-session",
    ]

    auth_state.update(uid="user-2", is_superuser=False)
    other_response = await client.get("/api/v1/chat/sessions/activity")
    assert [row["session_id"] for row in other_response.json()["data"]] == ["other-session"]


@pytest.mark.asyncio
async def test_activity_and_full_list_reflect_admin_cross_user_updates(api_context) -> None:
    db, auth_state, client = api_context
    await _add_users(db, "user-1", "user-2")
    admin_session_id = "admin-session"
    other_session_id = "other-session"
    admin_session = _make_session(
        admin_session_id,
        "user-1",
        created_at=datetime(2026, 8, 1, 1, 2, 3),
    )
    other_session = _make_session(
        other_session_id,
        "user-2",
        created_at=datetime(2026, 8, 1, 2, 3, 4),
    )
    db.add_all([admin_session, other_session])
    await db.commit()
    auth_state["is_superuser"] = True

    activity_response = await client.get("/api/v1/chat/sessions/activity")
    full_response = await client.get("/api/v1/chat/sessions/list")
    assert activity_response.status_code == 200
    assert full_response.status_code == 200
    activity_data = activity_response.json()["data"]
    full_data = full_response.json()["data"]
    activity_by_session = {row["session_id"]: row for row in activity_data}
    full_by_session = {row["session_id"]: row for row in full_data}
    assert set(activity_by_session) == set(full_by_session) == {admin_session_id, other_session_id}
    assert {session_id: {field: row[field] for field in ACTIVITY_FIELDS} for session_id, row in activity_by_session.items()} == {session_id: {field: row[field] for field in ACTIVITY_FIELDS} for session_id, row in full_by_session.items()}

    other_message = await _persist_message(
        db,
        session_id=other_session_id,
        uid="user-2",
        created_at=datetime(2026, 8, 2, 3, 4, 5),
    )
    await db.commit()
    work = await _add_work(
        db,
        session_id=other_session_id,
        uid="user-2",
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.RUNNING,
    )

    activity_response = await client.get("/api/v1/chat/sessions/activity")
    full_response = await client.get("/api/v1/chat/sessions/list")
    assert activity_response.status_code == 200
    assert full_response.status_code == 200
    activity_data = activity_response.json()["data"]
    full_data = full_response.json()["data"]
    activity_by_session = {row["session_id"]: row for row in activity_data}
    full_by_session = {row["session_id"]: row for row in full_data}
    assert set(activity_by_session) == set(full_by_session) == {admin_session_id, other_session_id}
    assert activity_by_session[other_session_id]["latest_message_id"] == other_message.id
    assert activity_by_session[other_session_id]["last_active"] == "2026-08-02 03:04:05"
    assert activity_by_session[other_session_id]["is_reply_running"] is True
    assert {session_id: {field: row[field] for field in ACTIVITY_FIELDS} for session_id, row in activity_by_session.items()} == {session_id: {field: row[field] for field in ACTIVITY_FIELDS} for session_id, row in full_by_session.items()}

    assert await session_reply_work_item_crud.mark_terminal(
        db,
        work_id=work.id,
        worker_id="worker",
        status=SessionReplyWorkStatus.SUCCEEDED,
    )
    await db.commit()

    activity_response = await client.get("/api/v1/chat/sessions/activity")
    full_response = await client.get("/api/v1/chat/sessions/list")
    assert activity_response.status_code == 200
    assert full_response.status_code == 200
    activity_data = activity_response.json()["data"]
    full_data = full_response.json()["data"]
    activity_by_session = {row["session_id"]: row for row in activity_data}
    full_by_session = {row["session_id"]: row for row in full_data}
    assert set(activity_by_session) == set(full_by_session) == {admin_session_id, other_session_id}
    assert activity_by_session[other_session_id]["is_reply_running"] is False
    assert {session_id: {field: row[field] for field in ACTIVITY_FIELDS} for session_id, row in activity_by_session.items()} == {session_id: {field: row[field] for field in ACTIVITY_FIELDS} for session_id, row in full_by_session.items()}

    auth_state["is_superuser"] = False
    regular_response = await client.get("/api/v1/chat/sessions/activity")
    assert regular_response.status_code == 200
    assert {row["session_id"] for row in regular_response.json()["data"]} == {admin_session_id}


@pytest.mark.asyncio
async def test_activity_does_not_truncate_owned_sessions_over_one_hundred(api_context) -> None:
    db, _auth_state, client = api_context
    base = datetime(2026, 3, 1)
    sessions = [
        _make_session(
            f"many-session-{index:03d}",
            "user-1",
            created_at=base + timedelta(seconds=index),
        )
        for index in range(105)
    ]
    db.add_all(sessions)
    await db.commit()

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 105
    assert {row["session_id"] for row in data} == {session.session_id for session in sessions}
    assert [row["session_id"] for row in data] == sorted(
        (session.session_id for session in sessions),
        reverse=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("work_type", "status", "expected_reply"), STATUS_CASES)
async def test_activity_projects_sidebar_loading_and_interactive_reply_statuses(
    api_context,
    work_type: SessionReplyWorkType,
    status: SessionReplyWorkStatus,
    expected_reply: bool,
) -> None:
    db, _auth_state, client = api_context
    await _add_users(db, "user-1")
    session = _make_session(
        "status-session",
        "user-1",
        created_at=datetime(2026, 4, 1, 1, 2, 3),
    )
    db.add(session)
    await db.commit()
    await _add_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        work_type=work_type,
        status=status,
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    activity_row = response.json()["data"][0]
    assert activity_row["is_loading"] is (status in SESSION_REPLY_ACTIVE_STATUSES)
    assert activity_row["is_reply_running"] is expected_reply

    full_response = await client.get("/api/v1/chat/sessions/list")
    assert full_response.status_code == 200
    full_row = full_response.json()["data"][0]
    assert {field: full_row[field] for field in ACTIVITY_FIELDS} == activity_row


@pytest.mark.asyncio
async def test_activity_stays_running_for_older_active_work_with_newer_terminal_work(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        "mixed-status-session",
        "user-1",
        created_at=datetime(2026, 5, 1, 1, 2, 3),
    )
    db.add(session)
    await db.commit()
    await _add_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.RUNNING,
        sequence_no=1,
    )
    await _add_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=2,
    )

    response = await client.get("/api/v1/chat/sessions/activity")
    assert response.status_code == 200
    assert response.json()["data"][0]["is_reply_running"] is True
    assert response.json()["data"][0]["is_loading"] is True


@pytest.mark.asyncio
async def test_activity_matches_full_list_and_successful_work_remains_recoverable(api_context) -> None:
    db, _auth_state, client = api_context
    await _add_users(db, "user-1")
    session = _make_session(
        "recoverable-session",
        "user-1",
        created_at=datetime(2026, 6, 1, 1, 2, 3),
        source="",
    )
    db.add(session)
    await db.commit()
    input_message = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        created_at=datetime(2026, 6, 1, 2, 3, 4),
    )
    await db.commit()
    work = await _add_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.RUNNING,
        source_id=str(input_message.id),
    )

    activity_response = await client.get("/api/v1/chat/sessions/activity")
    full_response = await client.get("/api/v1/chat/sessions/list")
    assert activity_response.status_code == 200
    assert full_response.status_code == 200
    activity_row = activity_response.json()["data"][0]
    full_row = full_response.json()["data"][0]
    assert {field: full_row[field] for field in ACTIVITY_FIELDS} == activity_row
    assert full_row["reply_works"] == [
        {
            "work_id": work.id,
            "status": SessionReplyWorkStatus.RUNNING.value,
            "request_ids": [],
            "result_message_id": None,
            "merged_into_id": None,
            "sequence_no": 1,
        }
    ]

    result_message = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        role=MessageRole.ASSISTANT,
        content="result",
        created_at=datetime(2026, 6, 1, 2, 4, 5),
    )
    await db.commit()
    assert await session_reply_work_item_crud.mark_terminal(
        db,
        work_id=work.id,
        worker_id="worker",
        status=SessionReplyWorkStatus.SUCCEEDED,
        result_message_id=result_message.id,
    )

    activity_after = await client.get("/api/v1/chat/sessions/activity")
    full_after = await client.get("/api/v1/chat/sessions/list")
    assert activity_after.status_code == 200
    assert full_after.status_code == 200
    activity_after_row = activity_after.json()["data"][0]
    full_after_row = full_after.json()["data"][0]
    assert activity_after_row["is_reply_running"] is False
    assert {field: full_after_row[field] for field in ACTIVITY_FIELDS} == activity_after_row
    assert full_after_row["reply_works"] == [
        {
            "work_id": work.id,
            "status": SessionReplyWorkStatus.SUCCEEDED.value,
            "request_ids": [],
            "result_message_id": result_message.id,
            "merged_into_id": None,
            "sequence_no": 1,
        }
    ]


@pytest.mark.asyncio
async def test_activity_uses_one_lightweight_select_without_message_or_full_work_read(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        "performance-session",
        "user-1",
        created_at=datetime(2026, 7, 1, 1, 2, 3),
    )
    db.add(session)
    await db.commit()
    await _add_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        status=SessionReplyWorkStatus.RUNNING,
    )

    statements: list[str] = []

    def capture_sql(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = statement.strip()
        if normalized.upper().startswith("SELECT"):
            statements.append(normalized)

    sync_engine = db.sync_session.bind
    assert sync_engine is not None
    event.listen(sync_engine, "before_cursor_execute", capture_sql)
    try:
        response = await client.get("/api/v1/chat/sessions/activity")
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture_sql)

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "session_id": "performance-session",
            "latest_message_id": None,
            "last_active": "2026-07-01 01:02:03",
            "source": "http",
            "is_reply_running": True,
            "is_loading": True,
        }
    ]
    assert len(statements) == 1
    sql = statements[0].lower()
    assert "from message" not in sql
    assert "join message" not in sql
    assert "select session_reply_work_item." not in sql
    assert "row_number" not in sql
    assert " over " not in sql
