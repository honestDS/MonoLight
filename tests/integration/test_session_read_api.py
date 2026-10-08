import asyncio
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_session_activity_api import (
    _add_work,
    _make_session,
    _persist_message,
    api_context,
    db_session,
)
from test_session_tasks_api import _add_result_work

from app.core.crud.session.session import session_crud
from app.models.message import MessageRole
from app.models.session_reply_work_item import SessionReplyWorkStatus, SessionReplyWorkType

__all__ = ("api_context", "db_session")


def _assert_code(response, code: int) -> dict:
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == code
    return body


async def _reload_session(db, session_id: str):
    session = await session_crud.get_by_session_id(db, session_id)
    assert session is not None
    await db.refresh(session)
    return session


@pytest.mark.asyncio
async def test_read_marks_real_message_and_survives_reopened_session(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session("read-persisted", "user-1", created_at=datetime(2026, 8, 1, 1, 2, 3))
    db.add(session)
    await db.commit()
    message = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        created_at=datetime(2026, 8, 1, 2, 3, 4),
    )
    await db.commit()

    body = _assert_code(
        await client.post(
            "/api/v1/chat/sessions/read",
            json={"session_id": session.session_id, "message_id": message.id},
        ),
        200,
    )
    data = body["data"]
    assert set(data) == {"session_id", "last_read_message_id", "last_read_at"}
    assert data["session_id"] == session.session_id
    assert data["last_read_message_id"] == message.id
    assert data["last_read_at"] is not None

    persisted = await _reload_session(db, session.session_id)
    assert persisted.last_read_message_id == message.id
    assert persisted.last_read_at is not None
    assert data["last_read_at"] == persisted.last_read_at.strftime("%Y-%m-%d %H:%M:%S")
    read_at = persisted.last_read_at

    reopened_factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with reopened_factory() as reopened_db:
        reopened = await _reload_session(reopened_db, session.session_id)
        assert reopened.last_read_message_id == message.id
        assert reopened.last_read_at == read_at


@pytest.mark.asyncio
async def test_repeated_and_older_reads_keep_the_cursor_and_timestamp(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session("read-monotonic", "user-1", created_at=datetime(2026, 8, 2, 1, 2, 3))
    db.add(session)
    await db.commit()
    older = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        created_at=datetime(2026, 8, 2, 2, 3, 4),
        content="older",
    )
    newer = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        created_at=datetime(2026, 8, 2, 3, 4, 5),
        content="newer",
    )
    await db.commit()
    assert newer.id > older.id

    _assert_code(
        await client.post(
            "/api/v1/chat/sessions/read",
            json={"session_id": session.session_id, "message_id": newer.id},
        ),
        200,
    )
    first = await _reload_session(db, session.session_id)
    read_at = first.last_read_at
    assert first.last_read_message_id == newer.id
    assert read_at is not None

    for message_id in (newer.id, older.id):
        body = _assert_code(
            await client.post(
                "/api/v1/chat/sessions/read",
                json={"session_id": session.session_id, "message_id": message_id},
            ),
            200,
        )
        assert body["data"]["last_read_message_id"] == newer.id
        current = await _reload_session(db, session.session_id)
        assert current.last_read_message_id == newer.id
        assert current.last_read_at == read_at


