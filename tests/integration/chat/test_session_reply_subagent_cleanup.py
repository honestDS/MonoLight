import asyncio
import json
import time
from datetime import datetime, timedelta
from functools import partial

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete, Update
from sqlmodel import select

import app.core.crud.session.subagent as subagent_module
import app.core.session_reply_queue.child_work as child_work
import app.models  # noqa: F401
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem
from app.core.crud.session.subagent import subagent_crud
from app.core.session_reply_queue import consumer as consumer_module
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkCheckpoint,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork
from tests.integration.chat.test_session_reply_queue_cleanup import subagent_database as _subagent_database

subagent_database = _subagent_database


async def _add_finished_subagent(
    db: AsyncSession,
    *,
    key: str,
    last_called_at: datetime,
) -> tuple[Subagent, SessionReplyWorkItem, SessionReplyWorkItem, SubagentWork, SubagentToolCall]:
    crud = CRUDSessionReplyWorkItem()
    parent, parent_created = await crud.enqueue(
        db,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id=key,
        dedupe_key=f"cleanup-parent:{key}",
        work_category=SessionReplyWorkCategory.FOREGROUND,
        commit=False,
    )
    assert parent_created is True and parent.id is not None
    child, child_created = await crud.enqueue(
        db,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=SessionReplyWorkType.AGENT_WORK,
        source_type=SessionReplySourceType.PARENT_WORK,
        source_id=parent.id,
        dedupe_key=f"cleanup-child:{key}",
        commit=False,
    )
    assert child_created is True and child.id is not None

    parent.status = SessionReplyWorkStatus.SUCCEEDED
    parent.execution_state = {**parent.execution_state, "has_child_work": True}
    subagent = Subagent(
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        name="清理检查",
        last_called_at=last_called_at,
    )
    db.add(subagent)
    await db.flush()

    child.subagent_id = subagent.id
    child.status = SessionReplyWorkStatus.SUCCEEDED
    child.updated_at = get_local_time() - timedelta(hours=48)
    subagent.latest_child_work_id = child.id
    subagent.active_child_work_id = None
    round_row = SubagentWork(
        subagent_id=subagent.id,
        child_work_id=child.id,
        current_work_id=child.id,
        result={
            "work_id": child.id,
            "current_work_id": child.id,
            "completed": True,
            "status": "succeeded",
            "result": {"content": key},
            "error": None,
        },
    )
    db.add(round_row)
    await db.flush()

    tool_call = SubagentToolCall(
        subagent_id=subagent.id,
        child_work_id=child.id,
        parent_work_id=parent.id,
        tool_call_id=f"cleanup-call:{key}",
        arguments={"operation": "create", "name": subagent.name, "message": key},
        delivered=True,
    )
    db.add(tool_call)
    await db.flush()
    return subagent, parent, child, round_row, tool_call


@pytest.mark.asyncio
async def test_consumer_cleanup_subagent_notifications_uses_bounded_scan_cursor(
    subagent_database,
    monkeypatch,
):
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", subagent_database)
    monkeypatch.setattr(consumer_module, "SUBAGENT_NOTIFICATION_CLEANUP_BATCH_SIZE", 2)
    monkeypatch.setattr(consumer_module, "SUBAGENT_NOTIFICATION_CLEANUP_MAX_SCAN_PER_RUN", 2)
    consumer = consumer_module.SessionReplyConsumer()
    crud = CRUDSessionReplyWorkItem()

    async with subagent_database() as db:
        subagent, parent, child, _round, _tool_call = await _add_finished_subagent(
            db,
            key="notification-cleanup",
            last_called_at=get_local_time(),
        )
        pending_work, pending_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="notification-pending-work",
            dedupe_key="cleanup-notification-pending-work",
            work_category=SessionReplyWorkCategory.FOREGROUND,
            commit=False,
        )
        assert pending_created is True and pending_work.id is not None
        pending_work.status = SessionReplyWorkStatus.RUNNING
        db.add(pending_work)
        await db.flush()

        message_base = {
            "uid": "user-1",
            "session_id": "session-1",
            "profile_id": 1,
            "role": MessageRole.USER,
            "work_id": None,
            "is_processed": True,
        }
        normal_messages = [
            Message(
                **message_base,
                type=MessageType.TEXT,
                content="plain",
            )
            for _ in range(3)
        ]
        db.add_all(normal_messages)
        await db.flush()

        orphan_payloads = [
            {
                "type": "subagent_board_update",
                "source": "subagent",
                "subagent_id": "missing-board-subagent",
                "work_id": 10001,
                "post_id": 1,
                "stage": "update",
            },
            {
                "type": "subagent_result_ready",
                "source": "subagent",
                "subagent_id": "missing-result-subagent",
                "work_id": 10002,
            },
            {
                "type": "external_work_resume",
                "source": "subagent",
                "results": [{"subagent_id": "missing-resume-subagent", "work_id": 10003}],
            },
        ]
        message_specs = [(json.dumps(payload), MessageType.SUBAGENT_RESULT) for payload in orphan_payloads]
        message_specs.extend(
            [
                (
                    json.dumps(
                        {
                            "type": "subagent_result_ready",
                            "source": "subagent",
                            "subagent_id": subagent.id,
                            "work_id": child.id,
                        }
                    ),
                    MessageType.SUBAGENT_RESULT,
                ),
                (
                    json.dumps(
                        {
                            "type": "subagent_result_ready",
                            "source": "subagent",
                            "subagent_id": "pending-subagent",
                            "work_id": 10004,
                        }
                    ),
                    MessageType.SUBAGENT_RESULT,
                ),
                ("plain text", MessageType.SUBAGENT_RESULT),
                ("approve", MessageType.AUDIT_DECISION),
            ]
        )
        messages = []
        for content, message_type in message_specs:
            messages.append(
                await message_crud.create(
                    db,
                    obj_in={**message_base, "type": message_type, "content": content},
                    commit=False,
                )
            )
        max_message = await message_crud.create(
            db,
            obj_in={
                **message_base,
                "type": MessageType.SUBAGENT_RESULT,
                "content": json.dumps(
                    {
                        "type": "subagent_result_ready",
                        "source": "subagent",
                        "subagent_id": "max-subagent",
                        "work_id": 10005,
                    }
                ),
            },
            commit=False,
        )
        await db.flush()

        orphan_messages = messages[:3]
        linked_message, pending_message, invalid_json_message, audit_message = messages[3:]
        assert pending_message.id is not None and max_message.id is not None
        checkpoint_messages = [{"role": "user", "content": "shared checkpoint"}]
        await crud.save_checkpoint(db, parent.id, {"messages": checkpoint_messages})
        await crud.save_checkpoint(
            db,
            pending_work.id,
            {"pending_resume_message_ids": [pending_message.id], "messages": checkpoint_messages},
        )
        await db.commit()

        await consumer._cleanup_subagent_notifications()

        remaining_message_ids = set((await db.execute(select(Message.id))).scalars().all())
        assert consumer._notification_cleanup_after_id == normal_messages[1].id
        assert remaining_message_ids == {message.id for message in [*normal_messages, *messages, max_message]}
        await db.commit()

        scan_batch_size = 2
        scan_calls = (len(normal_messages) + len(messages) + scan_batch_size - 1) // scan_batch_size
        for _ in range(scan_calls):
            await consumer._cleanup_subagent_notifications()

        retained_message_ids = {message.id for message in [*normal_messages, *messages[3:], max_message]}
        assert consumer._notification_cleanup_after_id == 0
        remaining_message_ids = set((await db.execute(select(Message.id))).scalars().all())
        assert not {message.id for message in orphan_messages}.intersection(remaining_message_ids)
        assert remaining_message_ids == retained_message_ids
        assert {linked_message.id, pending_message.id, invalid_json_message.id, audit_message.id, max_message.id} <= remaining_message_ids
        assert {work.id for work in (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_([parent.id, child.id])))).scalars().all()} == {parent.id, child.id}
        assert (await db.get(Subagent, subagent.id)) is not None
        assert (await db.get(SubagentWork, child.id)) is not None
        await db.commit()

        await crud.delete_checkpoint(db, pending_work.id)
        await db.commit()
        scan_calls = (len(retained_message_ids) + scan_batch_size - 1) // scan_batch_size
        for _ in range(scan_calls):
            await consumer._cleanup_subagent_notifications()

        remaining_message_ids = set((await db.execute(select(Message.id))).scalars().all())
        assert consumer._notification_cleanup_after_id == 0
        assert remaining_message_ids == retained_message_ids - {pending_message.id}
        assert max_message.id in remaining_message_ids


