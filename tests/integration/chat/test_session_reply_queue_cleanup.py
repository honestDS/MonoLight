import time
from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.models  # noqa: F401
from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem, SessionReplyCleanupResult
from app.core.session_reply_queue import consumer as consumer_module
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkCheckpoint,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from tests.integration.session_reply_queue_test_support import enqueue


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(
        foreign_keys=False,
        name="session-reply-queue-cleanup.sqlite3",
    ) as session_factory:
        async with session_factory() as session:
            session.add_all(
                [
                    Profile(id=1, uid="user-1", name="queue-cleanup", configs={}),
                    ChatSession(session_id="session-1", uid="user-1", profile_id=1),
                ]
            )
            await session.commit()
            yield session


@pytest_asyncio.fixture
async def subagent_database(database_factory) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="subagent-cleanup.sqlite3",
    ) as factory:
        async with factory() as db:
            db.add(User(uid="user-1", username="cleanup-user"))
            await db.flush()
            db.add(Profile(id=1, uid="user-1", name="cleanup", configs={}))
            await db.flush()
            db.add(ChatSession(session_id="session-1", uid="user-1", profile_id=1))
            await db.commit()
        yield factory


@pytest.mark.asyncio
async def test_cleanup_terminal_items_respects_retention_capacity_and_cleans_orphans(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    old_time = get_local_time() - timedelta(hours=25)

    old_terminal = []
    for index, status in enumerate(
        [
            SessionReplyWorkStatus.MERGED,
            SessionReplyWorkStatus.SUCCEEDED,
            SessionReplyWorkStatus.FAILED,
            SessionReplyWorkStatus.CANCELLED,
        ],
        start=1,
    ):
        work = await enqueue(
            crud,
            db_session,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=index,
            dedupe_key=f"foreground-message:{index}",
        )
        work.status = status
        work.updated_at = old_time
        old_terminal.append(work)

    recent_terminal = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=10,
        dedupe_key="foreground-message:10",
    )
    recent_terminal.status = SessionReplyWorkStatus.SUCCEEDED

    old_ready = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=11,
        dedupe_key="foreground-message:11",
    )
    old_ready.updated_at = old_time

    old_running = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=12,
        dedupe_key="foreground-message:12",
    )
    old_running.status = SessionReplyWorkStatus.RUNNING
    old_running.updated_at = old_time

    valid_work = [*old_terminal, recent_terminal, old_ready, old_running]
    for work in valid_work:
        db_session.add(
            SessionReplyStreamEvent(
                work_id=work.id,
                sequence_no=1,
                event={"type": "content", "content": str(work.id)},
            )
        )
    db_session.add(
        SessionReplyStreamEvent(
            work_id=999999,
            sequence_no=1,
            event={"type": "content", "content": "orphan"},
        )
    )
    db_session.add(
        SessionReplySequence(
            session_id="orphan-session",
            next_sequence_no=2,
        )
    )
    db_session.add(
        ChatSession(
            session_id="active-empty-session",
            uid="user-1",
        )
    )
    db_session.add(
        SessionReplySequence(
            session_id="active-empty-session",
            next_sequence_no=2,
        )
    )
    await db_session.commit()

    first_result = await crud.cleanup_terminal_items(db_session, retention_limit=5)

    remaining_work = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_events = list((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    remaining_sequences = list((await db_session.execute(select(SessionReplySequence))).scalars().all())

    assert first_result == SessionReplyCleanupResult(
        work_items=0,
        stream_events=1,
        sequences=1,
    )
    assert first_result.total == 2
    assert {work.id for work in remaining_work} == {work.id for work in valid_work}
    assert {event.work_id for event in remaining_events} == {work.id for work in valid_work}
    assert {sequence.session_id for sequence in remaining_sequences} == {
        "session-1",
        "active-empty-session",
    }

    second_result = await crud.cleanup_terminal_items(db_session, retention_limit=1)

    remaining_work = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_events = list((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    remaining_sequences = list((await db_session.execute(select(SessionReplySequence))).scalars().all())

    assert second_result == SessionReplyCleanupResult(
        work_items=4,
        stream_events=4,
        sequences=0,
    )
    assert second_result.total == 8
    assert {work.id for work in remaining_work} == {
        recent_terminal.id,
        old_ready.id,
        old_running.id,
    }
    assert {event.work_id for event in remaining_events} == {
        recent_terminal.id,
        old_ready.id,
        old_running.id,
    }
    assert {sequence.session_id for sequence in remaining_sequences} == {
        "session-1",
        "active-empty-session",
    }


@pytest.mark.parametrize(
    ("scenario", "retain_candidate"),
    [
        pytest.param("audit_pending", True, id="audit-pending"),
        pytest.param("audit_executing", True, id="audit-executing"),
        pytest.param("audit_passed", True, id="audit-passed"),
        pytest.param("audit_finished", False, id="audit-finished"),
        pytest.param("background_running", True, id="background-running"),
        pytest.param("background_result", True, id="background-result"),
        pytest.param("terminal_shared_pending", True, id="terminal-shared-pending"),
        pytest.param("terminal_consumed", False, id="terminal-consumed"),
        pytest.param("foreign_task", False, id="foreign-task"),
    ],
)
@pytest.mark.asyncio
async def test_cleanup_terminal_items_preserves_recoverable_completed_replies(
    subagent_database,
    scenario,
    retain_candidate,
):
    crud = CRUDSessionReplyWorkItem()
    audit_record_id = None
    source_message_id = None
    background_task_id = None

    async with subagent_database() as db:
        candidate, candidate_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=f"cleanup-protected-candidate:{scenario}",
            dedupe_key=f"cleanup-protected-candidate:{scenario}",
            work_category=SessionReplyWorkCategory.FOREGROUND,
            commit=False,
        )
        assert candidate_created is True and candidate.id is not None
        candidate_id = candidate.id
        candidate.status = SessionReplyWorkStatus.SUCCEEDED
        candidate.locked_by = None
        candidate.lock_until = None

        control = None
        if scenario != "terminal_consumed":
            control, control_created = await crud.enqueue(
                db,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id=f"cleanup-unreferenced-control:{scenario}",
                dedupe_key=f"cleanup-unreferenced-control:{scenario}",
                work_category=SessionReplyWorkCategory.FOREGROUND,
                commit=False,
            )
            assert control_created is True and control.id is not None
            control.status = SessionReplyWorkStatus.FAILED if scenario == "terminal_shared_pending" else SessionReplyWorkStatus.SUCCEEDED
            control.locked_by = None
            control.lock_until = None

        checkpoint_payloads = {
            candidate_id: {"messages": [{"role": "user", "content": f"candidate checkpoint {scenario}"}]},
        }
        if control is not None:
            assert control.id is not None
            checkpoint_payloads[control.id] = {"messages": [{"role": "user", "content": f"control checkpoint {scenario}"}]}
        for work_id, checkpoint in checkpoint_payloads.items():
            await crud.save_checkpoint(db, work_id, checkpoint)
            db.add(
                SessionReplyStreamEvent(
                    work_id=work_id,
                    sequence_no=1,
                    event={"type": "content", "content": str(work_id)},
                )
            )
        await db.flush()
        checkpoint_message_ids = {
            work_id: (
                await db.execute(
                    select(SessionReplyCheckpointMessageReference.message_id).where(
                        SessionReplyCheckpointMessageReference.work_id == work_id,
                        SessionReplyCheckpointMessageReference.kind == "checkpoint",
                    )
                )
            ).scalar_one()
            for work_id in checkpoint_payloads
        }

        if scenario.startswith("audit_"):
            tool_call_id = f"cleanup-audit-call:{scenario}"
            source_message = Message(
                uid="user-1",
                session_id="session-1",
                profile_id=1,
                work_id=candidate_id,
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                content=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id=tool_call_id,
                            name="execute_shell",
                            arguments={"command": "echo cleanup"},
                        )
                    ],
                ).model_dump_json(exclude_none=True),
                is_processed=True,
            )
            db.add(source_message)
            await db.flush()
            assert source_message.id is not None
            source_message_id = source_message.id
            now = get_local_time()
            audit_status = {
                "audit_pending": AuditRecordStatus.PENDING,
                "audit_executing": AuditRecordStatus.EXECUTING,
                "audit_passed": AuditRecordStatus.PASSED,
                "audit_finished": AuditRecordStatus.SUCCEEDED,
            }[scenario]
            audit_record = AuditRecord(
                uid="user-1",
                operator_username="cleanup-user",
                session_id="session-1",
                source="http",
                language="zh",
                execution_scope="task",
                source_work_id=candidate_id,
                status=audit_status,
                source_assistant_message_id=source_message.id,
                working_directory=".",
                round_arguments_hash="a" * 64,
                tool_count=1,
                pending_at=now if scenario in {"audit_pending", "audit_executing"} else None,
                execution_started_at=now if scenario == "audit_executing" else None,
                audited_at=now if scenario == "audit_passed" else None,
                completed_at=now if scenario == "audit_finished" else None,
            )
            db.add(audit_record)
            await db.flush()
            assert audit_record.id is not None
            audit_record_id = audit_record.id
        else:
            task_kwargs = {
                "uid": "user-1",
                "session_id": "session-1",
                "profile_id": 1,
                "tool_call_id": f"cleanup-background-call:{scenario}",
                "tool_name": "terminal_watch" if scenario.startswith("terminal_") else "external_tool",
                "status": BackgroundTaskStatus.RUNNING,
                "arguments": {},
                "auto_reply": False,
                "reply_status": BackgroundTaskReplyStatus.NONE,
                "extra": {"origin_work_id": candidate_id, "origin_work_identity": candidate.dedupe_key},
            }
            if scenario == "background_result":
                task_kwargs.update(
                    status=BackgroundTaskStatus.SUCCEEDED,
                    result={"status": "succeeded", "content": "completed"},
                )
            elif scenario == "terminal_shared_pending":
                assert control is not None and control.id is not None
                task_kwargs.update(
                    status=BackgroundTaskStatus.FAILED,
                    result={"status": "failed", "content": "terminal failed"},
                    error="terminal failed",
                    auto_reply=True,
                    reply_status=BackgroundTaskReplyStatus.PENDING,
                    extra={
                        "auto_terminal_watch": True,
                        "origin_work_id": control.id,
                        "origin_work_identity": control.dedupe_key,
                        "terminal_watch_deliveries": {
                            str(candidate_id): {
                                "identity": candidate.dedupe_key,
                                "consumed": False,
                            }
                        },
                    },
                )
            elif scenario == "terminal_consumed":
                task_kwargs.update(
                    status=BackgroundTaskStatus.SUCCEEDED,
                    result={"status": "succeeded", "content": "terminal completed"},
                    auto_reply=True,
                    reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                    extra={
                        "auto_terminal_watch": True,
                        "origin_work_id": candidate_id,
                        "origin_work_identity": candidate.dedupe_key,
                        "terminal_watch_deliveries": {
                            str(candidate_id): {
                                "identity": candidate.dedupe_key,
                                "consumed": True,
                            }
                        },
                    },
                )
            elif scenario == "foreign_task":
                task_kwargs.update(
                    uid="foreign-user",
                    extra={"origin_work_id": candidate_id, "origin_work_identity": candidate.dedupe_key},
                )

            task = BackgroundTask(**task_kwargs)
            db.add(task)
            await db.flush()
            assert task.id is not None
            background_task_id = task.id
            if scenario == "background_result":
                candidate.execution_state = {
                    **candidate.execution_state,
                    "pending_tools": [
                        {
                            "task_id": task.id,
                            "tool_call_id": task.tool_call_id,
                            "tool_name": task.tool_name,
                        }
                    ],
                }
                db.add(candidate)

        await db.commit()

        result = await crud.cleanup_terminal_items(db, retention_limit=0)
        expected_work_ids = {candidate_id} if retain_candidate else set()
        if control is not None:
            assert control.id is not None
        deleted_count = len(checkpoint_payloads) - len(expected_work_ids)
        assert result == SessionReplyCleanupResult(
            work_items=deleted_count,
            stream_events=deleted_count,
            private_messages=1 if scenario == "audit_finished" else 0,
            checkpoint_messages=deleted_count,
        )

        remaining_work_ids = set((await db.execute(select(SessionReplyWorkItem.id))).scalars().all())
        assert remaining_work_ids == expected_work_ids
        assert {event.work_id for event in (await db.execute(select(SessionReplyStreamEvent))).scalars().all()} == expected_work_ids
        assert {checkpoint.work_id for checkpoint in (await db.execute(select(SessionReplyWorkCheckpoint))).scalars().all()} == expected_work_ids
        assert {reference.work_id for reference in (await db.execute(select(SessionReplyCheckpointMessageReference))).scalars().all()} == expected_work_ids
        assert {row.id for row in (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id.in_(list(checkpoint_message_ids.values()))))).scalars().all()} == {checkpoint_message_ids[work_id] for work_id in expected_work_ids}
        for work_id, checkpoint in checkpoint_payloads.items():
            assert await crud.get_checkpoint(db, work_id) == (checkpoint if work_id in expected_work_ids else None)

        if audit_record_id is not None:
            audit_row = (await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id))).scalars().one()
            assert (audit_row.source_work_id, audit_row.source_assistant_message_id, audit_row.status) == (
                candidate_id,
                source_message_id,
                {
                    "audit_pending": AuditRecordStatus.PENDING,
                    "audit_executing": AuditRecordStatus.EXECUTING,
                    "audit_passed": AuditRecordStatus.PASSED,
                    "audit_finished": AuditRecordStatus.SUCCEEDED,
                }[scenario],
            )
            source_message_rows = (await db.execute(select(Message).where(Message.id == source_message_id))).scalars().all()
            if retain_candidate:
                source_message_row = source_message_rows[0]
                assert (source_message_row.uid, source_message_row.session_id, source_message_row.profile_id, source_message_row.type) == (
                    "user-1",
                    "session-1",
                    1,
                    MessageType.TOOL_CALL,
                )
            else:
                assert source_message_rows == []
        if background_task_id is not None:
            task_row = (await db.execute(select(BackgroundTask).where(BackgroundTask.id == background_task_id))).scalars().one()
            assert task_row.id == background_task_id


