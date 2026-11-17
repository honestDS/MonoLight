import asyncio
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.models.message import Message, MessageCreate, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem

SESSION_ID = "activity-projection-session"
UID = "activity-projection-user"
LAST_READ_MESSAGE_ID = 314
LAST_READ_AT = datetime(2026, 1, 1, 12, 0, 0)


@pytest_asyncio.fixture
async def activity_session_factory(database_factory) -> async_sessionmaker[AsyncSession]:
    async with database_factory(
        foreign_keys=True,
        wal=True,
    ) as session_factory:
        async with session_factory() as db:
            db.add(
                ChatSession(
                    session_id=SESSION_ID,
                    uid=UID,
                    last_read_message_id=LAST_READ_MESSAGE_ID,
                    last_read_at=LAST_READ_AT,
                )
            )
            await db.commit()
        yield session_factory


def _message_data(
    *,
    session_id: str = SESSION_ID,
    uid: str = UID,
    message_id: int | None = None,
    created_at: datetime | None = None,
    message_type: MessageType = MessageType.TEXT,
    dedupe_key: str | None = None,
    content: str = "activity projection test",
) -> dict[str, object]:
    data: dict[str, object] = {
        "session_id": session_id,
        "uid": uid,
        "role": MessageRole.USER,
        "type": message_type,
        "content": content,
        "profile_id": 1,
    }
    if message_id is not None:
        data["id"] = message_id
    if created_at is not None:
        data["created_at"] = created_at
    if dedupe_key is not None:
        data["dedupe_key"] = dedupe_key
    return data


async def _query_activity(db: AsyncSession, *, session_id: str = SESSION_ID, uid: str = UID) -> dict[str, object]:
    result = await db.execute(
        select(
            ChatSession.latest_message_id,
            ChatSession.last_message_at,
            ChatSession.last_read_message_id,
            ChatSession.last_read_at,
        ).where(ChatSession.session_id == session_id, ChatSession.uid == uid)
    )
    row = result.one()
    return {
        "latest_message_id": row[0],
        "last_message_at": row[1],
        "last_read_message_id": row[2],
        "last_read_at": row[3],
    }


async def _activity(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str = SESSION_ID,
    uid: str = UID,
) -> dict[str, object]:
    async with session_factory() as db:
        return await _query_activity(db, session_id=session_id, uid=uid)


async def _message_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str = SESSION_ID,
    uid: str = UID,
) -> list[tuple[int, datetime]]:
    async with session_factory() as db:
        result = await db.execute(select(Message.id, Message.created_at).where(Message.session_id == session_id, Message.uid == uid).order_by(Message.id.asc()))
        return [(row[0], row[1]) for row in result.all()]


@pytest.mark.asyncio
async def test_all_message_types_update_activity_projection(activity_session_factory):
    async with activity_session_factory() as db:
        for message_type in MessageType:
            await message_crud.create(
                db,
                obj_in=MessageCreate(
                    session_id=SESSION_ID,
                    uid=UID,
                    role=MessageRole.USER,
                    type=message_type,
                    content=message_type.value,
                    profile_id=1,
                ),
            )

    rows = await _message_rows(activity_session_factory)
    activity = await _activity(activity_session_factory)
    assert len(rows) == len(MessageType)
    assert activity["latest_message_id"] == max(message_id for message_id, _ in rows)
    assert activity["last_message_at"] == max(created_at for _, created_at in rows)
    assert activity["last_read_message_id"] == LAST_READ_MESSAGE_ID
    assert activity["last_read_at"] == LAST_READ_AT


