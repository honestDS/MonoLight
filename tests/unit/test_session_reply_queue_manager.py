from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session_reply_work_item import CRUDSessionReplyWorkItem
from app.core.exceptions import BaseBusinessException
from app.core.session_reply_queue import executor as executor_module
from app.core.session_reply_queue.manager import SessionReplyQueueManager
from app.models.message import Message
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.unit.session_reply_queue_test_support import (
    add_message,
    enqueue,
)

pytest_plugins = ("tests.unit.session_reply_queue_fixture",)


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
    assert merged.status == SessionReplyWorkStatus.MERGED
    assert merged.merged_into_id == first.id
    processed = list((await db_session.execute(select(Message).where(Message.id.in_([1, 2])))).scalars().all())
    assert all(message.is_processed for message in processed)


@pytest.mark.asyncio
async def test_running_foreground_work_absorbs_later_contiguous_messages(db_session: AsyncSession, monkeypatch):
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

    await add_message(db_session, 2, "second")
    second = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=2, dedupe_key="foreground-message:2")
    await add_message(db_session, 3, "third")
    third = await enqueue(crud, db_session, work_type=SessionReplyWorkType.FOREGROUND_REPLY, source_id=3, dedupe_key="foreground-message:3")
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
    assert len(logged_messages) == 1
    assert "second\nthird" in logged_messages[0]
    await db_session.refresh(first)
    await db_session.refresh(second)
    await db_session.refresh(third)
    assert first.input_message_ids == [1, 2, 3]
    assert second.status == SessionReplyWorkStatus.MERGED
    assert second.merged_into_id == first.id
    assert third.status == SessionReplyWorkStatus.MERGED
    assert third.merged_into_id == first.id


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
    assert response["choices"][0]["message"]["content"] == "result"


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

    with pytest.raises(BaseBusinessException, match="所有对话渠道均不可用"):
        await manager.wait_for_result(7)