@pytest.mark.asyncio
async def test_cleanup_scheduled_private_messages_is_bounded_and_waits_for_confirmed_reply(subagent_database):
    crud = CRUDSessionReplyWorkItem()
    worker_id = "scheduled-confirmation-worker"
    now = get_local_time()
    root_time = now - timedelta(hours=2)
    followup_time = now - timedelta(hours=1)
    checkpoint = {"messages": [{"role": "user", "content": "scheduled checkpoint"}]}
    tool_call_id = "scheduled-cleanup-tool-call"

    async with subagent_database() as db:
        trigger = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.SCHEDULED_TASK_TRIGGER,
            content=InternalMessage(
                role=MessageRole.USER,
                type=MessageType.SCHEDULED_TASK_TRIGGER,
                content="scheduled cleanup",
            ).model_dump_json(exclude_none=True),
            is_processed=True,
            created_at=root_time,
        )
        db.add(trigger)
        await db.flush()
        assert trigger.id is not None

        root, root_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.AGENT_WORK,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id=trigger.id,
            dedupe_key="scheduled-cleanup-root",
            work_category=SessionReplyWorkCategory.SCHEDULED_TASK,
            commit=False,
        )
        assert root_created is True and root.id is not None
        trigger.work_id = root.id
        root.status = SessionReplyWorkStatus.SUCCEEDED
        root.created_at = root_time
        root.updated_at = root_time

        private_message_base = {
            "uid": "user-1",
            "session_id": "session-1",
            "profile_id": 1,
            "work_id": root.id,
            "is_processed": True,
        }
        tool_call = Message(
            **private_message_base,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=InternalMessage(
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                tool_calls=[
                    InternalToolCall(
                        id=tool_call_id,
                        name="execute_shell",
                        arguments={"command": "echo cleanup"},
                    )
                ],
            ).model_dump_json(exclude_none=True),
            created_at=root_time + timedelta(seconds=1),
        )
        tool_result = Message(
            **private_message_base,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=InternalMessage(
                role=MessageRole.TOOL,
                type=MessageType.TOOL_RESULT,
                content="cleanup complete",
                tool_call_id=tool_call_id,
                tool_execution_status="succeeded",
            ).model_dump_json(exclude_none=True),
            created_at=root_time + timedelta(seconds=2),
        )
        public_result = Message(
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="public scheduled result",
            is_processed=True,
            created_at=followup_time,
        )
        db.add_all(
            [
                tool_call,
                tool_result,
                public_result,
                SessionReplyStreamEvent(
                    work_id=root.id,
                    sequence_no=1,
                    event={"type": "content", "content": "scheduled event 1"},
                ),
                SessionReplyStreamEvent(
                    work_id=root.id,
                    sequence_no=2,
                    event={"type": "content", "content": "scheduled event 2"},
                ),
            ]
        )
        await db.flush()
        assert tool_call.id is not None and tool_result.id is not None and public_result.id is not None

        audit_record = AuditRecord(
            uid="user-1",
            operator_username="cleanup-user",
            session_id="session-1",
            source="http",
            language="zh",
            execution_scope="task",
            source_work_id=root.id,
            status=AuditRecordStatus.SUCCEEDED,
            source_assistant_message_id=tool_call.id,
            working_directory=".",
            round_arguments_hash="c" * 64,
            tool_count=1,
            created_at=root_time,
            updated_at=root_time,
            completed_at=root_time,
        )
        db.add(audit_record)
        await db.flush()
        assert audit_record.id is not None

        confirmed, confirmed_created = await crud.enqueue(
            db,
            uid="user-1",
            session_id="session-1",
            profile_id=1,
            work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            source_type=SessionReplySourceType.AUDIT_RECORD,
            source_id=audit_record.id,
            dedupe_key="scheduled-cleanup-confirmed",
            work_category=SessionReplyWorkCategory.SCHEDULED_TASK,
            commit=False,
        )
        assert confirmed_created is True and confirmed.id is not None
        confirmed.status = SessionReplyWorkStatus.RUNNING
        confirmed.locked_by = worker_id
        confirmed.lock_until = int(time.time()) + 300
        confirmed.created_at = followup_time
        confirmed.updated_at = followup_time

        await crud.save_checkpoint(db, root.id, checkpoint)
        checkpoint_message_id = (
            await db.execute(
                select(SessionReplyCheckpointMessageReference.message_id).where(
                    SessionReplyCheckpointMessageReference.work_id == root.id,
                    SessionReplyCheckpointMessageReference.kind == "checkpoint",
                )
            )
        ).scalar_one()
        await db.commit()

        private_messages = [trigger, tool_call, tool_result]
        private_message_ids = [message.id for message in private_messages]
        root_id = root.id
        confirmed_id = confirmed.id
        audit_record_id = audit_record.id
        public_result_id = public_result.id
        assert all(value is not None for value in [*private_message_ids, root_id, confirmed_id, audit_record_id, public_result_id])

        first_result = await crud.cleanup_terminal_items(db, retention_limit=1, batch_size=2)
        assert first_result == SessionReplyCleanupResult()
        assert first_result.total <= 2
        assert await crud.get_message_work_id(db, confirmed) == confirmed.id
        assert (await db.execute(select(SessionReplyWorkItem.id).where(SessionReplyWorkItem.id == root_id))).scalar_one() == root_id
        assert set((await db.execute(select(Message.id).where(Message.work_id == root_id))).scalars().all()) == set(private_message_ids)
        assert {event.sequence_no for event in (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == root_id))).scalars().all()} == {1, 2}
        assert await crud.get_checkpoint(db, root_id) == checkpoint

        assert await crud.mark_terminal(
            db,
            work_id=confirmed_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
        )

        expected_results = [
            SessionReplyCleanupResult(stream_events=2),
            SessionReplyCleanupResult(private_messages=2),
            SessionReplyCleanupResult(work_items=1, private_messages=1),
            SessionReplyCleanupResult(checkpoint_messages=1),
        ]
        for expected_result in expected_results:
            result = await crud.cleanup_terminal_items(db, retention_limit=1, batch_size=2)
            assert result == expected_result
            assert result.total <= 2

        assert (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == root_id))).scalars().one_or_none() is None
        assert not (await db.execute(select(Message).where(Message.id.in_(private_message_ids)))).scalars().all()
        assert (await db.execute(select(Message).where(Message.id == public_result_id))).scalars().one().work_id is None
        audit_row = (await db.execute(select(AuditRecord).where(AuditRecord.id == audit_record_id))).scalars().one()
        assert (audit_row.source_work_id, audit_row.source_assistant_message_id, audit_row.status) == (
            root_id,
            tool_call.id,
            AuditRecordStatus.SUCCEEDED,
        )
        confirmed_row = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == confirmed_id))).scalars().one()
        assert (confirmed_row.status, confirmed_row.source_id, confirmed_row.locked_by) == (
            SessionReplyWorkStatus.SUCCEEDED,
            str(audit_record_id),
            None,
        )
        assert (await db.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id == checkpoint_message_id))).scalars().one_or_none() is None