@pytest.mark.asyncio
async def test_consumer_cleanup_removes_idle_subagent_data_and_respects_hourly_schedule(subagent_database, monkeypatch):
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", subagent_database)
    monkeypatch.setattr(subagent_crud, "cleanup_idle", partial(subagent_crud.cleanup_idle, retention_limit=1))
    consumer = consumer_module.SessionReplyConsumer()
    now = get_local_time()
    expired_at = now - timedelta(hours=25)

    async with subagent_database() as db:
        expired_subagent, expired_parent, expired_child, _expired_round, expired_call = await _add_finished_subagent(
            db,
            key="expired",
            last_called_at=expired_at,
        )
        recent_subagent, recent_parent, recent_child, _recent_round, recent_call = await _add_finished_subagent(
            db,
            key="recent",
            last_called_at=now,
        )
        private_user = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_id=expired_child.id,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="private user",
            is_processed=False,
        )
        private_result = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_id=expired_child.id,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="private result",
            is_processed=True,
        )
        public_message = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="public message",
            is_processed=True,
        )
        db.add_all(
            [
                private_user,
                private_result,
                public_message,
                SessionReplyStreamEvent(
                    work_id=expired_child.id,
                    sequence_no=1,
                    event={"type": "content", "content": "private stream"},
                ),
            ]
        )
        await db.flush()
        expired_child.result_message_id = private_result.id
        private_result_id = private_result.id
        assert private_result_id is not None
        audit_record = AuditRecord(
            uid="user-1",
            operator_username="cleanup-user",
            session_id="session-1",
            source="http",
            language="zh",
            execution_scope="task",
            source_work_id=expired_child.id,
            status=AuditRecordStatus.SUCCEEDED,
            source_assistant_message_id=private_result_id,
            working_directory=".",
            round_arguments_hash="a" * 64,
            tool_count=1,
        )
        db.add(audit_record)
        await db.flush()
        audit_record_id = audit_record.id
        assert audit_record_id is not None
        audit_detail = AuditToolDetail(
            audit_record_id=audit_record_id,
            original_tool_call_id="cleanup-audit-call",
            turn_index=0,
            tool_name="file_tool",
            conclusion=AuditToolConclusion.PASSED,
            score=0,
            reason="allowed file operation",
            arguments_hash="b" * 64,
            arguments_summary="write child result",
        )
        db.add(audit_detail)
        await db.flush()
        audit_detail_id = audit_detail.id
        assert audit_detail_id is not None

        shared_payload = {"role": "assistant", "content": "shared checkpoint"}
        private_payload = {"role": "user", "content": "private checkpoint"}
        crud = CRUDSessionReplyWorkItem()
        await crud.save_checkpoint(
            db,
            expired_child.id,
            {"messages": [shared_payload, private_payload]},
        )
        await crud.save_checkpoint(
            db,
            expired_parent.id,
            {"messages": [shared_payload]},
        )
        checkpoint_messages = list((await db.execute(select(SessionReplyCheckpointMessage))).scalars().all())
        shared_checkpoint_message_id = next(row.id for row in checkpoint_messages if row.payload == shared_payload)
        private_checkpoint_message_id = next(row.id for row in checkpoint_messages if row.payload == private_payload)
        await db.commit()

    await consumer._cleanup_terminal_items_if_due(5.0)

    async with subagent_database() as db:
        assert not (await db.execute(select(Subagent).where(Subagent.id == expired_subagent.id))).scalars().all()
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == expired_child.id))).scalars().all()
        assert not (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == expired_child.id))).scalars().all()
        assert not (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == expired_call.id))).scalars().all()
        assert not (await db.execute(select(Message).where(Message.id.in_([private_user.id, private_result.id])))).scalars().all()
        audit_record_row = (await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id))).scalars().one()
        assert (
            audit_record_row.source_work_id,
            audit_record_row.source_assistant_message_id,
            audit_record_row.round_arguments_hash,
            audit_record_row.status,
        ) == (expired_child.id, private_result_id, "a" * 64, AuditRecordStatus.SUCCEEDED)
        audit_detail_row = (await db.execute(select(AuditToolDetail).where(AuditToolDetail.id == audit_detail_id))).scalars().one()
        assert (audit_detail_row.audit_record_id, audit_detail_row.original_tool_call_id, audit_detail_row.arguments_hash) == (
            audit_record_id,
            "cleanup-audit-call",
            "b" * 64,
        )
        assert not (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == expired_child.id))).scalars().all()
        assert not (await db.execute(select(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == expired_child.id))).scalars().all()
        assert not (await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id == expired_child.id))).scalars().all()
        assert not (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id == private_checkpoint_message_id))).scalars().all()

        expired_parent_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == expired_parent.id))).scalars().one()
        assert expired_parent_row.status == SessionReplyWorkStatus.SUCCEEDED
        parent_checkpoint = await crud.get_checkpoint(db, expired_parent.id)
        assert parent_checkpoint == {"messages": [shared_payload]}
        shared_checkpoint_row = (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id == shared_checkpoint_message_id))).scalars().one()
        assert shared_checkpoint_row.payload == shared_payload
        assert (await db.execute(select(Message.content).where(Message.id == public_message.id))).scalar_one() == "public message"

        recent_subagent_row = (await db.execute(select(Subagent).where(Subagent.id == recent_subagent.id))).scalars().one()
        assert (recent_subagent_row.latest_child_work_id, recent_subagent_row.active_child_work_id) == (recent_child.id, None)
        recent_child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == recent_child.id))).scalars().one()
        assert recent_child_row.status == SessionReplyWorkStatus.SUCCEEDED
        assert recent_child_row.subagent_id == recent_subagent.id
        recent_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == recent_child.id))).scalars().one()
        assert recent_round_row.result == {
            "work_id": recent_child.id,
            "current_work_id": recent_child.id,
            "completed": True,
            "status": "succeeded",
            "result": {"content": "recent"},
            "error": None,
        }
        recent_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == recent_call.id))).scalars().one()
        assert (recent_call_row.parent_work_id, recent_call_row.child_work_id, recent_call_row.delivered) == (
            recent_parent.id,
            recent_child.id,
            True,
        )

    async with subagent_database() as db:
        recent_subagent_row = (await db.execute(select(Subagent).where(Subagent.id == recent_subagent.id))).scalars().one()
        recent_subagent_row.last_called_at = get_local_time() - timedelta(hours=25)
        await db.commit()

    await consumer._cleanup_terminal_items_if_due(3604.9)
    async with subagent_database() as db:
        assert (await db.execute(select(Subagent).where(Subagent.id == recent_subagent.id))).scalars().one().id == recent_subagent.id
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == recent_child.id))).scalars().one().id == recent_child.id

    async with subagent_database() as db:
        new_subagent, new_parent, new_child, _new_round, _new_call = await _add_finished_subagent(
            db,
            key="new",
            last_called_at=get_local_time(),
        )
        await db.commit()

    await consumer._cleanup_terminal_items_if_due(3605.0)
    async with subagent_database() as db:
        assert not (await db.execute(select(Subagent).where(Subagent.id == recent_subagent.id))).scalars().all()
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == recent_child.id))).scalars().all()
        assert (await db.execute(select(Subagent).where(Subagent.id == new_subagent.id))).scalars().one().id == new_subagent.id
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == new_child.id))).scalars().one().id == new_child.id

        parent_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == expired_parent.id))).scalars().one()
        parent_row.updated_at = get_local_time() - timedelta(hours=25)
        await db.commit()
        await crud.cleanup_terminal_items(db, retention_limit=2)

    async with subagent_database() as db:
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == expired_parent.id))).scalars().all()
        assert {work.id for work in (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_([recent_parent.id, new_parent.id])))).scalars().all()} == {recent_parent.id, new_parent.id}
        assert (await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id))).scalars().one().id == audit_record_id
        assert (await db.execute(select(AuditToolDetail).where(AuditToolDetail.id == audit_detail_id))).scalars().one().id == audit_detail_id
        assert (await db.execute(select(Message.content).where(Message.id == public_message.id))).scalar_one() == "public message"


