import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem, session_reply_work_item_crud
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.models.message import InternalMessage, Message, MessageRole
from app.models.session_reply_work_item import (
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.session_reply_queue_fixture import (
    concurrent_session_factory as _concurrent_session_factory,
)
from tests.integration.session_reply_queue_fixture import (
    db_session as _db_session,
)
from tests.integration.session_reply_queue_test_support import (
    AsyncBarrier,
    add_message,
    enqueue,
)

db_session = _db_session
concurrent_session_factory = _concurrent_session_factory


@pytest.mark.asyncio
async def test_has_nonterminal_predecessor_detects_prior_same_session_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    first = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    await db_session.commit()

    assert await crud.has_nonterminal_predecessor(db_session, first) is False

    second = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:2",
    )
    await db_session.commit()

    assert await crud.has_nonterminal_predecessor(db_session, second) is True

    first.status = SessionReplyWorkStatus.SUCCEEDED
    db_session.add(first)
    await db_session.commit()

    assert await crud.has_nonterminal_predecessor(db_session, second) is False


@pytest.mark.asyncio
async def test_foreground_freeze_merges_only_until_background_work(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "A")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=9, dedupe_key="background-task-summary:9")
    await add_message(db_session, 2, "B")
    later = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    content, attachments, message_ids = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    assert content == "A"
    assert attachments == []
    assert message_ids == [1]
    await db_session.refresh(later)
    assert later.status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_foreground_freeze_merges_contiguous_work_and_is_stable(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "B")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await add_message(db_session, 2, "C")
    merged = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    first.execution_state = {"request_ids": ["request-1", "request-shared"]}
    merged.execution_state = {"request_ids": ["request-shared", "request-2"]}
    db_session.add(first)
    db_session.add(merged)
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    first_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await add_message(db_session, 3, "D")
    await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
    await db_session.commit()
    await db_session.refresh(first)
    second_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    assert first_result == ("B\nC", [], [1, 2])
    assert second_result == first_result
    await db_session.refresh(merged)
    await db_session.refresh(first)
    assert first.execution_state["request_ids"] == ["request-1", "request-shared", "request-2"]
    assert merged.status == SessionReplyWorkStatus.MERGED
    assert merged.merged_into_id == first.id
    processed = list((await db_session.execute(select(Message).where(Message.id.in_([1, 2])))).scalars().all())
    assert all(message.is_processed for message in processed)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_stage", ["freeze", "absorb"])