@pytest.mark.asyncio
async def test_cleanup_terminal_items_deletes_stream_events_before_work_in_batches(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    same_updated_at = get_local_time() - timedelta(hours=25)
    works = []
    for index in range(3):
        work = await enqueue(
            crud,
            db_session,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=index + 1,
            dedupe_key=f"foreground-message:{index + 1}",
        )
        work.status = SessionReplyWorkStatus.SUCCEEDED
        work.updated_at = same_updated_at
        works.append(work)
        for sequence_no in range(2):
            db_session.add(
                SessionReplyStreamEvent(
                    work_id=work.id,
                    sequence_no=sequence_no + 1,
                    event={"type": "content", "content": str(work.id)},
                )
            )
    await db_session.commit()

    oldest, middle, newest = works

    first_result = await crud.cleanup_terminal_items(
        db_session,
        retention_limit=1,
        batch_size=2,
    )

    remaining_work = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_events = list((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    assert first_result == SessionReplyCleanupResult(
        work_items=0,
        stream_events=2,
        sequences=0,
    )
    assert first_result.total == 2
    assert {work.id for work in remaining_work} == {oldest.id, middle.id, newest.id}
    assert {(event.work_id, event.sequence_no) for event in remaining_events} == {
        (middle.id, 1),
        (middle.id, 2),
        (newest.id, 1),
        (newest.id, 2),
    }

    second_result = await crud.cleanup_terminal_items(
        db_session,
        retention_limit=1,
        batch_size=2,
    )

    remaining_work = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_events = list((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    assert second_result == SessionReplyCleanupResult(
        work_items=0,
        stream_events=2,
        sequences=0,
    )
    assert second_result.total == 2
    assert {work.id for work in remaining_work} == {oldest.id, middle.id, newest.id}
    assert {(event.work_id, event.sequence_no) for event in remaining_events} == {
        (newest.id, 1),
        (newest.id, 2),
    }

    third_result = await crud.cleanup_terminal_items(
        db_session,
        retention_limit=1,
        batch_size=2,
    )

    remaining_work = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_events = list((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    assert third_result == SessionReplyCleanupResult(
        work_items=2,
        stream_events=0,
        sequences=0,
    )
    assert third_result.total == 2
    assert {work.id for work in remaining_work} == {newest.id}
    assert {(event.work_id, event.sequence_no) for event in remaining_events} == {
        (newest.id, 1),
        (newest.id, 2),
    }


@pytest.mark.asyncio
async def test_consumer_cleanup_processes_multiple_batches_and_yields(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    cleanup_calls = []
    sleep_calls = []
    results = iter(
        [
            SessionReplyCleanupResult(work_items=100, stream_events=400),
            SessionReplyCleanupResult(work_items=50, stream_events=25, sequences=5),
        ]
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_terminal_items(db, *, batch_size):
        cleanup_calls.append((db, batch_size))
        return next(results)

    async def sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        consumer_module.session_reply_work_item_crud,
        "cleanup_terminal_items",
        cleanup_terminal_items,
    )
    monkeypatch.setattr(consumer_module.asyncio, "sleep", sleep)

    await consumer._cleanup_terminal_items()

    assert [batch_size for _db, batch_size in cleanup_calls] == [500, 500]
    assert sleep_calls == [0]


@pytest.mark.asyncio
async def test_consumer_cleanup_stops_at_per_run_limit(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    cleanup_batch_sizes = []
    sleep_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_terminal_items(db, *, batch_size):
        cleanup_batch_sizes.append(batch_size)
        return SessionReplyCleanupResult(stream_events=batch_size)

    async def sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        consumer_module.session_reply_work_item_crud,
        "cleanup_terminal_items",
        cleanup_terminal_items,
    )
    monkeypatch.setattr(consumer_module.asyncio, "sleep", sleep)

    await consumer._cleanup_terminal_items()

    assert sum(cleanup_batch_sizes) == consumer_module.SESSION_REPLY_CLEANUP_MAX_ITEMS_PER_RUN
    assert cleanup_batch_sizes == [consumer_module.SESSION_REPLY_CLEANUP_BATCH_SIZE] * 10
    assert sleep_calls == [0] * 9