@pytest.mark.asyncio
async def test_idle_cleanup_removes_all_expired_subagent_round_and_derived_work(subagent_database):
    now = get_local_time()
    expired_at = now - timedelta(hours=25)
    crud = CRUDSessionReplyWorkItem()

    async with subagent_database() as db:
        expired_subagent, expired_parent, expired_root, expired_round, expired_call = await _add_finished_subagent(
            db,
            key="expired-all-rounds",
            last_called_at=expired_at,
        )
        recent_subagent, recent_parent, recent_child, recent_round, _recent_call = await _add_finished_subagent(
            db,
            key="recent-all-rounds",
            last_called_at=now,
        )

        expired_root.status = SessionReplyWorkStatus.FAILED
        expired_root.error = "first round failed"
        expired_round.result = {
            "work_id": expired_root.id,
            "current_work_id": expired_root.id,
            "completed": True,
            "status": "failed",
            "result": {"content": "first round"},
            "error": "first round failed",
        }

        second_root, second_root_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.AGENT_WORK,
            source_type=SessionReplySourceType.PARENT_WORK,
            source_id=expired_parent.id,
            dedupe_key="cleanup-second-round",
            work_category=SessionReplyWorkCategory.SUBAGENT,
            commit=False,
        )
        assert second_root_created is True and second_root.id is not None
        second_root.subagent_id = expired_subagent.id
        second_root.status = SessionReplyWorkStatus.CANCELLED
        second_round = SubagentWork(
            subagent_id=expired_subagent.id,
            child_work_id=second_root.id,
            current_work_id=second_root.id,
            result={
                "work_id": second_root.id,
                "current_work_id": second_root.id,
                "completed": True,
                "status": "cancelled",
                "result": {"content": "second round"},
                "error": None,
            },
        )
        db.add(second_round)
        await db.flush()

        second_call = SubagentToolCall(
            subagent_id=expired_subagent.id,
            child_work_id=second_root.id,
            parent_work_id=expired_parent.id,
            tool_call_id="cleanup-second-round-call",
            arguments={"operation": "create", "name": expired_subagent.name, "message": "second round"},
            delivered=True,
        )
        db.add(second_call)
        await db.flush()

        confirmed, confirmed_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            source_type=SessionReplySourceType.AUDIT_RECORD,
            source_id="cleanup-expired-audit",
            dedupe_key="cleanup-expired-confirmed",
            work_category=SessionReplyWorkCategory.SUBAGENT,
            commit=False,
        )
        assert confirmed_created is True and confirmed.id is not None
        confirmed.subagent_id = expired_subagent.id
        confirmed.status = SessionReplyWorkStatus.SUCCEEDED
        confirmed.execution_state = {
            **confirmed.execution_state,
            "child_work_id": expired_root.id,
        }
        expired_round.current_work_id = confirmed.id
        expired_round.result = {
            **expired_round.result,
            "current_work_id": confirmed.id,
        }

        resume, resume_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.BACKGROUND_TASK_RESUME,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id="cleanup-expired-background",
            dedupe_key="cleanup-expired-resume",
            work_category=SessionReplyWorkCategory.SUBAGENT,
            commit=False,
        )
        assert resume_created is True and resume.id is not None
        resume.subagent_id = expired_subagent.id
        resume.status = SessionReplyWorkStatus.SUCCEEDED
        resume.execution_state = {
            **resume.execution_state,
            "child_work_id": second_root.id,
        }
        second_round.current_work_id = resume.id
        second_round.result = {
            **second_round.result,
            "current_work_id": resume.id,
        }
        expired_subagent.active_child_work_id = None
        expired_subagent.latest_child_work_id = second_root.id

        expired_work_items = [expired_root, second_root, confirmed, resume]
        private_messages = [
            Message(
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_id=work.id,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=f"expired private message {index}",
                is_processed=False,
            )
            for index, work in enumerate(expired_work_items, start=1)
        ]
        db.add_all(private_messages)
        await db.flush()
        expired_work_ids = [work.id for work in expired_work_items]
        private_message_ids = [message.id for message in private_messages]

        for index, work in enumerate(expired_work_items, start=1):
            await crud.save_checkpoint(
                db,
                work.id,
                {"messages": [{"role": "user", "content": f"expired checkpoint {index}"}]},
            )
        checkpoint_references = list((await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id.in_(expired_work_ids)).order_by(SessionReplyCheckpointMessageReference.work_id))).scalars().all())
        checkpoint_message_ids = [reference.message_id for reference in checkpoint_references]
        assert len(checkpoint_references) == len(expired_work_items)
        await db.commit()

    async with subagent_database() as db:
        assert await subagent_crud.cleanup_idle(db, retention_limit=1) == 1

    async with subagent_database() as db:
        assert (await db.execute(select(Subagent).where(Subagent.id == expired_subagent.id))).scalars().one_or_none() is None
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(expired_work_ids)))).scalars().all()
        assert not (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id.in_([expired_root.id, second_root.id])))).scalars().all()
        assert not (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id.in_([expired_call.id, second_call.id])))).scalars().all()
        assert not (await db.execute(select(Message).where(Message.id.in_(private_message_ids)))).scalars().all()
        assert not (await db.execute(select(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id.in_(expired_work_ids)))).scalars().all()
        assert not (await db.execute(select(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.work_id.in_(expired_work_ids)))).scalars().all()
        assert not (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id.in_(checkpoint_message_ids)))).scalars().all()

        parent_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == expired_parent.id))).scalars().one()
        assert (parent_row.uid, parent_row.session_id, parent_row.profile_id) == ("user-1", "session-1", 1)
        assert parent_row.status == SessionReplyWorkStatus.SUCCEEDED

        recent_subagent_row = (await db.execute(select(Subagent).where(Subagent.id == recent_subagent.id))).scalars().one()
        assert (recent_subagent_row.latest_child_work_id, recent_subagent_row.active_child_work_id) == (
            recent_child.id,
            None,
        )
        recent_child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == recent_child.id))).scalars().one()
        assert (recent_child_row.subagent_id, recent_child_row.status) == (
            recent_subagent.id,
            SessionReplyWorkStatus.SUCCEEDED,
        )
        recent_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == recent_child.id))).scalars().one()
        assert recent_round_row.result == {
            "work_id": recent_child.id,
            "current_work_id": recent_child.id,
            "completed": True,
            "status": "succeeded",
            "result": {"content": "recent-all-rounds"},
            "error": None,
        }
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == recent_parent.id))).scalars().one().status == SessionReplyWorkStatus.SUCCEEDED


