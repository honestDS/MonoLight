import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem, session_reply_work_item_crud
from app.core.exceptions import BaseBusinessException
from app.core.session_reply_queue import executor as executor_module
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.unit.session_reply_queue_test_support import (
    AsyncBarrier,
    add_message,
    enqueue,
)

pytest_plugins = ("tests.unit.session_reply_queue_fixture",)


@pytest.mark.asyncio
async def test_external_foreground_message_uses_latest_guidance_across_turns(db_session: AsyncSession):
    manager = SessionReplyQueueManager()
    db_session.add(
        ChatSession(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            source="weixin-openclaw",
            reply_target_source="weixin-openclaw",
        )
    )
    guidance_messages = [
        Message(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.SYSTEM,
            type=MessageType.GUIDANCE,
            content="[系统提示信息]第一条引导[系统提示信息结束]",
            is_processed=False,
        ),
        Message(
            session_id="session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.SYSTEM,
            type=MessageType.GUIDANCE,
            content="[系统提示信息]第二条引导[系统提示信息结束]",
            is_processed=False,
        ),
    ]
    db_session.add_all(guidance_messages)
    await db_session.commit()

    initial_message, work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="用户从 IM 发来的消息",
        attachments=None,
        source="weixin-openclaw",
    )
    input_message = await db_session.get(Message, initial_message.id)
    latest_guidance = guidance_messages[-1].content

    assert input_message.content == "用户从 IM 发来的消息"
    assert input_message.guidance_prompt == latest_guidance
    assert work.execution_state["guidance_prompt"] == latest_guidance
    assert "additional_system_prompt" not in work.execution_state
    for guidance in guidance_messages:
        await db_session.refresh(guidance)
        assert guidance.is_processed is True

    work.status = SessionReplyWorkStatus.RUNNING
    work.locked_by = "worker-1"
    db_session.add(work)
    await db_session.commit()
    content, attachments, message_ids = await manager.freeze_foreground_input(
        db_session,
        work=work,
        worker_id="worker-1",
    )

    assert content == "用户从 IM 发来的消息"
    assert attachments == []
    assert message_ids == [input_message.id]

    work.status = SessionReplyWorkStatus.SUCCEEDED
    work.locked_by = None
    db_session.add(work)
    await db_session.commit()

    next_initial_message, next_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="第二条 IM 消息",
        attachments=None,
        source="weixin-openclaw",
    )
    next_input_message = await db_session.get(Message, next_initial_message.id)

    assert next_input_message.guidance_prompt == latest_guidance
    assert next_work.execution_state["guidance_prompt"] == latest_guidance

    latest_guidance = await message_crud.create_guidance(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        content="[系统提示信息]第三条引导[系统提示信息结束]",
    )
    newest_initial_message, newest_work = await manager._enqueue_foreground_message(
        db_session,
        uid="user-1",
        session_id="session-1",
        profile=SimpleNamespace(id=1),
        message="第三条 IM 消息",
        attachments=None,
        source="weixin-openclaw",
    )
    newest_input_message = await db_session.get(Message, newest_initial_message.id)

    assert newest_input_message.guidance_prompt == latest_guidance.content
    assert newest_work.execution_state["guidance_prompt"] == latest_guidance.content
    assert guidance_messages[0].content not in newest_input_message.guidance_prompt
    assert guidance_messages[1].content not in newest_input_message.guidance_prompt