async def test_foreground_input_stops_at_resumed_frozen_work(db_session: AsyncSession, input_stage: str):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    old_worker_id = "worker-old"
    current_worker_id = "worker-current"

    old_message = await add_message(db_session, 165, "old frozen input")
    old_work = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=old_message.id,
        dedupe_key="foreground-message:165",
    )
    old_work.execution_state = {**old_work.execution_state, "request_ids": ["request-old"]}
    db_session.add(old_work)
    await db_session.commit()

    claimed_old_work = await crud.claim_next(db_session, worker_id=old_worker_id, lease_seconds=300)
    assert claimed_old_work is not None
    assert claimed_old_work.id == old_work.id
    old_work = claimed_old_work
    old_frozen_result = await manager.freeze_foreground_input(
        db_session,
        work=old_work,
        worker_id=old_worker_id,
    )
    assert old_frozen_result == ("old frozen input", [], [old_message.id])
    await db_session.refresh(old_work)
    await db_session.refresh(old_message)
    assert old_work.input_message_ids == [old_message.id]
    assert old_message.is_processed is True

    old_checkpoint_message = InternalMessage(role=MessageRole.USER, content="old resume checkpoint")
    old_checkpoint = {
        "messages": [old_checkpoint_message.model_dump(mode="json", exclude_none=True)],
    }
    assert old_work.id is not None
    await crud.save_checkpoint(db_session, old_work.id, old_checkpoint)
    await db_session.commit()
    old_execution_state = dict(old_work.execution_state)

    old_work.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    old_work.locked_by = None
    old_work.lock_until = None
    db_session.add(old_work)
    await db_session.commit()

    current_message = await add_message(db_session, 169, "current user input")
    current_work = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=current_message.id,
        dedupe_key="foreground-message:169",
    )
    current_work.execution_state = {**current_work.execution_state, "request_ids": ["request-current"]}
    db_session.add(current_work)
    await db_session.commit()

    if input_stage == "absorb":
        claimed_current_work = await crud.claim_next(db_session, worker_id=current_worker_id, lease_seconds=300)
        assert claimed_current_work is not None
        assert claimed_current_work.id == current_work.id
        current_work = claimed_current_work
        current_frozen_result = await manager.freeze_foreground_input(
            db_session,
            work=current_work,
            worker_id=current_worker_id,
        )
        assert current_frozen_result == ("current user input", [], [current_message.id])

    before_boundary_message = await add_message(db_session, 170, "before resumed work")
    before_boundary_work = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=before_boundary_message.id,
        dedupe_key="foreground-message:170",
    )
    before_boundary_work.execution_state = {**before_boundary_work.execution_state, "request_ids": ["request-before"]}
    db_session.add(before_boundary_work)

    resumed_execution_state = {**old_execution_state, "external_resume_pending": True}
    assert await external_work_crud.wake_waiting(
        db_session,
        work=old_work,
        execution_state=resumed_execution_state,
        commit=False,
    )

    after_boundary_message = await add_message(db_session, 171, "after resumed work")
    after_boundary_work = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=after_boundary_message.id,
        dedupe_key="foreground-message:171",
    )
    after_boundary_work.execution_state = {**after_boundary_work.execution_state, "request_ids": ["request-after"]}
    db_session.add(after_boundary_work)
    await db_session.commit()
    await db_session.refresh(old_work)

    if input_stage == "freeze":
        claimed_current_work = await crud.claim_next(db_session, worker_id=current_worker_id, lease_seconds=300)
        assert claimed_current_work is not None
        assert claimed_current_work.id == current_work.id
        current_work = claimed_current_work
        current_input_result = await manager.freeze_foreground_input(
            db_session,
            work=current_work,
            worker_id=current_worker_id,
        )
        assert current_input_result == ("current user input\nbefore resumed work", [], [current_message.id, before_boundary_message.id])
    else:
        current_input_batch = await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=current_work.id,
            worker_id=current_worker_id,
        )
        assert current_input_batch is not None
        assert current_input_batch[0].content == "before resumed work"
        assert current_input_batch.source_message_ids == (before_boundary_message.id,)

    await db_session.refresh(current_work)
    await db_session.refresh(before_boundary_work)
    await db_session.refresh(old_work)
    await db_session.refresh(after_boundary_work)
    await db_session.refresh(old_message)
    await db_session.refresh(current_message)
    await db_session.refresh(before_boundary_message)
    await db_session.refresh(after_boundary_message)

    assert current_work.input_message_ids == [current_message.id, before_boundary_message.id]
    assert current_work.execution_state["request_ids"] == ["request-current", "request-before"]
    assert before_boundary_work.status == SessionReplyWorkStatus.MERGED
    assert before_boundary_work.merged_into_id == current_work.id
    assert old_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert old_work.input_message_ids == [old_message.id]
    assert old_work.merged_into_id is None
    assert old_work.execution_state == resumed_execution_state
    assert old_work.execution_state["request_ids"] == ["request-old"]
    assert old_work.execution_state["external_resume_pending"] is True
    assert await crud.get_checkpoint(db_session, old_work.id) == old_checkpoint
    assert after_boundary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert after_boundary_work.merged_into_id is None
    assert old_message.is_processed is True
    assert current_message.is_processed is True
    assert before_boundary_message.is_processed is True
    assert after_boundary_message.is_processed is False

    assert await crud.mark_terminal(
        db_session,
        work_id=current_work.id,
        worker_id=current_worker_id,
        status=SessionReplyWorkStatus.SUCCEEDED,
    )
    resumed_work = await crud.claim_next(db_session, worker_id="worker-resume", lease_seconds=300)
    assert resumed_work is not None
    assert resumed_work.id == old_work.id
    resumed_result = await manager.freeze_foreground_input(
        db_session,
        work=resumed_work,
        worker_id="worker-resume",
    )
    assert resumed_result == ("old frozen input", [], [old_message.id])
    await db_session.refresh(resumed_work)
    assert resumed_work.input_message_ids == [old_message.id]
    assert resumed_work.execution_state == resumed_execution_state
    assert await crud.get_checkpoint(db_session, resumed_work.id) == old_checkpoint
    await db_session.refresh(after_boundary_work)
    await db_session.refresh(after_boundary_message)
    assert after_boundary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert after_boundary_message.is_processed is False