@pytest.mark.parametrize("interruption", ["error", "cancel"])
@pytest.mark.asyncio
async def test_idle_cleanup_rolls_back_all_data_on_interruption(subagent_database, monkeypatch, interruption):
    crud = CRUDSessionReplyWorkItem()
    private_message_content = f"rollback private message {interruption}"
    stream_event_payload = {"type": "content", "content": f"rollback stream {interruption}"}
    checkpoint_payload = {"role": "user", "content": f"rollback checkpoint {interruption}"}

    async with subagent_database() as db:
        subagent, parent, child, round_row, tool_call = await _add_finished_subagent(
            db,
            key=f"rollback-{interruption}",
            last_called_at=get_local_time() - timedelta(hours=25),
        )
        private_message = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_id=child.id,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=private_message_content,
            is_processed=False,
        )
        stream_event = SessionReplyStreamEvent(
            work_id=child.id,
            sequence_no=1,
            event=stream_event_payload,
        )
        db.add_all([private_message, stream_event])
        await db.flush()

        expected_checkpoint = {"messages": [checkpoint_payload]}
        await crud.save_checkpoint(db, child.id, expected_checkpoint)
        saved_checkpoint = await crud.get_checkpoint(db, child.id)
        assert saved_checkpoint == expected_checkpoint

        checkpoint_row = (await db.execute(select(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == child.id))).scalars().one()
        checkpoint_reference = (
            (
                await db.execute(
                    select(SessionReplyCheckpointMessageReference).where(
                        SessionReplyCheckpointMessageReference.work_id == child.id,
                        SessionReplyCheckpointMessageReference.kind == "checkpoint",
                    )
                )
            )
            .scalars()
            .one()
        )
        checkpoint_message = (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id == checkpoint_reference.message_id))).scalars().one()
        await db.commit()

        subagent_id = subagent.id
        parent_id = parent.id
        child_id = child.id
        round_child_work_id = round_row.child_work_id
        tool_call_id = tool_call.id
        private_message_id = private_message.id
        stream_event_id = stream_event.id
        checkpoint_work_id = checkpoint_row.work_id
        checkpoint_reference_key = (checkpoint_reference.work_id, checkpoint_reference.kind, checkpoint_reference.message_id)
        checkpoint_message_id = checkpoint_message.id
        saved_round_result = round_row.result
        saved_call_delivered = tool_call.delivered

    async with subagent_database() as cleanup_db:
        original_execute = cleanup_db.execute

        async def execute(statement, *args, **kwargs):
            if isinstance(statement, Delete) and statement.table.name == "subagent":
                if interruption == "error":
                    raise RuntimeError("cleanup interruption")
                raise asyncio.CancelledError
            return await original_execute(statement, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(cleanup_db, "execute", execute)
            expected_exception = RuntimeError if interruption == "error" else asyncio.CancelledError
            expected_match = "cleanup interruption" if interruption == "error" else None
            with pytest.raises(expected_exception, match=expected_match):
                await subagent_crud.cleanup_idle(cleanup_db, retention_limit=0)

    async with subagent_database() as db:
        subagent_row = (await db.execute(select(Subagent).where(Subagent.id == subagent_id))).scalars().one()
        assert subagent_row.id == subagent_id

        parent_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == parent_id))).scalars().one()
        assert parent_row.id == parent_id

        child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == child_id))).scalars().one()
        assert (child_row.id, child_row.subagent_id) == (child_id, subagent_id)

        round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == round_child_work_id))).scalars().one()
        assert round_row.result == saved_round_result

        call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == tool_call_id))).scalars().one()
        assert (call_row.id, call_row.delivered) == (tool_call_id, saved_call_delivered)

        private_message_row = (await db.execute(select(Message).where(Message.id == private_message_id))).scalars().one()
        assert private_message_row.content == private_message_content

        stream_event_row = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.id == stream_event_id))).scalars().one()
        assert stream_event_row.event == stream_event_payload

        checkpoint_row = (await db.execute(select(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == checkpoint_work_id))).scalars().one()
        assert checkpoint_row.checkpoint == {"messages": [checkpoint_message_id]}

        checkpoint_reference = (
            (
                await db.execute(
                    select(SessionReplyCheckpointMessageReference).where(
                        SessionReplyCheckpointMessageReference.work_id == checkpoint_reference_key[0],
                        SessionReplyCheckpointMessageReference.kind == checkpoint_reference_key[1],
                        SessionReplyCheckpointMessageReference.message_id == checkpoint_reference_key[2],
                    )
                )
            )
            .scalars()
            .one()
        )
        assert (checkpoint_reference.work_id, checkpoint_reference.kind, checkpoint_reference.message_id) == checkpoint_reference_key

        checkpoint_message = (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id == checkpoint_message_id))).scalars().one()
        assert checkpoint_message.payload == checkpoint_payload
        assert await crud.get_checkpoint(db, child_id) == saved_checkpoint

    async with subagent_database() as db:
        assert await subagent_crud.cleanup_idle(db, retention_limit=0) == 1

    async with subagent_database() as db:
        assert (await db.execute(select(Subagent).where(Subagent.id == subagent_id))).scalars().one_or_none() is None
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == child_id))).scalars().one_or_none() is None
        assert (await db.execute(select(Message).where(Message.id == private_message_id))).scalars().one_or_none() is None