@pytest.mark.asyncio
async def test_permanent_guidance_is_visible_in_web_history_but_excluded_from_model_history(db_session: AsyncSession):
    guidance = await message_crud.create_guidance(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        content="[系统提示信息]永久引导[系统提示信息结束]",
        commit=False,
    )
    text_message = Message(
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="普通消息",
        is_processed=False,
    )
    refinement_message = Message(
        session_id="session-1",
        uid="user-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.OUTBOUND_TEXT_REFINEMENT,
        content="[系统提示,此处不是用户说的话]上一条助手回复超过渠道文本限制。... [系统提示结束]",
        is_processed=False,
    )
    db_session.add_all([text_message, refinement_message])
    await db_session.commit()

    model_history = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
    )
    unprocessed = await message_crud.get_unprocessed_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
    )
    web_history = await message_crud.get_history_paged(
        db_session,
        session_id="session-1",
        uid="user-1",
    )

    assert [message.id for message in model_history] == [text_message.id]
    assert [message.id for message in unprocessed] == [text_message.id]
    assert {message.id for message in web_history} == {guidance.id, text_message.id}
    assert refinement_message.id not in {message.id for message in model_history}
    assert refinement_message.id not in {message.id for message in unprocessed}
    assert refinement_message.id not in {message.id for message in web_history}
    assert guidance.id in {message.id for message in web_history}
    assert text_message.id in {message.id for message in web_history}


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
async def test_running_foreground_work_absorbs_later_contiguous_messages(db_session: AsyncSession, monkeypatch):
    crud = CRUDSessionReplyWorkItem()
    manager = SessionReplyQueueManager()
    await add_message(db_session, 1, "first")
    first = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=1, dedupe_key="foreground-message:1")
    first.execution_state = {"request_ids": ["request-1", "request-shared"]}
    db_session.add(first)
    await db_session.commit()

    first.status = SessionReplyWorkStatus.RUNNING
    first.locked_by = "worker-1"
    db_session.add(first)
    await db_session.commit()
    await manager.freeze_foreground_input(db_session, work=first, worker_id="worker-1")

    await add_message(db_session, 2, "second")
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await add_message(db_session, 3, "third")
    third = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
    second.execution_state = {"request_ids": ["request-2", "request-shared"]}
    third.execution_state = {"request_ids": ["request-shared", "request-3"]}
    db_session.add(second)
    db_session.add(third)
    await db_session.commit()

    logged_messages: list[str] = []

    class CapturingLogger:
        def bind(self, **kwargs):
            return self

        def info(self, message):
            logged_messages.append(message)

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr("app.core.session_reply_queue.manager.logger", CapturingLogger())
    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

    additional_messages = await manager.absorb_contiguous_foreground_messages(
        db_session,
        work_id=first.id,
        worker_id="worker-1",
    )

    assert len(additional_messages) == 1
    assert additional_messages[0].content == "second\nthird"
    assert additional_messages[0].id == 3
    assert additional_messages.source_message_ids == (2, 3)
    assert additional_messages.summary_boundary_message_id == 2
    assert additional_messages.latest_message_id == 3
    assert len(logged_messages) == 1
    assert "second\nthird" in logged_messages[0]
    await db_session.refresh(first)
    await db_session.refresh(second)
    await db_session.refresh(third)
    assert first.input_message_ids == [1, 2, 3]
    assert first.execution_state["request_ids"] == ["request-1", "request-shared", "request-2", "request-3"]
    assert second.status == SessionReplyWorkStatus.MERGED
    assert second.merged_into_id == first.id
    assert third.status == SessionReplyWorkStatus.MERGED
    assert third.merged_into_id == first.id


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

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

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

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", synchronize_merge)
    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

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

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr(session_reply_work_item_crud, "merge_ready_foreground", wait_for_terminal)
    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

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

    async def skip_runtime_instructions(db, session_id, message):
        return None

    async def merge_ready_foreground(*args, **kwargs):
        if merge_mode == "failure":
            return []
        updated_work_ids = await original_merge_ready_foreground(*args, **kwargs)
        return updated_work_ids[:-1]

    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)
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

    async def skip_runtime_instructions(db, session_id, message):
        return None

    async def lose_claim(*args, **kwargs):
        return False

    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)
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

    async def skip_runtime_instructions(db, session_id, message):
        return None

    monkeypatch.setattr("app.core.session_reply_queue.manager.append_user_runtime_instructions", skip_runtime_instructions)

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
async def test_wait_for_result_returns_resolved_work_id(monkeypatch):
    manager = SessionReplyQueueManager()
    resolved_work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.SUCCEEDED,
        result_message_id=9,
        execution_state={
            "response": {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "result"},
                        "finish_reason": True,
                    }
                ]
            }
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return resolved_work

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)

    response = await manager.wait_for_result(9)

    assert response["work_id"] == 7
    assert response["message_id"] == 9
    assert response["choices"][0]["message"]["content"] == "result"


@pytest.mark.asyncio
async def test_wait_for_stream_returns_result_message_identity_in_done_event(monkeypatch):
    manager = SessionReplyQueueManager()
    resolved_work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
        source_type=SessionReplySourceType.AUDIT_RECORD,
        source_id="42",
        dedupe_key="confirmed-audit:42",
        status=SessionReplyWorkStatus.SUCCEEDED,
        result_message_id=9,
        execution_state={
            "request_ids": ["request-1"],
            "response": {
                "content": "confirmed result",
                "history": [],
                "files": [],
                "response_id": "response-turn-final",
            },
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return resolved_work

    async def list_after_sequence(db, *, work_id, after_sequence_no):
        return []

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)
    monkeypatch.setattr(executor_module.session_reply_stream_event_crud, "list_after_sequence", list_after_sequence)

    events = [event async for event in manager.wait_for_stream(7)]

    assert events == [
        {
            "type": "done",
            "session_id": "session-1",
            "work_id": 7,
            "response_id": "response-turn-final",
            "message_id": 9,
            "history": [],
            "files": [],
            "response": {
                "content": "confirmed result",
                "history": [],
                "files": [],
                "response_id": "response-turn-final",
                "work_id": 7,
                "message_id": 9,
            },
            "request_ids": ["request-1"],
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_result_restores_persisted_user_error_for_adapter(monkeypatch):
    manager = SessionReplyQueueManager()
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.FAILED,
        result_message_id=9,
        error="internal provider failure",
    )
    error_message = SimpleNamespace(content="所有对话渠道均不可用")

    class FakeSession:
        async def get(self, model, object_id):
            assert model is Message
            assert object_id == 9
            return error_message

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def resolve_merged_target(db, work_id):
        return work

    monkeypatch.setattr("app.providers.database.AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "resolve_merged_target", resolve_merged_target)

    with pytest.raises(BaseBusinessException, match="所有对话渠道均不可用") as exc_info:
        await manager.wait_for_result(9)

    assert exc_info.value.data == {
        "work_id": 7,
        "event_id": executor_module.build_session_reply_work_event_id(work, error=True),
    }
