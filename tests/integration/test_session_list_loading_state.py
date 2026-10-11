from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import chat as chat_api
from app.core.crud.session.message import message_crud
from app.models.message import Message
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SESSION_REPLY_TERMINAL_STATUSES,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from tests.integration.test_chat_session_workflow import _build_app


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncSession:
    async with database_factory(
        tables=[
            User.__table__,
            ChatSession.__table__,
            Message.__table__,
            SessionReplyWorkItem.__table__,
        ]
    ) as factory:
        async with factory() as session:
            yield session


def test_session_reply_work_statuses_have_complete_loading_classification():
    assert SESSION_REPLY_ACTIVE_STATUSES.isdisjoint(SESSION_REPLY_TERMINAL_STATUSES)
    assert SESSION_REPLY_ACTIVE_STATUSES | SESSION_REPLY_TERMINAL_STATUSES == set(SessionReplyWorkStatus)


def _work(*, session_id: str, status: SessionReplyWorkStatus, sequence_no: int) -> SessionReplyWorkItem:
    return SessionReplyWorkItem(
        uid="user-1",
        session_id=session_id,
        profile_id=1,
        sequence_no=sequence_no,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=f"source-{sequence_no}",
        dedupe_key=f"session-list-loading:{sequence_no}",
        status=status,
    )


@pytest.mark.asyncio
async def test_user_sessions_loading_state_follows_persisted_reply_work_status(db_session: AsyncSession):
    db_session.add(User(uid="user-1", username="alice"))

    statuses = [*SESSION_REPLY_ACTIVE_STATUSES, *SESSION_REPLY_TERMINAL_STATUSES]
    for index, status in enumerate(statuses, start=1):
        session_id = f"session-{status.value}"
        db_session.add(ChatSession(session_id=session_id, uid="user-1", profile_id=1))
        db_session.add(_work(session_id=session_id, status=status, sequence_no=index))
    db_session.add(ChatSession(session_id="session-without-work", uid="user-1", profile_id=1))
    await db_session.commit()

    sessions = await message_crud.get_user_sessions(db_session, uid="user-1")
    loading_by_session = {row.session_id: bool(row.is_loading) for row in sessions}

    for status in SESSION_REPLY_ACTIVE_STATUSES:
        assert loading_by_session[f"session-{status.value}"] is True
    for status in SESSION_REPLY_TERMINAL_STATUSES:
        assert loading_by_session[f"session-{status.value}"] is False
    assert loading_by_session["session-without-work"] is False


@pytest.mark.asyncio
async def test_session_list_api_exposes_loading_state(db_session: AsyncSession):
    db_session.add(User(uid="user-1", username="alice"))
    db_session.add(ChatSession(session_id="session-1", uid="user-1", profile_id=1, show_reasoning=False))
    db_session.add(_work(session_id="session-1", status=SessionReplyWorkStatus.RUNNING, sequence_no=1))
    await db_session.commit()

    response = await chat_api.get_user_sessions(
        db=db_session,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 200
    assert response.data is not None
    assert response.data[0]["session_id"] == "session-1"
    assert response.data[0]["is_loading"] is True
    assert response.data[0]["show_reasoning"] is False


@pytest.mark.asyncio
async def test_session_list_api_admin_recovers_cross_owner_http_reply_works(db_session: AsyncSession):
    sessions = [
        ChatSession(session_id="session-1", uid="user-1", profile_id=1),
        ChatSession(session_id="session-2", uid="user-2", profile_id=1),
    ]
    db_session.add_all(
        [
            User(uid="user-1", username="alice"),
            User(uid="user-2", username="bob"),
            *sessions,
        ]
    )
    await db_session.flush()

    def make_work(
        *,
        uid: str,
        session_id: str,
        sequence_no: int,
        work_type: SessionReplyWorkType,
        source_type: SessionReplySourceType,
        message_source: str,
        request_id: str,
        status: SessionReplyWorkStatus,
    ) -> SessionReplyWorkItem:
        return SessionReplyWorkItem(
            uid=uid,
            session_id=session_id,
            profile_id=1,
            sequence_no=sequence_no,
            work_type=work_type,
            source_type=source_type,
            source_id=f"source-{uid}-{sequence_no}",
            dedupe_key=f"session-list-cross-owner:{uid}:{sequence_no}",
            status=status,
            execution_state={"message_source": message_source, "request_ids": [request_id]},
        )

    owner_http = make_work(
        uid="user-1",
        session_id="session-1",
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        message_source="http",
        request_id="owner-http-request",
        status=SessionReplyWorkStatus.RUNNING,
    )
    other_http = make_work(
        uid="user-2",
        session_id="session-2",
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        message_source="http",
        request_id="other-http-request",
        status=SessionReplyWorkStatus.SUCCEEDED,
    )
    db_session.add_all(
        [
            owner_http,
            other_http,
            make_work(
                uid="user-1",
                session_id="session-1",
                sequence_no=2,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                message_source="ws",
                request_id="owner-ws-request",
                status=SessionReplyWorkStatus.SUCCEEDED,
            ),
            make_work(
                uid="user-1",
                session_id="session-1",
                sequence_no=3,
                work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                source_type=SessionReplySourceType.BACKGROUND_TASK,
                message_source="http",
                request_id="owner-background-request",
                status=SessionReplyWorkStatus.SUCCEEDED,
            ),
            make_work(
                uid="user-2",
                session_id="session-2",
                sequence_no=2,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                message_source="ws",
                request_id="other-ws-request",
                status=SessionReplyWorkStatus.SUCCEEDED,
            ),
            make_work(
                uid="user-2",
                session_id="session-2",
                sequence_no=3,
                work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                source_type=SessionReplySourceType.BACKGROUND_TASK,
                message_source="http",
                request_id="other-background-request",
                status=SessionReplyWorkStatus.SUCCEEDED,
            ),
        ]
    )
    await db_session.commit()

    def expected_reply(work: SessionReplyWorkItem, request_id: str) -> dict[str, object]:
        return {
            "work_id": work.id,
            "status": work.status.value,
            "request_ids": [request_id],
            "result_message_id": None,
            "merged_into_id": None,
            "sequence_no": work.sequence_no,
        }

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(db_session, auth_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        regular = await client.get("/api/v1/chat/sessions/list")
        assert regular.status_code == 200
        regular_rows = regular.json()["data"]
        assert [row["session_id"] for row in regular_rows] == ["session-1"]
        assert regular_rows[0]["reply_works"] == [expected_reply(owner_http, "owner-http-request")]

        auth_state["is_superuser"] = True
        admin = await client.get("/api/v1/chat/sessions/list")
        assert admin.status_code == 200
        rows_by_session = {row["session_id"]: row for row in admin.json()["data"]}
        assert set(rows_by_session) == {"session-1", "session-2"}
        assert rows_by_session["session-1"]["reply_works"] == [expected_reply(owner_http, "owner-http-request")]
        assert rows_by_session["session-2"]["reply_works"] == [expected_reply(other_http, "other-http-request")]

        auth_state["is_superuser"] = False
        regular_again = await client.get("/api/v1/chat/sessions/list")
        assert regular_again.status_code == 200
        assert [row["session_id"] for row in regular_again.json()["data"]] == ["session-1"]
        assert regular_again.json()["data"][0]["reply_works"] == [expected_reply(owner_http, "owner-http-request")]