@pytest.mark.asyncio
async def test_fixed_old_cursor_does_not_consume_a_new_result(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session("read-new-result", "user-1", created_at=datetime(2026, 8, 3, 1, 2, 3))
    db.add(session)
    await db.commit()
    _old_work, old_result = await _add_result_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=1,
        created_at=datetime(2026, 8, 3, 2, 3, 4),
        content="old result",
    )
    _assert_code(
        await client.post(
            "/api/v1/chat/sessions/read",
            json={"session_id": session.session_id, "message_id": old_result.id},
        ),
        200,
    )

    _new_work, new_result = await _add_result_work(
        db,
        session_id=session.session_id,
        uid=session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=2,
        created_at=datetime(2026, 8, 3, 3, 4, 5),
        content="new result",
    )
    assert new_result.id > old_result.id
    rows = (await client.get("/api/v1/chat/sessions/activity")).json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {
        "session_id",
        "title",
        "source",
        "latest_message_id",
        "last_active",
        "last_read_message_id",
        "last_read_at",
        "is_running",
        "completed_message_id",
        "completed_status",
        "has_unread_result",
        "is_owned",
        "is_loading",
        "is_reply_running",
    }
    assert row["session_id"] == session.session_id
    assert row["title"] is None
    assert row["source"] == "http"
    assert row["latest_message_id"] == new_result.id
    assert row["last_active"] == "2026-08-03 03:04:05"
    assert row["last_read_message_id"] == old_result.id
    assert row["last_read_at"] is not None
    assert row["is_running"] is False
    assert row["completed_message_id"] == new_result.id
    assert row["completed_status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert row["has_unread_result"] is True

    stale_body = _assert_code(
        await client.post(
            "/api/v1/chat/sessions/read",
            json={"session_id": session.session_id, "message_id": old_result.id},
        ),
        200,
    )
    assert stale_body["data"]["last_read_message_id"] == old_result.id
    after = (await client.get("/api/v1/chat/sessions/activity")).json()["data"]
    assert after[0]["completed_message_id"] == new_result.id
    assert after[0]["last_read_message_id"] == old_result.id
    assert after[0]["has_unread_result"] is True


@pytest.mark.asyncio
async def test_tasks_converge_after_read_but_running_work_remains(api_context) -> None:
    db, _auth_state, client = api_context
    completed_session = _make_session("read-completed", "user-1", created_at=datetime(2026, 8, 4, 1, 2, 3))
    running_session = _make_session("read-running", "user-1", created_at=datetime(2026, 8, 4, 1, 2, 4))
    db.add_all([completed_session, running_session])
    await db.commit()
    _completed_work, completed_result = await _add_result_work(
        db,
        session_id=completed_session.session_id,
        uid=completed_session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=1,
        created_at=datetime(2026, 8, 4, 2, 3, 4),
    )
    _running_result_work, running_result = await _add_result_work(
        db,
        session_id=running_session.session_id,
        uid=running_session.uid,
        status=SessionReplyWorkStatus.SUCCEEDED,
        sequence_no=1,
        created_at=datetime(2026, 8, 4, 2, 3, 5),
    )
    await _add_work(
        db,
        session_id=running_session.session_id,
        uid=running_session.uid,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        status=SessionReplyWorkStatus.RUNNING,
        sequence_no=2,
    )

    initial = {row["session_id"]: row for row in (await client.get("/api/v1/chat/sessions/activity")).json()["data"]}
    assert initial[completed_session.session_id]["has_unread_result"] is True
    assert initial[running_session.session_id]["is_running"] is True
    assert initial[running_session.session_id]["has_unread_result"] is True

    for read_session, result in ((completed_session, completed_result), (running_session, running_result)):
        _assert_code(
            await client.post(
                "/api/v1/chat/sessions/read",
                json={"session_id": read_session.session_id, "message_id": result.id},
            ),
            200,
        )

    rows = {row["session_id"]: row for row in (await client.get("/api/v1/chat/sessions/activity")).json()["data"]}
    assert rows[completed_session.session_id]["has_unread_result"] is False
    assert rows[completed_session.session_id]["is_running"] is False
    assert rows[completed_session.session_id]["last_read_message_id"] == completed_result.id
    assert rows[completed_session.session_id]["last_read_at"] is not None
    assert rows[running_session.session_id]["is_running"] is True
    assert rows[running_session.session_id]["completed_message_id"] == running_result.id
    assert rows[running_session.session_id]["has_unread_result"] is False
    assert rows[running_session.session_id]["last_read_message_id"] == running_result.id
    assert rows[running_session.session_id]["last_read_at"] is not None


@pytest.mark.asyncio
async def test_external_source_session_can_mark_its_message_read(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session(
        "read-external",
        "user-1",
        created_at=datetime(2026, 8, 5, 1, 2, 3),
        source="external",
    )
    db.add(session)
    await db.commit()
    message = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        created_at=datetime(2026, 8, 5, 2, 3, 4),
    )
    await db.commit()

    body = _assert_code(
        await client.post(
            "/api/v1/chat/sessions/read",
            json={"session_id": session.session_id, "message_id": message.id},
        ),
        200,
    )
    assert body["data"]["session_id"] == session.session_id
    assert (await _reload_session(db, session.session_id)).last_read_message_id == message.id


@pytest.mark.asyncio
async def test_admin_cannot_mark_another_users_session_and_unknown_is_404(api_context) -> None:
    db, auth_state, client = api_context
    owned = _make_session("read-owned", "user-1", created_at=datetime(2026, 8, 6, 1, 2, 3))
    other = _make_session("read-other", "user-2", created_at=datetime(2026, 8, 6, 1, 2, 4))
    db.add_all([owned, other])
    await db.commit()
    other_message = await _persist_message(
        db,
        session_id=other.session_id,
        uid=other.uid,
        created_at=datetime(2026, 8, 6, 2, 3, 4),
    )
    await db.commit()
    auth_state["is_superuser"] = True

    other_response = await client.post(
        "/api/v1/chat/sessions/read",
        json={"session_id": other.session_id, "message_id": other_message.id},
    )
    _assert_code(other_response, 403)
    other_state = await _reload_session(db, other.session_id)
    assert other_state.last_read_message_id is None
    assert other_state.last_read_at is None

    unknown_response = await client.post(
        "/api/v1/chat/sessions/read",
        json={"session_id": "read-missing", "message_id": other_message.id},
    )
    _assert_code(unknown_response, 404)
    assert (await _reload_session(db, owned.session_id)).last_read_message_id is None


@pytest.mark.asyncio
async def test_invalid_message_ids_are_rejected_without_changing_cursor(api_context) -> None:
    db, _auth_state, client = api_context
    owned = _make_session("read-invalid-owned", "user-1", created_at=datetime(2026, 8, 7, 1, 2, 3))
    another_owned = _make_session("read-invalid-another", "user-1", created_at=datetime(2026, 8, 7, 1, 2, 4))
    other = _make_session("read-invalid-other", "user-2", created_at=datetime(2026, 8, 7, 1, 2, 5))
    db.add_all([owned, another_owned, other])
    await db.commit()
    own_other_message = await _persist_message(
        db,
        session_id=another_owned.session_id,
        uid=another_owned.uid,
        created_at=datetime(2026, 8, 7, 2, 3, 4),
    )
    foreign_message = await _persist_message(
        db,
        session_id=other.session_id,
        uid=other.uid,
        created_at=datetime(2026, 8, 7, 2, 3, 5),
    )
    await db.commit()
    invalid_message_id = max(own_other_message.id, foreign_message.id) + 1000

    for message_id in (invalid_message_id, own_other_message.id, foreign_message.id):
        body = _assert_code(
            await client.post(
                "/api/v1/chat/sessions/read",
                json={"session_id": owned.session_id, "message_id": message_id},
            ),
            400,
        )
        assert body["data"] is None
        state = await _reload_session(db, owned.session_id)
        assert state.last_read_message_id is None
        assert state.last_read_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"session_id": "read-strict", "message_id": 0},
        {"session_id": "read-strict", "message_id": -1},
        {"session_id": "read-strict", "message_id": 1.5},
        {"session_id": "read-strict", "message_id": True},
        {"session_id": "read-strict", "message_id": "1"},
        {"session_id": "read-strict"},
        {"session_id": "read-strict", "message_id": 1, "extra": "forbidden"},
    ),
    ids=("zero", "negative", "decimal", "bool", "numeric-string", "missing", "extra"),
)
async def test_read_body_requires_strict_positive_message_id(api_context, payload) -> None:
    db, _auth_state, client = api_context
    session = _make_session("read-strict", "user-1", created_at=datetime(2026, 8, 8, 1, 2, 3))
    db.add(session)
    await db.commit()

    response = await client.post("/api/v1/chat/sessions/read", json=payload)
    assert response.status_code == 422
    state = await _reload_session(db, session.session_id)
    assert state.last_read_message_id is None
    assert state.last_read_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", ("", "x" * 101), ids=("empty", "too-long"))