@pytest.mark.asyncio
async def test_activity_projection_orders_id_and_created_at_independently(activity_session_factory):
    base = datetime(2026, 2, 1, 0, 0, 0)
    messages = (
        (20, base.replace(minute=20)),
        (10, base.replace(minute=30)),
        (30, base.replace(minute=10)),
        (25, base.replace(minute=5)),
    )

    async with activity_session_factory() as db:
        for message_id, created_at in messages:
            await message_crud.create(
                db,
                obj_in=_message_data(message_id=message_id, created_at=created_at),
            )

    activity = await _activity(activity_session_factory)
    assert activity["latest_message_id"] == 30
    assert activity["last_message_at"] == base.replace(minute=30)

    async with activity_session_factory() as db:
        await db.execute(
            update(ChatSession)
            .where(ChatSession.session_id == SESSION_ID, ChatSession.uid == UID)
            .values(
                title="updated title",
                enable_markdown=True,
                goal_mode=True,
                max_turns=3,
                show_tool_calls=False,
                show_reasoning=False,
                llm_request_metadata={"source": "activity-test"},
            )
        )
        await db.commit()

    assert await _activity(activity_session_factory) == activity


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["create", "guidance", "idempotent"])
async def test_commit_false_activity_rolls_back_with_outer_transaction(activity_session_factory, entrypoint: str):
    async with activity_session_factory() as db:
        outer_transaction = await db.begin()
        try:
            if entrypoint == "create":
                message = await message_crud.create(
                    db,
                    obj_in=MessageCreate(
                        session_id=SESSION_ID,
                        uid=UID,
                        role=MessageRole.USER,
                        type=MessageType.TEXT,
                        content="rollback create",
                        profile_id=1,
                    ),
                    commit=False,
                )
            elif entrypoint == "guidance":
                message = await message_crud.create_guidance(
                    db,
                    session_id=SESSION_ID,
                    uid=UID,
                    profile_id=1,
                    content="rollback guidance",
                    commit=False,
                )
            else:
                message = await message_crud.create_idempotent(
                    db,
                    obj_in=_message_data(content="rollback idempotent"),
                    dedupe_key="rollback-idempotent",
                    commit=False,
                )

            result = await db.execute(
                select(Message.id, Message.created_at).where(
                    Message.session_id == SESSION_ID,
                    Message.uid == UID,
                )
            )
            rows = result.all()
            activity_before_rollback = await _query_activity(db)
            assert len(rows) == 1
            assert rows[0][0] == message.id
            assert activity_before_rollback["latest_message_id"] == rows[0][0]
            assert activity_before_rollback["last_message_at"] == rows[0][1]
        finally:
            if outer_transaction.is_active:
                await outer_transaction.rollback()

    assert await _message_rows(activity_session_factory) == []
    assert await _activity(activity_session_factory) == {
        "latest_message_id": None,
        "last_message_at": None,
        "last_read_message_id": LAST_READ_MESSAGE_ID,
        "last_read_at": LAST_READ_AT,
    }


@pytest.mark.asyncio
async def test_duplicate_dedupe_key_preserves_later_activity(activity_session_factory):
    first_time = datetime(2026, 3, 1, 0, 0, 0)
    later_time = datetime(2026, 3, 1, 1, 0, 0)
    duplicate_time = datetime(2026, 3, 1, 2, 0, 0)

    async with activity_session_factory() as db:
        first = await message_crud.create_idempotent(
            db,
            obj_in=_message_data(message_id=10, created_at=first_time),
            dedupe_key="same-dedupe-key",
        )
        await message_crud.create(
            db,
            obj_in=_message_data(message_id=20, created_at=later_time),
        )
        activity_before_duplicate = await _query_activity(db)
        duplicate = await message_crud.create_idempotent(
            db,
            obj_in=_message_data(message_id=999, created_at=duplicate_time),
            dedupe_key="same-dedupe-key",
        )

    assert duplicate.id == first.id
    assert await _activity(activity_session_factory) == activity_before_duplicate
    assert await _message_rows(activity_session_factory) == [(10, first_time), (20, later_time)]


@pytest.mark.asyncio
async def test_owner_isolation_and_invalid_owner_do_not_update_activity(activity_session_factory):
    other_session_id = "activity-projection-other-session"
    other_uid = "activity-projection-other-user"
    first_time = datetime(2026, 4, 1, 0, 0, 0)
    second_time = datetime(2026, 4, 1, 1, 0, 0)

    async with activity_session_factory() as db:
        db.add(ChatSession(session_id=other_session_id, uid=other_uid))
        await db.commit()
        await message_crud.create(
            db,
            obj_in=_message_data(message_id=101, created_at=first_time),
        )
        await message_crud.create(
            db,
            obj_in=_message_data(
                session_id=other_session_id,
                uid=other_uid,
                message_id=202,
                created_at=second_time,
            ),
        )
        first_activity = await _query_activity(db)
        second_activity = await _query_activity(db, session_id=other_session_id, uid=other_uid)

        with pytest.raises(IntegrityError):
            await message_crud.create(
                db,
                obj_in=_message_data(
                    uid="wrong-owner",
                    message_id=303,
                    created_at=datetime(2026, 4, 1, 2, 0, 0),
                ),
            )
        await db.rollback()

    assert await _message_rows(activity_session_factory) == [(101, first_time)]
    assert await _message_rows(activity_session_factory, session_id=other_session_id, uid=other_uid) == [(202, second_time)]
    assert await _activity(activity_session_factory) == first_activity
    assert await _activity(activity_session_factory, session_id=other_session_id, uid=other_uid) == second_activity