@pytest.mark.asyncio
async def test_foreground_freeze_preserves_work_order_with_non_monotonic_message_ids(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 30, "first")
    first_message = await db_session.get(Message, 30)
    first_message.attachments = ["attachment-first", "attachment-shared"]
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=30, dedupe_key="foreground-message:30")
    await add_message(db_session, 10, "second")
    second_message = await db_session.get(Message, 10)
    second_message.attachments = ["attachment-second", "attachment-shared"]
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=10, dedupe_key="foreground-message:10")
    await add_message(db_session, 20, "third")
    third_message = await db_session.get(Message, 20)
    third_message.attachments = ["attachment-third"]
    third = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=20, dedupe_key="foreground-message:20")
    db_session.add_all([first, second, third])
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    first_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await db_session.refresh(first)
    second_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    expected_result = (
        "first\nsecond\nthird",
        ["attachment-first", "attachment-shared", "attachment-second", "attachment-third"],
        [30, 10, 20],
    )
    assert first_result == expected_result
    assert second_result == expected_result
    assert first.input_message_ids == [30, 10, 20]
    await db_session.refresh(second)
    await db_session.refresh(third)
    assert second.status == SessionReplyWorkStatus.MERGED
    assert third.status == SessionReplyWorkStatus.MERGED


@pytest.mark.asyncio
async def test_foreground_freeze_rolls_back_when_candidate_merge_is_incomplete(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "first")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    first.execution_state = {"request_ids": ["request-first"]}
    await add_message(db_session, 2, "second")
    candidate = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:2",
    )
    candidate.execution_state = {"request_ids": ["request-second"]}
    db_session.add_all([first, candidate])
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    initial_input_message_ids = list(first.input_message_ids or [])
    original_merge_ready_foreground = session_reply_work_item_crud.merge_ready_foreground

    async def merge_ready_foreground(*args, **kwargs):
        updated_work_ids = await original_merge_ready_foreground(*args, **kwargs)
        return updated_work_ids[:-1]

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", merge_ready_foreground)

    with pytest.raises(RuntimeError):
        await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await db_session.refresh(first)
    await db_session.refresh(candidate)
    messages = list((await db_session.execute(select(Message).where(Message.id.in_([1, 2])))).scalars().all())
    assert list(first.input_message_ids or []) == initial_input_message_ids
    assert first.execution_state == {"request_ids": ["request-first"]}
    assert candidate.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert candidate.merged_into_id is None
    assert all(message.is_processed is False for message in messages)

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", original_merge_ready_foreground)
    first_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")
    await db_session.refresh(first)
    second_result = await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    assert first_result == ("first\nsecond", [], [1, 2])
    assert second_result == first_result
    await db_session.refresh(candidate)
    messages = list((await db_session.execute(select(Message).where(Message.id.in_([1, 2])))).scalars().all())
    assert first.input_message_ids == [1, 2]
    assert first.execution_state["request_ids"] == ["request-first", "request-second"]
    assert candidate.status == SessionReplyWorkStatus.MERGED
    assert candidate.merged_into_id == first.id
    assert all(message.is_processed is True for message in messages)