@pytest.mark.parametrize("specify_work_id", [False, True])
@pytest.mark.asyncio
async def test_query_subagent_refreshes_retention_and_cleanup_respects_capacity(
    subagent_database,
    monkeypatch,
    specify_work_id,
):
    fixed_now = get_local_time()
    clock = fixed_now
    monkeypatch.setattr(subagent_module, "get_local_time", lambda: clock)

    async with subagent_database() as db:
        subagent, parent, child, round_row, tool_call = await _add_finished_subagent(
            db,
            key="query-retention-old",
            last_called_at=fixed_now - timedelta(hours=48),
        )
        control_subagent, control_parent, control_child, _control_round, control_call = await _add_finished_subagent(
            db,
            key="query-retention-control",
            last_called_at=fixed_now - timedelta(hours=36),
        )
        child_updated_at = child.updated_at
        saved_round_result = round_row.result
        crud = CRUDSessionReplyWorkItem()
        query_parent, query_parent_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="query-retention-operator",
            dedupe_key="cleanup-parent:query-retention-operator",
            work_category=SessionReplyWorkCategory.FOREGROUND,
            commit=False,
        )
        assert query_parent_created is True and query_parent.id is not None
        query_parent.status = SessionReplyWorkStatus.RUNNING
        query_parent.locked_by = "cleanup-query-worker"
        query_parent.lock_until = int(time.time()) + 300
        await db.commit()

    async with subagent_database() as db:
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (query_parent.id, "cleanup-query-worker")
        response = await child_work.query_subagent_result(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            subagent_id=subagent.id,
            work_id=child.id if specify_work_id else None,
        )

        assert response == {"subagent_id": subagent.id, "work_id": child.id, "status": "succeeded", "content": saved_round_result["result"]["content"]}

    async with subagent_database() as db:
        subagent_row = (await db.execute(select(Subagent).where(Subagent.id == subagent.id))).scalars().one()
        assert subagent_row.last_called_at.replace(tzinfo=None) == fixed_now.replace(tzinfo=None)
        child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == child.id))).scalars().one()
        assert child_row.updated_at.replace(tzinfo=None) == child_updated_at.replace(tzinfo=None)
        round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child.id))).scalars().one()
        assert round_row.result == saved_round_result
        call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == tool_call.id))).scalars().one()
        assert call_row.id == tool_call.id

    async with subagent_database() as db:
        assert await subagent_crud.cleanup_idle(db, retention_limit=1) == 1

    async with subagent_database() as db:
        queried_subagent = (await db.execute(select(Subagent).where(Subagent.id == subagent.id))).scalars().one()
        assert queried_subagent.id == subagent.id
        queried_child = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == child.id))).scalars().one()
        assert queried_child.id == child.id
        queried_round = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child.id))).scalars().one()
        assert queried_round.result == saved_round_result
        queried_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == tool_call.id))).scalars().one()
        assert queried_call.id == tool_call.id

        assert (await db.execute(select(Subagent).where(Subagent.id == control_subagent.id))).scalars().one_or_none() is None
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == control_child.id))).scalars().one_or_none() is None
        assert (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == control_child.id))).scalars().one_or_none() is None
        assert (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == control_call.id))).scalars().one_or_none() is None
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == parent.id))).scalars().one().id == parent.id
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == control_parent.id))).scalars().one().id == control_parent.id
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == query_parent.id))).scalars().one().id == query_parent.id