@pytest.mark.asyncio
async def test_concurrent_writers_keep_maximum_id_and_time(activity_session_factory):
    barrier = asyncio.Barrier(2)
    writes = (
        _message_data(message_id=800, created_at=datetime(2026, 5, 1, 0, 0, 0)),
        _message_data(message_id=700, created_at=datetime(2026, 5, 1, 1, 0, 0)),
    )

    async def write_message(data: dict[str, object]) -> None:
        async with activity_session_factory() as db:
            await barrier.wait()
            await message_crud.create(db, obj_in=data)

    await asyncio.gather(*(write_message(data) for data in writes))

    assert await _message_rows(activity_session_factory) == [
        (700, datetime(2026, 5, 1, 1, 0, 0)),
        (800, datetime(2026, 5, 1, 0, 0, 0)),
    ]
    activity = await _activity(activity_session_factory)
    assert activity["latest_message_id"] == 800
    assert activity["last_message_at"] == datetime(2026, 5, 1, 1, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_existing_message", [False, True], ids=["empty", "existing"])
async def test_http_idempotent_submission_rolls_back_persisted_message_when_enqueue_fails(
    activity_session_factory,
    monkeypatch,
    has_existing_message: bool,
):
    profile = Profile(id=1, uid=UID, name="activity projection profile")
    async with activity_session_factory() as db:
        db.add(profile)
        if has_existing_message:
            await message_crud.create(
                db,
                obj_in=_message_data(content="existing message"),
                commit=False,
            )
        await db.commit()

    message_rows_before = await _message_rows(activity_session_factory)
    activity_before = await _activity(activity_session_factory)

    async def fail_enqueue(*args, **kwargs):
        raise RuntimeError("enqueue failed after message persistence")

    with monkeypatch.context() as patch:
        patch.setattr(session_reply_work_item_crud, "enqueue", fail_enqueue)
        async with activity_session_factory() as db:
            manager = SessionReplyQueueManager()
            with pytest.raises(RuntimeError, match="enqueue failed after message persistence"):
                await manager.submit_user_message(
                    db,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile=profile,
                    message="rollback request",
                    attachments=None,
                    source="http",
                    request_id="rollback-request",
                    idempotent_http_request=True,
                )
            await db.rollback()

    assert await _message_rows(activity_session_factory) == message_rows_before
    assert await _activity(activity_session_factory) == activity_before
    async with activity_session_factory() as db:
        result = await db.execute(
            select(SessionReplyWorkItem.id).where(
                SessionReplyWorkItem.session_id == SESSION_ID,
                SessionReplyWorkItem.uid == UID,
            )
        )
        assert result.scalars().all() == []

    async with activity_session_factory() as db:
        retry_profile = await db.get(Profile, 1)
        manager = SessionReplyQueueManager()
        retry_message, retry_work, _retry_status, _retry_events = await manager.submit_user_message(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile=retry_profile,
            message="rollback request",
            attachments=None,
            source="http",
            request_id="rollback-request",
            idempotent_http_request=True,
        )
        duplicate_message, duplicate_work, _duplicate_status, _duplicate_events = await manager.submit_user_message(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile=retry_profile,
            message="rollback request",
            attachments=None,
            source="http",
            request_id="rollback-request",
            idempotent_http_request=True,
        )

    assert duplicate_message.id == retry_message.id
    assert duplicate_work.id == retry_work.id

    final_message_rows = await _message_rows(activity_session_factory)
    assert len(final_message_rows) == len(message_rows_before) + 1
    assert retry_message.id in {message_id for message_id, _ in final_message_rows}

    async with activity_session_factory() as db:
        result = await db.execute(
            select(SessionReplyWorkItem.id, SessionReplyWorkItem.source_id).where(
                SessionReplyWorkItem.session_id == SESSION_ID,
                SessionReplyWorkItem.uid == UID,
            )
        )
        work_rows = result.all()
    assert len(work_rows) == 1
    assert work_rows[0][0] == retry_work.id
    assert work_rows[0][1] == str(retry_message.id)

    final_activity = await _activity(activity_session_factory)
    retry_created_at = next(created_at for message_id, created_at in final_message_rows if message_id == retry_message.id)
    assert retry_message.created_at == retry_created_at.timestamp()
    assert final_activity["latest_message_id"] == retry_message.id
    assert final_activity["last_message_at"] == retry_created_at
    assert final_activity["last_read_message_id"] == activity_before["last_read_message_id"]
    assert final_activity["last_read_at"] == activity_before["last_read_at"]