@pytest.mark.asyncio
async def test_running_confirmed_tool_execution_absorbs_later_foreground_message(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    confirmed = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_id=42,
        dedupe_key="confirmed-audit:42",
    )
    confirmed.execution_state = {"request_ids": ["request-confirmed", "request-shared"]}
    db_session.add(confirmed)
    await db_session.commit()

    confirmed.status = SessionReplyWorkStatus.RUNNING
    confirmed.locked_by = "worker-1"
    db_session.add(confirmed)
    await db_session.commit()

    await add_message(db_session, 30, "follow-up one")
    first_message = await db_session.get(Message, 30)
    first_message.attachments = ["attachment-one", "attachment-shared"]
    first_foreground = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=30,
        dedupe_key="foreground-message:30",
    )
    await add_message(db_session, 10, "follow-up two")
    second_message = await db_session.get(Message, 10)
    second_message.attachments = ["attachment-two", "attachment-shared"]
    second_foreground = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=10,
        dedupe_key="foreground-message:10",
    )
    await add_message(db_session, 20, "follow-up three")
    third_message = await db_session.get(Message, 20)
    third_message.attachments = ["attachment-three"]
    third_foreground = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=20,
        dedupe_key="foreground-message:20",
    )
    first_foreground.execution_state = {"request_ids": ["request-shared", "request-first"]}
    second_foreground.execution_state = {"request_ids": ["request-second", "request-shared"]}
    third_foreground.execution_state = {"request_ids": ["request-third", "request-first"]}
    db_session.add_all([first_foreground, second_foreground, third_foreground])
    await db_session.commit()

    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=confirmed.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "follow-up one\nfollow-up two\nfollow-up three"
    assert additional_messages[0].attachments == ["attachment-one", "attachment-shared", "attachment-two", "attachment-three"]
    assert additional_messages[0].id == 20
    assert additional_messages.source_message_ids == (30, 10, 20)
    assert additional_messages.summary_boundary_message_id == 10
    assert additional_messages.latest_message_id == 30
    await db_session.refresh(confirmed)
    await db_session.refresh(first_foreground)
    await db_session.refresh(second_foreground)
    await db_session.refresh(third_foreground)
    assert confirmed.input_message_ids == [30, 10, 20]
    assert confirmed.execution_state["request_ids"] == ["request-confirmed", "request-shared", "request-first", "request-second", "request-third"]
    for foreground in (first_foreground, second_foreground, third_foreground):
        assert foreground.status == SessionReplyWorkStatus.MERGED
        assert foreground.merged_into_id == confirmed.id
    processed_messages = list((await db_session.execute(select(Message).where(Message.id.in_([30, 10, 20])))).scalars().all())
    assert all(message.is_processed for message in processed_messages)

    assert (
        await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=confirmed.id,
            worker_id="worker-1",
        )
        is None
    )
    await db_session.refresh(confirmed)
    assert confirmed.input_message_ids == [30, 10, 20]
    assert confirmed.execution_state["request_ids"] == ["request-confirmed", "request-shared", "request-first", "request-second", "request-third"]