@pytest.mark.parametrize("operation", ["query_result", "send_message"])
@pytest.mark.asyncio
async def test_idle_cleanup_rechecks_activity_after_candidate_scan(subagent_database, monkeypatch, operation):
    idle_at = get_local_time() - timedelta(hours=25)
    async with subagent_database() as db:
        subagent, parent, child, round_row, tool_call = await _add_finished_subagent(
            db,
            key=f"cleanup-race-{operation}",
            last_called_at=idle_at,
        )
        saved_round_result = round_row.result
        crud = CRUDSessionReplyWorkItem()
        operator_parent, operator_parent_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=f"cleanup-race-operator-{operation}",
            dedupe_key=f"cleanup-parent:cleanup-race-operator-{operation}",
            work_category=SessionReplyWorkCategory.FOREGROUND,
            commit=False,
        )
        assert operator_parent_created is True and operator_parent.id is not None
        operator_parent.status = SessionReplyWorkStatus.RUNNING
        operator_parent.locked_by = "cleanup-race-worker"
        operator_parent.lock_until = int(time.time()) + 300
        await db.commit()

    scanned = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_task = None

    async with subagent_database() as cleanup_db:
        async with subagent_database() as tool_db:
            original_execute = cleanup_db.execute
            update_paused = False

            async def execute(statement, *args, **kwargs):
                nonlocal update_paused
                if isinstance(statement, Update) and statement.table.name == "subagent" and not update_paused:
                    update_paused = True
                    scanned.set()
                    await allow_cleanup.wait()
                return await original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(cleanup_db, "execute", execute)

            try:
                cleanup_task = asyncio.create_task(subagent_crud.cleanup_idle(cleanup_db, retention_limit=0))
                await asyncio.wait_for(scanned.wait(), timeout=5)

                tool_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (operator_parent.id, "cleanup-race-worker")
                if operation == "query_result":
                    response = await child_work.query_subagent_result(
                        tool_db,
                        uid="user-1",
                        session_id="session-1",
                        profile_id=1,
                        subagent_id=subagent.id,
                    )
                    assert response == {
                        "subagent_id": subagent.id,
                        "work_id": child.id,
                        "status": "succeeded",
                        "content": saved_round_result["result"]["content"],
                    }
                    new_work_id = None
                else:
                    response = await child_work.send_subagent_message(
                        tool_db,
                        uid="user-1",
                        session_id="session-1",
                        profile_id=1,
                        tool_call_id="cleanup-race-send",
                        subagent_id=subagent.id,
                        message="继续已保存的子任务",
                    )
                    assert response["status"] == "queued"
                    assert response["subagent_id"] == subagent.id
                    new_work_id = response["work_id"]
                    assert new_work_id is not None

                allow_cleanup.set()
                assert await asyncio.wait_for(cleanup_task, timeout=10) == 0

                async with subagent_database() as db:
                    subagent_row = (await db.execute(select(Subagent).where(Subagent.id == subagent.id))).scalars().one()
                    assert subagent_row.last_called_at.replace(tzinfo=None) > idle_at.replace(tzinfo=None)

                    child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == child.id))).scalars().one()
                    assert child_row.id == child.id
                    old_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child.id))).scalars().one()
                    old_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == tool_call.id))).scalars().one()
                    assert old_call_row.id == tool_call.id

                    if operation == "query_result":
                        assert old_round_row.result == saved_round_result
                    else:
                        new_child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == new_work_id))).scalars().one()
                        new_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == new_work_id))).scalars().one()
                        new_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.tool_call_id == "cleanup-race-send"))).scalars().one()
                        sent_message = (
                            (
                                await db.execute(
                                    select(Message).where(
                                        Message.content == "继续已保存的子任务",
                                        Message.role == MessageRole.USER,
                                        Message.type == MessageType.TEXT,
                                    )
                                )
                            )
                            .scalars()
                            .one()
                        )

                        assert new_child_row.id == new_work_id
                        assert new_round_row.child_work_id == new_work_id
                        assert (subagent_row.active_child_work_id, subagent_row.latest_child_work_id) == (
                            new_work_id,
                            new_work_id,
                        )
                        assert new_call_row.delivered is False
                        assert new_call_row.child_work_id == new_work_id
                        assert sent_message.content == "继续已保存的子任务"
                        assert sent_message.work_id == new_work_id
            finally:
                allow_cleanup.set()
                if cleanup_task is not None:
                    if not cleanup_task.done():
                        cleanup_task.cancel()
                    try:
                        await cleanup_task
                    except asyncio.CancelledError:
                        pass


