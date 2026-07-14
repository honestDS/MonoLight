from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.utils.context_summary.history import iter_persistent_summary_source_units
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot, iter_persistent_summary_rounds
from app.models.message import Message, MessageRole, MessageType


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[Message.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _message(message_id: int, role: MessageRole, *, content: str | None = None) -> Message:
    return Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=role,
        type=MessageType.TEXT,
        content=content or f"message-{message_id}",
        is_processed=True,
    )


@pytest.mark.asyncio
async def test_snapshot_finds_recent_two_rounds_across_backward_pages(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.TOOL),
            _message(8, MessageRole.USER),
            _message(9, MessageRole.ASSISTANT),
            _message(10, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=10,
        frozen_user_message_ids=[10],
        page_size=2,
    )

    assert snapshot.snapshot_before_id == 10
    assert snapshot.snapshot_max_message_id == 9
    assert snapshot.persistent_summary_target_id == 4
    assert snapshot.recent_round_start_ids == (5, 8)
    assert snapshot.frozen_user_message_ids == (10,)
    assert [message.id for message in snapshot.recent_messages] == [5, 6, 7, 8, 9]


@pytest.mark.asyncio
async def test_forward_round_scan_keeps_cross_page_rounds_complete_and_ordered(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.TOOL),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.USER),
            _message(8, MessageRole.ASSISTANT),
            _message(9, MessageRole.USER),
            _message(10, MessageRole.ASSISTANT),
            _message(11, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=11,
        page_size=2,
    )
    rounds = [
        [message.id for message in round_messages]
        async for round_messages in iter_persistent_summary_rounds(
            db_session,
            session_id="session-1",
            uid="user-1",
            snapshot=snapshot,
            page_size=2,
        )
    ]

    assert snapshot.persistent_summary_target_id == 6
    assert rounds == [[1, 2, 3, 4], [5, 6]]
    assert [message_id for round_ids in rounds for message_id in round_ids] == list(range(1, 7))


@pytest.mark.asyncio
async def test_snapshot_boundaries_ignore_new_messages_arriving_after_snapshot(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.USER),
            _message(8, MessageRole.ASSISTANT),
            _message(9, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=9,
        frozen_user_message_ids=[9],
        page_size=3,
    )
    db_session.add_all(
        [
            _message(10, MessageRole.ASSISTANT, content="late response"),
            _message(11, MessageRole.USER, content="late input"),
        ]
    )
    await db_session.commit()

    scanned_ids = [
        message.id
        async for round_messages in iter_persistent_summary_rounds(
            db_session,
            session_id="session-1",
            uid="user-1",
            snapshot=snapshot,
            page_size=2,
        )
        for message in round_messages
    ]

    assert snapshot.snapshot_max_message_id == 8
    assert snapshot.persistent_summary_target_id == 4
    assert [message.id for message in snapshot.recent_messages] == [5, 6, 7, 8]
    assert scanned_ids == [1, 2, 3, 4]
    assert 9 not in scanned_ids
    assert 10 not in scanned_ids
    assert 11 not in scanned_ids


@pytest.mark.asyncio
async def test_frozen_user_ids_restore_snapshot_boundary_when_before_id_is_absent(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(1, MessageRole.USER),
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER),
            _message(4, MessageRole.ASSISTANT),
            _message(5, MessageRole.USER),
            _message(6, MessageRole.ASSISTANT),
            _message(7, MessageRole.USER, content="frozen input part one"),
            _message(8, MessageRole.USER, content="frozen input part two"),
            _message(9, MessageRole.ASSISTANT, content="current work response"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=None,
        frozen_user_message_ids=[7, 8],
        page_size=2,
    )

    assert snapshot.snapshot_before_id == 7
    assert snapshot.snapshot_max_message_id == 6
    assert snapshot.frozen_user_message_ids == (7, 8)
    assert all(message.id < 7 for message in snapshot.recent_messages)
    assert snapshot.persistent_summary_target_id == 2


@pytest.mark.asyncio
async def test_model_excluded_user_message_extends_source_coverage_without_exposing_content(db_session: AsyncSession):
    covered_user_content = "必须逐字保留且不得交给总结模型的用户原文"
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content=covered_user_content),
            _message(2, MessageRole.ASSISTANT, content="并行工具调用"),
            _message(3, MessageRole.TOOL, content="工具结果一"),
            _message(4, MessageRole.TOOL, content="工具结果二"),
        ]
    )
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=None,
        target_message_id=4,
        model_excluded_message_ids=[1],
    )
    units = [
        unit
        async for unit in iter_persistent_summary_source_units(
            db_session,
            session_id="session-1",
            uid="user-1",
            snapshot=snapshot,
            max_unit_tokens=10_000,
        )
    ]

    assert len(units) == 1
    assert units[0].message_start_id == 1
    assert units[0].message_end_id == 4
    assert covered_user_content not in units[0].content
    assert "并行工具调用" in units[0].content
    assert "工具结果一" in units[0].content
    assert "工具结果二" in units[0].content


@pytest.mark.asyncio
async def test_snapshot_scan_processes_more_than_five_thousand_messages(db_session: AsyncSession):
    history_message_count = 5006
    db_session.add_all(
        [
            _message(
                message_id,
                MessageRole.USER if message_id % 2 == 1 else MessageRole.ASSISTANT,
            )
            for message_id in range(1, history_message_count + 1)
        ]
    )
    db_session.add(_message(history_message_count + 1, MessageRole.USER, content="current input"))
    await db_session.commit()

    snapshot = await build_context_summary_snapshot(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_summary_message_id=None,
        before_id=history_message_count + 1,
        page_size=200,
    )
    scanned_count = 0
    first_id = None
    last_id = None
    async for round_messages in iter_persistent_summary_rounds(
        db_session,
        session_id="session-1",
        uid="user-1",
        snapshot=snapshot,
        page_size=200,
    ):
        for message in round_messages:
            scanned_count += 1
            first_id = message.id if first_id is None else first_id
            last_id = message.id

    assert snapshot.snapshot_max_message_id == history_message_count
    assert snapshot.persistent_summary_target_id == history_message_count - 4
    assert [message.id for message in snapshot.recent_messages] == list(range(history_message_count - 3, history_message_count + 1))
    assert first_id == 1
    assert last_id == history_message_count - 4
    assert scanned_count == history_message_count - 4