async def test_read_body_requires_valid_session_id(api_context, session_id) -> None:
    db, _auth_state, client = api_context
    session = _make_session("read-valid-session", "user-1", created_at=datetime(2026, 8, 9, 1, 2, 3))
    db.add(session)
    await db.commit()

    response = await client.post(
        "/api/v1/chat/sessions/read",
        json={"session_id": session_id, "message_id": 1},
    )
    assert response.status_code == 422
    state = await _reload_session(db, session.session_id)
    assert state.last_read_message_id is None


@pytest.mark.asyncio
async def test_message_persist_does_not_automatically_mark_session_read(api_context) -> None:
    db, _auth_state, client = api_context
    session = _make_session("read-write-projection", "user-1", created_at=datetime(2026, 8, 10, 1, 2, 3))
    db.add(session)
    await db.commit()
    message = await _persist_message(
        db,
        session_id=session.session_id,
        uid=session.uid,
        created_at=datetime(2026, 8, 10, 2, 3, 4),
        role=MessageRole.USER,
    )
    await db.commit()

    state = await _reload_session(db, session.session_id)
    assert state.latest_message_id == message.id
    assert state.last_read_message_id is None
    assert state.last_read_at is None
    activity_response = await client.get("/api/v1/chat/sessions/activity")
    assert activity_response.status_code == 200
    rows = activity_response.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == session.session_id
    assert row["is_running"] is False
    assert row["completed_message_id"] is None
    assert row["completed_status"] is None
    assert row["has_unread_result"] is False
    assert row["last_read_message_id"] is None
    assert row["last_read_at"] is None