@pytest.mark.parametrize(
    "state",
    [
        "queued",
        "running",
        "waiting_external",
        "waiting_confirmation",
        "awaiting_reconciliation",
        "pending_delivery",
    ],
)
@pytest.mark.asyncio
async def test_idle_cleanup_preserves_unfinished_or_undelivered_subagents(subagent_database, state):
    now = get_local_time()
    idle_at = now - timedelta(hours=25)
    expected_status = {
        "queued": SessionReplyWorkStatus.READY_FOR_LLM,
        "running": SessionReplyWorkStatus.RUNNING,
        "waiting_external": SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
        "waiting_confirmation": SessionReplyWorkStatus.SUCCEEDED,
        "awaiting_reconciliation": SessionReplyWorkStatus.FAILED,
        "pending_delivery": SessionReplyWorkStatus.SUCCEEDED,
    }[state]

    async with subagent_database() as db:
        protected_subagent, protected_parent, protected_child, protected_round, protected_call = await _add_finished_subagent(
            db,
            key=f"protected-{state}",
            last_called_at=idle_at,
        )
        control_subagent, _control_parent, control_child, _control_round, _control_call = await _add_finished_subagent(
            db,
            key=f"control-{state}",
            last_called_at=idle_at,
        )

        protected_parent.updated_at = now - timedelta(hours=48)
        protected_message = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_id=protected_child.id,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=f"private user {state}",
            is_processed=False,
        )
        db.add(protected_message)

        saved_round_result = protected_round.result
        protected_call.delivered = False
        if state == "pending_delivery":
            protected_child.status = SessionReplyWorkStatus.SUCCEEDED
            protected_subagent.active_child_work_id = None
        else:
            protected_round.result = None
            protected_subagent.active_child_work_id = protected_child.id
            protected_child.status = expected_status
            if state == "waiting_confirmation":
                protected_round.waiting_confirmation = True

        await db.commit()

        assert await subagent_crud.cleanup_idle(db, retention_limit=0) == 1
        await CRUDSessionReplyWorkItem().cleanup_terminal_items(db, retention_limit=0)

    async with subagent_database() as db:
        protected_subagent_row = (await db.execute(select(Subagent).where(Subagent.id == protected_subagent.id))).scalars().one()
        assert protected_subagent_row.id == protected_subagent.id
        assert protected_subagent_row.latest_child_work_id == protected_child.id
        assert protected_subagent_row.active_child_work_id == (None if state == "pending_delivery" else protected_child.id)

        protected_parent_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == protected_parent.id))).scalars().one()
        assert protected_parent_row.id == protected_parent.id
        assert protected_parent_row.status == SessionReplyWorkStatus.SUCCEEDED

        protected_child_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == protected_child.id))).scalars().one()
        assert protected_child_row.id == protected_child.id
        assert protected_child_row.subagent_id == protected_subagent.id
        assert protected_child_row.status == expected_status

        protected_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == protected_child.id))).scalars().one()
        assert protected_round_row.subagent_id == protected_subagent.id
        assert protected_round_row.current_work_id == protected_child.id
        assert protected_round_row.result == (saved_round_result if state == "pending_delivery" else None)
        assert protected_round_row.waiting_confirmation is (state == "waiting_confirmation")

        protected_call_row = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id == protected_call.id))).scalars().one()
        assert (protected_call_row.subagent_id, protected_call_row.child_work_id, protected_call_row.parent_work_id) == (
            protected_subagent.id,
            protected_child.id,
            protected_parent.id,
        )
        assert protected_call_row.delivered is False

        protected_message_row = (await db.execute(select(Message).where(Message.id == protected_message.id))).scalars().one()
        assert protected_message_row.work_id == protected_child.id
        assert protected_message_row.role == MessageRole.USER
        assert protected_message_row.is_processed is False

        assert (await db.execute(select(Subagent).where(Subagent.id == control_subagent.id))).scalars().one_or_none() is None
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == control_child.id))).scalars().one_or_none() is None


@pytest.mark.asyncio
async def test_consumer_idle_cleanup_obeys_batch_and_per_run_limits(subagent_database, monkeypatch):
    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", subagent_database)
    monkeypatch.setattr(consumer_module, "SUBAGENT_CLEANUP_BATCH_SIZE", 1)
    monkeypatch.setattr(consumer_module, "SUBAGENT_CLEANUP_MAX_ITEMS_PER_RUN", 2)
    monkeypatch.setattr(subagent_crud, "cleanup_idle", partial(subagent_crud.cleanup_idle, retention_limit=0))
    consumer = consumer_module.SessionReplyConsumer()
    now = get_local_time()

    async with subagent_database() as db:
        seeded = [
            await _add_finished_subagent(
                db,
                key=key,
                last_called_at=now - timedelta(hours=hours),
            )
            for key, hours in (("batch-oldest", 28), ("batch-middle", 27), ("batch-newest", 26))
        ]
        await db.commit()

    oldest, middle, newest = seeded
    old_subagent_ids = [oldest[0].id, middle[0].id]
    old_child_ids = [oldest[2].id, middle[2].id]
    old_call_ids = [oldest[4].id, middle[4].id]
    child_ids = [record[2].id for record in seeded]
    parent_ids = [record[1].id for record in seeded]
    retained_round_result = newest[3].result

    await consumer._cleanup_subagent_data()

    async with subagent_database() as db:
        assert {row.id for row in (await db.execute(select(Subagent))).scalars().all()} == {newest[0].id}
        assert not (await db.execute(select(Subagent).where(Subagent.id.in_(old_subagent_ids)))).scalars().all()
        assert {row.id for row in (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(child_ids)))).scalars().all()} == {newest[2].id}
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(old_child_ids)))).scalars().all()
        assert not (await db.execute(select(SubagentToolCall).where(SubagentToolCall.id.in_(old_call_ids)))).scalars().all()
        assert {row.child_work_id for row in (await db.execute(select(SubagentWork))).scalars().all()} == {newest[2].id}
        assert {row.id for row in (await db.execute(select(SubagentToolCall))).scalars().all()} == {newest[4].id}
        retained_round = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == newest[2].id))).scalars().one()
        assert retained_round.result == retained_round_result

    await consumer._cleanup_subagent_data()

    async with subagent_database() as db:
        assert not (await db.execute(select(Subagent))).scalars().all()
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(child_ids)))).scalars().all()
        assert not (await db.execute(select(SubagentWork))).scalars().all()
        assert not (await db.execute(select(SubagentToolCall))).scalars().all()
        parent_rows = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(parent_ids)).order_by(SessionReplyWorkItem.id))).scalars().all()
        assert [row.id for row in parent_rows] == sorted(parent_ids)
        parent_snapshot = [(row.id, row.status, row.updated_at, row.execution_state) for row in parent_rows]

    await consumer._cleanup_subagent_data()

    async with subagent_database() as db:
        assert not (await db.execute(select(Subagent))).scalars().all()
        assert not (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(child_ids)))).scalars().all()
        assert not (await db.execute(select(SubagentWork))).scalars().all()
        assert not (await db.execute(select(SubagentToolCall))).scalars().all()
        parent_rows = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(parent_ids)).order_by(SessionReplyWorkItem.id))).scalars().all()
        assert [(row.id, row.status, row.updated_at, row.execution_state) for row in parent_rows] == parent_snapshot


