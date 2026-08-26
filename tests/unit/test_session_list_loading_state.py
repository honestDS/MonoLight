from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.api.v1 import chat as chat_api
from app.core.crud.message import message_crud
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


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    User.__table__,
                    ChatSession.__table__,
                    Message.__table__,
                    SessionReplyWorkItem.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


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
    db_session.add(ChatSession(session_id="session-1", uid="user-1", profile_id=1))
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