@pytest.mark.asyncio
async def test_mark_read_concurrent_sessions_keep_max_and_old_timestamp(database_factory) -> None:
    async with database_factory(foreign_keys=True, wal=True) as session_factory:
        session = _make_session("crud-concurrent", "user-1", created_at=datetime(2026, 8, 11, 1, 2, 3))
        async with session_factory() as setup_db:
            setup_db.add(session)
            await setup_db.commit()
            old_message = await _persist_message(
                setup_db,
                session_id=session.session_id,
                uid=session.uid,
                created_at=datetime(2026, 8, 11, 2, 3, 4),
            )
            new_message = await _persist_message(
                setup_db,
                session_id=session.session_id,
                uid=session.uid,
                created_at=datetime(2026, 8, 11, 3, 4, 5),
            )
            await setup_db.commit()
        assert new_message.id > old_message.id

        async def mark(message_id: int) -> bool:
            async with session_factory() as db:
                return await session_crud.mark_read(
                    db,
                    uid=session.uid,
                    session_id=session.session_id,
                    message_id=message_id,
                    commit=True,
                )

        results = await asyncio.gather(mark(new_message.id), mark(old_message.id))
        assert any(results)
        async with session_factory() as db:
            current = await _reload_session(db, session.session_id)
            assert current.last_read_message_id == new_message.id
            assert current.last_read_at is not None
            read_at = current.last_read_at

        async with session_factory() as db:
            assert (
                await session_crud.mark_read(
                    db,
                    uid=session.uid,
                    session_id=session.session_id,
                    message_id=old_message.id,
                    commit=True,
                )
                is False
            )
            assert (
                await session_crud.mark_read(
                    db,
                    uid=session.uid,
                    session_id=session.session_id,
                    message_id=new_message.id,
                    commit=True,
                )
                is False
            )
        async with session_factory() as db:
            current = await _reload_session(db, session.session_id)
            assert current.last_read_message_id == new_message.id
            assert current.last_read_at == read_at


@pytest.mark.asyncio
async def test_mark_read_rollback_and_invalid_ids_do_not_persist(db_session) -> None:
    owner = _make_session("crud-rollback-owner", "user-1", created_at=datetime(2026, 8, 12, 1, 2, 3))
    other = _make_session("crud-rollback-other", "user-2", created_at=datetime(2026, 8, 12, 1, 2, 4))
    db_session.add_all([owner, other])
    await db_session.commit()
    owner_message = await _persist_message(
        db_session,
        session_id=owner.session_id,
        uid=owner.uid,
        created_at=datetime(2026, 8, 12, 2, 3, 4),
    )
    other_message = await _persist_message(
        db_session,
        session_id=other.session_id,
        uid=other.uid,
        created_at=datetime(2026, 8, 12, 2, 3, 5),
    )
    await db_session.commit()
    owner_session_id = owner.session_id
    owner_uid = owner.uid
    owner_message_id = owner_message.id
    other_message_id = other_message.id

    assert (
        await session_crud.mark_read(
            db_session,
            uid=owner_uid,
            session_id=owner_session_id,
            message_id=owner_message_id,
            commit=False,
        )
        is True
    )
    await db_session.rollback()
    rolled_back = await _reload_session(db_session, owner_session_id)
    assert rolled_back.last_read_message_id is None
    assert rolled_back.last_read_at is None

    invalid_ids = (
        (owner_session_id, max(owner_message_id, other_message_id) + 1000),
        (owner_session_id, other_message_id),
        ("crud-missing", owner_message_id),
    )
    for session_id, message_id in invalid_ids:
        assert (
            await session_crud.mark_read(
                db_session,
                uid=owner_uid,
                session_id=session_id,
                message_id=message_id,
                commit=True,
            )
            is False
        )
    unchanged = await _reload_session(db_session, owner_session_id)
    assert unchanged.last_read_message_id is None
    assert unchanged.last_read_at is None