@pytest.mark.asyncio
async def test_concurrent_absorb_merges_each_work_once_and_preserves_work_order(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    async with concurrent_session_factory() as db:
        root = await enqueue(
            crud,
            db,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=1,
            dedupe_key="foreground-message:1",
        )
        await add_message(db, 30, "B")
        second = await enqueue(
            crud,
            db,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=30,
            dedupe_key="foreground-message:30",
        )
        await add_message(db, 10, "C")
        third = await enqueue(
            crud,
            db,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=10,
            dedupe_key="foreground-message:10",
        )
        root.status = SessionReplyWorkStatus.RUNNING
        root.locked_by = "worker-1"
        root.input_message_ids = [1]
        root.execution_state = {"request_ids": ["request-root", "request-shared"]}
        second.execution_state = {"request_ids": ["request-second", "request-shared"]}
        third.execution_state = {"request_ids": ["request-third", "request-second"]}
        db.add_all([root, second, third])
        await db.commit()

    merge_barrier = AsyncBarrier(2)
    original_merge_ready_foreground = session_reply_work_item_crud.merge_ready_foreground

    async def synchronize_merge(*args, **kwargs):
        await merge_barrier.wait()
        return await original_merge_ready_foreground(*args, **kwargs)

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", synchronize_merge)

    async def absorb_in_session():
        async with concurrent_session_factory() as db:
            return await manager.absorb_contiguous_foreground_messages(
                db,
                work_id=root.id,
                worker_id="worker-1",
            )

    batches = await asyncio.gather(absorb_in_session(), absorb_in_session())

    async with concurrent_session_factory() as db:
        persisted_root = await db.get(SessionReplyWorkItem, root.id)
        persisted_second = await db.get(SessionReplyWorkItem, second.id)
        persisted_third = await db.get(SessionReplyWorkItem, third.id)
        messages = list((await db.execute(select(Message).where(Message.id.in_([30, 10])))).scalars().all())

    successful_batches = [batch for batch in batches if batch is not None]
    assert len(successful_batches) == 1
    assert successful_batches[0].source_message_ids == (30, 10)
    assert persisted_root.input_message_ids == [1, 30, 10]
    assert persisted_root.execution_state["request_ids"] == ["request-root", "request-shared", "request-second", "request-third"]
    assert persisted_second.status == SessionReplyWorkStatus.MERGED
    assert persisted_third.status == SessionReplyWorkStatus.MERGED
    assert persisted_second.merged_into_id == root.id
    assert persisted_third.merged_into_id == root.id
    assert all(message.is_processed for message in messages)


@pytest.mark.asyncio
async def test_absorb_rolls_back_when_work_reaches_terminal_state_before_commit(
    concurrent_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    async with concurrent_session_factory() as db:
        root = await enqueue(
            crud,
            db,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=1,
            dedupe_key="foreground-message:1",
        )
        await add_message(db, 2, "pending input")
        candidate = await enqueue(
            crud,
            db,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=2,
            dedupe_key="foreground-message:2",
        )
        root.status = SessionReplyWorkStatus.RUNNING
        root.locked_by = "worker-1"
        root.input_message_ids = [1]
        db.add(root)
        await db.commit()

    terminal_started = asyncio.Event()
    terminal_finished = asyncio.Event()
    original_merge_ready_foreground = session_reply_work_item_crud.merge_ready_foreground

    async def wait_for_terminal(*args, **kwargs):
        terminal_started.set()
        await terminal_finished.wait()
        return await original_merge_ready_foreground(*args, **kwargs)

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", wait_for_terminal)

    async def absorb():
        async with concurrent_session_factory() as db:
            return await manager.absorb_contiguous_foreground_messages(
                db,
                work_id=root.id,
                worker_id="worker-1",
            )

    async def mark_terminal_after_absorb_reads() -> bool:
        await terminal_started.wait()
        async with concurrent_session_factory() as db:
            updated = await crud.mark_terminal(
                db,
                work_id=root.id,
                worker_id="worker-1",
                status=SessionReplyWorkStatus.SUCCEEDED,
            )
        terminal_finished.set()
        return updated

    absorbed, terminal_updated = await asyncio.gather(absorb(), mark_terminal_after_absorb_reads())

    async with concurrent_session_factory() as db:
        persisted_root = await db.get(SessionReplyWorkItem, root.id)
        persisted_candidate = await db.get(SessionReplyWorkItem, candidate.id)
        candidate_message = await db.get(Message, 2)

    assert terminal_updated is True
    assert absorbed is None
    assert persisted_root.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted_candidate.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert persisted_candidate.merged_into_id is None
    assert candidate_message.is_processed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_mode", ["incomplete", "failure"])
async def test_absorb_contiguous_messages_rolls_back_when_candidate_merge_is_not_complete(
    db_session: AsyncSession,
    monkeypatch,
    merge_mode: str,
):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "first")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    first.input_message_ids = [1]
    first.execution_state = {"request_ids": ["request-first"]}
    db_session.add(first)
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    await add_message(db_session, 2, "second")
    candidate = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:2",
    )
    candidate.execution_state = {"request_ids": ["request-second"]}
    db_session.add(candidate)
    await db_session.commit()

    original_merge_ready_foreground = session_reply_work_item_crud.merge_ready_foreground

    async def merge_ready_foreground(*args, **kwargs):
        if merge_mode == "failure":
            return []
        updated_work_ids = await original_merge_ready_foreground(*args, **kwargs)
        return updated_work_ids[:-1]

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", merge_ready_foreground)

    assert (
        await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=first.id,
            worker_id="worker-1",
        )
        is None
    )

    await db_session.refresh(first)
    await db_session.refresh(candidate)
    candidate_message = await db_session.get(Message, 2)
    assert first.input_message_ids == [1]
    assert first.execution_state == {"request_ids": ["request-first"]}
    assert candidate.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert candidate.merged_into_id is None
    assert candidate_message.is_processed is False

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", original_merge_ready_foreground)
    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=first.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "second"
    assert additional_messages.source_message_ids == (2,)
    await db_session.refresh(first)
    await db_session.refresh(candidate)
    candidate_message = await db_session.get(Message, 2)
    assert first.input_message_ids == [1, 2]
    assert first.execution_state["request_ids"] == ["request-first", "request-second"]
    assert candidate.status == SessionReplyWorkStatus.MERGED
    assert candidate.merged_into_id == first.id
    assert candidate_message.is_processed is True

    assert (
        await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=first.id,
            worker_id="worker-1",
        )
        is None
    )
    await db_session.refresh(first)
    assert first.input_message_ids == [1, 2]
    assert first.execution_state["request_ids"] == ["request-first", "request-second"]