@pytest.mark.asyncio
async def test_cleanup_old_rounds_respects_capacity_and_preserves_tool_call_source(subagent_database):
    crud = CRUDSessionReplyWorkItem()
    now = get_local_time()
    round_times = [now - timedelta(hours=4), now - timedelta(hours=3), now - timedelta(hours=2), now - timedelta(hours=1)]

    async with subagent_database() as db:
        subagent, parent, first_child, first_round, _source_call = await _add_finished_subagent(
            db,
            key="history-capacity-source",
            last_called_at=round_times[3],
        )
        first_child.created_at = round_times[0]
        first_child.updated_at = round_times[0]
        first_round.created_at = round_times[0]

        source_message = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_id=first_child.id,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content='{"role":"assistant","tool_calls":[{"id":"history-source-call","name":"file_tool","arguments":{"operation":"read","path":"source.txt"}}]}',
            is_processed=True,
            created_at=round_times[0],
        )
        db.add(source_message)
        await db.flush()
        assert source_message.id is not None

        source_tool_snapshot = {
            "id": source_message.id,
            "role": "assistant",
            "type": "tool_call",
            "tool_calls": [
                {
                    "id": "history-source-call",
                    "name": "file_tool",
                    "arguments": {"operation": "read", "path": "source.txt"},
                }
            ],
        }

        round_rows = [first_child]
        for index, round_time in enumerate(round_times[1:], start=2):
            child, child_created = await crud.enqueue(
                db,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_type=SessionReplyWorkType.AGENT_WORK,
                source_type=SessionReplySourceType.PARENT_WORK,
                source_id=parent.id,
                dedupe_key=f"history-capacity-round-{index}",
                work_category=SessionReplyWorkCategory.SUBAGENT,
                commit=False,
            )
            assert child_created is True and child.id is not None
            child.subagent_id = subagent.id
            child.status = SessionReplyWorkStatus.SUCCEEDED
            child.created_at = round_time
            child.updated_at = round_time
            round_row = SubagentWork(
                subagent_id=subagent.id,
                child_work_id=child.id,
                current_work_id=child.id,
                result={
                    "work_id": child.id,
                    "current_work_id": child.id,
                    "completed": True,
                    "status": "succeeded",
                    "result": {"content": f"history round {index}"},
                    "error": None,
                },
                created_at=round_time,
            )
            db.add(round_row)
            await db.flush()
            round_rows.append(child)

        second_child = round_rows[1]
        third_child = round_rows[2]
        fourth_child = round_rows[3]

        board_post = SubagentBoardPost(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            subagent_id=subagent.id,
            child_work_id=second_child.id,
            stage="progress",
            content="history round 2 progress",
            created_at=round_times[1],
        )
        private_message = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_id=second_child.id,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="history round 2 private message",
            is_processed=False,
            created_at=round_times[1],
        )
        db.add_all([board_post, private_message])
        await db.flush()
        assert board_post.id is not None and private_message.id is not None

        await crud.save_checkpoint(
            db,
            second_child.id,
            {"messages": [{"role": "user", "content": "history round 2 checkpoint"}]},
        )
        await crud.save_checkpoint(db, fourth_child.id, {"messages": [source_tool_snapshot]})
        second_checkpoint_message_id = (
            await db.execute(
                select(SessionReplyCheckpointMessageReference.message_id).where(
                    SessionReplyCheckpointMessageReference.work_id == second_child.id,
                    SessionReplyCheckpointMessageReference.kind == "checkpoint",
                )
            )
        ).scalar_one()
        subagent.latest_child_work_id = fourth_child.id
        subagent.active_child_work_id = None
        await db.flush()
        await db.commit()

        first_child_id = first_child.id
        second_child_id = second_child.id
        third_child_id = third_child.id
        fourth_child_id = fourth_child.id
        source_message_id = source_message.id
        board_post_id = board_post.id
        private_message_id = private_message.id
        subagent_id = subagent.id
        assert first_child_id is not None and second_child_id is not None and third_child_id is not None and fourth_child_id is not None and source_message_id is not None and board_post_id is not None and private_message_id is not None

    async with subagent_database() as db:
        assert await subagent_crud.cleanup_old_rounds(db, retention_limit=2, batch_size=1) == 0

    async with subagent_database() as db:
        assert {row.child_work_id for row in (await db.execute(select(SubagentWork))).scalars().all()} == {first_child_id, second_child_id, third_child_id, fourth_child_id}

    async with subagent_database() as db:
        assert await subagent_crud.cleanup_old_rounds(db, retention_limit=1, batch_size=1) == 1

    async with subagent_database() as db:
        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == second_child_id))).scalars().one_or_none() is None
        assert (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == second_child_id))).scalars().one_or_none() is None
        assert (await db.execute(select(SubagentBoardPost).where(SubagentBoardPost.id == board_post_id))).scalars().one_or_none() is None
        assert (await db.execute(select(Message).where(Message.id == private_message_id))).scalars().one_or_none() is None
        assert (await db.execute(select(SessionReplyWorkCheckpoint).where(SessionReplyWorkCheckpoint.work_id == second_child_id))).scalars().one_or_none() is None
        assert (
            await db.execute(
                select(SessionReplyCheckpointMessageReference).where(
                    SessionReplyCheckpointMessageReference.work_id == second_child_id,
                )
            )
        ).scalars().all() == []
        assert (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id == second_checkpoint_message_id))).scalars().one_or_none() is None

        source_message_row = (await db.execute(select(Message).where(Message.id == source_message_id))).scalars().one()
        assert (source_message_row.work_id, source_message_row.role, source_message_row.type) == (
            first_child_id,
            MessageRole.ASSISTANT,
            MessageType.TOOL_CALL,
        )
        first_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == first_child_id))).scalars().one()
        assert first_round_row.current_work_id == first_child_id

        third_work_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == third_child_id))).scalars().one()
        third_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == third_child_id))).scalars().one()
        assert (third_work_row.status, third_round_row.current_work_id) == (SessionReplyWorkStatus.SUCCEEDED, third_child_id)

        latest_subagent = (await db.execute(select(Subagent).where(Subagent.id == subagent_id))).scalars().one()
        assert (latest_subagent.latest_child_work_id, latest_subagent.active_child_work_id) == (fourth_child_id, None)
        latest_work_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == fourth_child_id))).scalars().one()
        latest_round_row = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == fourth_child_id))).scalars().one()
        assert (latest_work_row.status, latest_round_row.current_work_id) == (SessionReplyWorkStatus.SUCCEEDED, fourth_child_id)
        assert await crud.get_checkpoint(db, fourth_child_id) == {"messages": [source_tool_snapshot]}