@pytest.mark.asyncio
async def test_absorb_contiguous_messages_rolls_back_when_claim_is_lost(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "first")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    first.input_message_ids = [1]
    first.execution_state = {"request_ids": ["request-first"]}
    db_session.add(first)
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    await add_message(db_session, 2, "second")
    candidate = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=2,
        dedupe_key="foreground-message:2",
    )
    candidate.execution_state = {"request_ids": ["request-second"]}
    db_session.add(candidate)
    await db_session.commit()

    original_update_claimed = session_reply_work_item_crud.update_claimed

    async def lose_claim(*args, **kwargs):
        return False

    monkeypatch.setattr(session_reply_work_item_crud, "update_claimed", lose_claim)

    assert (
        await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=first.id,
            worker_id="worker-1",
        )
        is None
    )

    await db_session.refresh(first)
    await db_session.refresh(candidate)
    candidate_message = await db_session.get(Message, 2)
    assert first.input_message_ids == [1]
    assert first.execution_state == {"request_ids": ["request-first"]}
    assert candidate.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert candidate.merged_into_id is None
    assert candidate_message.is_processed is False

    monkeypatch.setattr(session_reply_work_item_crud, "update_claimed", original_update_claimed)
    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=first.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "second"
    assert additional_messages.source_message_ids == (2,)
    await db_session.refresh(first)
    await db_session.refresh(candidate)
    candidate_message = await db_session.get(Message, 2)
    assert first.input_message_ids == [1, 2]
    assert first.execution_state["request_ids"] == ["request-first", "request-second"]
    assert candidate.status == SessionReplyWorkStatus.MERGED
    assert candidate.merged_into_id == first.id
    assert candidate_message.is_processed is True

    assert (
        await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=first.id,
            worker_id="worker-1",
        )
        is None
    )
    await db_session.refresh(first)
    assert first.input_message_ids == [1, 2]
    assert first.execution_state["request_ids"] == ["request-first", "request-second"]


@pytest.mark.asyncio
async def test_background_task_resume_does_not_absorb_foreground_messages(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()

    background = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.BACKGROUND_TASK_RESUME,
        source_id=9,
        dedupe_key="background-task-resume:9",
    )
    background.status = SessionReplyWorkStatus.RUNNING
    background.locked_by = "worker-1"
    db_session.add(background)

    message = await add_message(db_session, 1, "foreground message")
    foreground = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=1,
        dedupe_key="foreground-message:1",
    )
    await db_session.commit()

    batch = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=background.id,
        worker_id="worker-1",
    )

    assert batch is None
    await db_session.refresh(background)
    await db_session.refresh(foreground)
    await db_session.refresh(message)
    assert background.input_message_ids in (None, [])
    assert foreground.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert foreground.merged_into_id is None
    assert message.is_processed is False


@pytest.mark.asyncio
async def test_running_foreground_work_does_not_absorb_across_background_boundary(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "first")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await add_message(db_session, 2, "before boundary")
    before_boundary = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await enqueue(crud, db_session, work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, source_id=9, dedupe_key="background-task-summary:9")
    await add_message(db_session, 3, "after boundary")
    after_boundary = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
    await db_session.commit()

    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=first.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "before boundary"
    await db_session.refresh(before_boundary)
    await db_session.refresh(after_boundary)
    assert before_boundary.status == SessionReplyWorkStatus.MERGED
    assert before_boundary.merged_into_id == first.id
    assert after_boundary.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert after_boundary.merged_into_id is None
    before_boundary_message = await db_session.get(Message, 2)
    after_boundary_message = await db_session.get(Message, 3)
    assert before_boundary_message.is_processed is True
    assert after_boundary_message.is_processed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["freeze", "absorb"])
@pytest.mark.parametrize("base_actor", ["admin", "legacy-owner"])
async def test_foreground_merge_stops_at_different_actor_boundary(
    db_session: AsyncSession,
    mode: str,
    base_actor: str,
):
    manager = SessionReplyQueueManager()
    base_actor_uid = "admin" if base_actor == "admin" else "user-1"
    base_actor_username = "Admin Stable" if base_actor == "admin" else "Legacy Owner Stable"
    different_actor_uid = "different-actor"
    different_actor_username = "Different Actor Stable"

    async def enqueue_message(content: str, actor_uid: str, actor_username: str):
        return await manager._enqueue_foreground_message(
            db_session,
            uid="user-1",
            session_id="session-1",
            profile=SimpleNamespace(id=1),
            message=content,
            attachments=None,
            source="http",
            actor_uid=actor_uid,
            actor_username=actor_username,
        )

    first_message, first_work = await enqueue_message("first", base_actor_uid, base_actor_username)
    claimed_work = await session_reply_work_item_crud.claim_next(
        db_session,
        worker_id="worker-actor",
        lease_seconds=300,
    )
    assert claimed_work is not None
    assert claimed_work.id == first_work.id
    first_work = claimed_work

    if base_actor == "legacy-owner":
        legacy_execution_state = dict(first_work.execution_state)
        legacy_execution_state.pop("actor_uid", None)
        legacy_execution_state.pop("actor_username", None)
        first_work.execution_state = legacy_execution_state
        db_session.add(first_work)
        await db_session.commit()

    actor_snapshot = {key: first_work.execution_state[key] for key in ("actor_uid", "actor_username") if key in first_work.execution_state}

    if mode == "absorb":
        _, _, initial_message_ids = await manager.freeze_foreground_input(
            db_session,
            work=first_work,
            worker_id="worker-actor",
        )
        assert initial_message_ids == [first_message.id]

    same_actor_message, same_actor_work = await enqueue_message("same actor", base_actor_uid, base_actor_username)
    different_actor_message, different_actor_work = await enqueue_message(
        "different actor",
        different_actor_uid,
        different_actor_username,
    )
    after_boundary_message, after_boundary_work = await enqueue_message("same actor after boundary", base_actor_uid, base_actor_username)

    if mode == "freeze":
        _, _, frozen_message_ids = await manager.freeze_foreground_input(
            db_session,
            work=first_work,
            worker_id="worker-actor",
        )
        assert frozen_message_ids == [first_message.id, same_actor_message.id]
    else:
        batch = await manager.absorb_contiguous_foreground_messages(
            db_session,
            work_id=first_work.id,
            worker_id="worker-actor",
        )
        assert batch is not None
        assert batch.source_message_ids == (same_actor_message.id,)

    await db_session.refresh(first_work)
    await db_session.refresh(same_actor_work)
    await db_session.refresh(different_actor_work)
    await db_session.refresh(after_boundary_work)

    assert first_work.input_message_ids == [first_message.id, same_actor_message.id]
    assert {key: first_work.execution_state[key] for key in ("actor_uid", "actor_username") if key in first_work.execution_state} == actor_snapshot
    assert same_actor_work.status == SessionReplyWorkStatus.MERGED
    assert same_actor_work.merged_into_id == first_work.id
    assert different_actor_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert different_actor_work.merged_into_id is None
    assert after_boundary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert after_boundary_work.merged_into_id is None

    message_rows = [
        await db_session.get(Message, message_id)
        for message_id in (
            first_message.id,
            same_actor_message.id,
            different_actor_message.id,
            after_boundary_message.id,
        )
    ]
    assert all(message is not None for message in message_rows)
    assert all(message.uid == "user-1" for message in message_rows if message is not None)
    assert all(message.is_processed for message in message_rows[:2] if message is not None)
    assert all(not message.is_processed for message in message_rows[2:] if message is not None)

    work_items = [first_work, same_actor_work, different_actor_work, after_boundary_work]
    assert all(work.uid == "user-1" for work in work_items)
