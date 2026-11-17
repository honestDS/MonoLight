import json

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.core.audit.confirmation_lifecycle import stop_interactive_reply
from app.core.crud.audit.audit import audit_crud
from app.core.crud.task.background import background_task_crud
from app.core.tools.background_tasks import ManageBackgroundTasksExecutor
from app.models.audit import AuditExecutionRecord, AuditExecutionStatus, AuditRecord, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from tests.integration.background_tasks.test_background_task_audit_binding import _seed_bound_tasks
from tests.integration.background_tasks.test_background_task_audit_binding import audit_task_database as _audit_task_database

audit_task_database = _audit_task_database


@pytest.mark.asyncio
async def test_completed_bound_task_cannot_be_cancelled_into_unknown(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=tasks[0].id,
            worker_id="worker-a",
            result={"status": "succeeded"},
            auto_reply=False,
        )
        assert await audit_crud.finish_execution_attempt(
            db,
            execution_record_id=executions[0].id,
            status=AuditExecutionStatus.SUCCEEDED,
            result_summary="success",
        )
        assert (
            await audit_crud.finish_execution_round_if_complete(
                db,
                audit_record_id=record_id,
                claim_token="claim-token",
            )
            == AuditRecordStatus.SUCCEEDED
        )
        cancelled = await background_task_crud.cancel_user_task(db, task_id=tasks[0].id, uid="user-1")
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert cancelled.status == BackgroundTaskStatus.SUCCEEDED
    assert stored_execution.status == AuditExecutionStatus.SUCCEEDED
    assert stored_record.status == AuditRecordStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_pending_bound_task_cancellation_closes_audit_as_cancelled(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[0].id).values(status=BackgroundTaskStatus.PENDING, locked_by=None, lock_until=None))
        await db.commit()
        executor = ManageBackgroundTasksExecutor(project_root=".", uid="user-1")
        executor.set_runtime_context(db=db, session_id="session-1")
        cancelled = json.loads(await executor.execute(operation="cancel", task_id=tasks[0].id))
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert cancelled["status"] == "success"
    assert cancelled["task"]["status"] == BackgroundTaskStatus.CANCELLED
    assert stored_execution.status == AuditExecutionStatus.CANCELLED
    assert stored_record.status == AuditRecordStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancelled_execution_cannot_make_a_round_succeed(audit_task_database):
    record_id, _tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)

    async with audit_task_database() as db:
        assert await audit_crud.finish_execution_attempt(
            db,
            execution_record_id=executions[0].id,
            status=AuditExecutionStatus.CANCELLED,
        )
        assert await audit_crud.finish_execution_attempt(
            db,
            execution_record_id=executions[1].id,
            status=AuditExecutionStatus.SUCCEEDED,
            result_summary="success",
        )
        assert (
            await audit_crud.finish_execution_round_if_complete(
                db,
                audit_record_id=record_id,
                claim_token="claim-token",
            )
            == AuditRecordStatus.CANCELLED
        )


@pytest.mark.asyncio
async def test_user_cancellation_marks_bound_execution_unknown(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        executor = ManageBackgroundTasksExecutor(project_root=".", uid="user-1")
        executor.set_runtime_context(db=db, session_id="session-1")
        cancelled = json.loads(await executor.execute(operation="cancel", task_id=tasks[0].id))
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert cancelled["status"] == "success"
    assert cancelled["task"]["status"] == BackgroundTaskStatus.CANCELLED
    assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_statuses", "expected_execution_statuses", "expected_record_status", "pre_finished_execution_index"),
    [
        pytest.param(
            (BackgroundTaskStatus.PENDING, BackgroundTaskStatus.PENDING),
            (AuditExecutionStatus.CANCELLED, AuditExecutionStatus.CANCELLED),
            AuditRecordStatus.CANCELLED,
            None,
            id="pending_tasks",
        ),
        pytest.param(
            (BackgroundTaskStatus.RUNNING, BackgroundTaskStatus.RUNNING),
            (AuditExecutionStatus.EXECUTION_UNKNOWN, AuditExecutionStatus.EXECUTION_UNKNOWN),
            AuditRecordStatus.EXECUTION_UNKNOWN,
            None,
            id="running_tasks",
        ),
        pytest.param(
            (BackgroundTaskStatus.RUNNING, BackgroundTaskStatus.PENDING),
            (AuditExecutionStatus.EXECUTION_UNKNOWN, AuditExecutionStatus.CANCELLED),
            AuditRecordStatus.EXECUTION_UNKNOWN,
            None,
            id="running_then_pending",
        ),
        pytest.param(
            (BackgroundTaskStatus.RUNNING, BackgroundTaskStatus.RUNNING),
            (AuditExecutionStatus.SUCCEEDED, AuditExecutionStatus.EXECUTION_UNKNOWN),
            AuditRecordStatus.EXECUTION_UNKNOWN,
            0,
            id="running_tasks_with_first_execution_succeeded",
        ),
    ],
)
async def test_stop_interactive_reply_closes_bound_tasks_and_audit_round_idempotently(
    audit_task_database,
    task_statuses,
    expected_execution_statuses,
    expected_record_status,
    pre_finished_execution_index,
):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)
    task_ids = [task.id for task in tasks]
    execution_ids = [execution.id for execution in executions]

    async with audit_task_database() as db:
        for task_id, status in zip(task_ids, task_statuses):
            await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == task_id)
                .values(
                    status=status,
                    auto_reply=True,
                    reply_status=BackgroundTaskReplyStatus.RUNNING,
                    reply_locked_by=f"reply-worker-{task_id}",
                    reply_lock_until=9_999_999_999,
                    locked_by="worker-a" if status == BackgroundTaskStatus.RUNNING else None,
                    lock_until=9_999_999_999 if status == BackgroundTaskStatus.RUNNING else None,
                )
            )
        await db.commit()
        if pre_finished_execution_index is not None:
            assert await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution_ids[pre_finished_execution_index],
                status=AuditExecutionStatus.SUCCEEDED,
                result_summary="already finished",
            )

    async def load_state(db):
        task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.id.in_(task_ids)).order_by(BackgroundTask.id).execution_options(populate_existing=True))
        execution_result = await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.id.in_(execution_ids)).order_by(AuditExecutionRecord.id).execution_options(populate_existing=True))
        return list(task_result.scalars().all()), list(execution_result.scalars().all()), await db.get(AuditRecord, record_id)

    async with audit_task_database() as db:
        assert await stop_interactive_reply(db, uid="user-1", session_id="session-1") == 0

    async with audit_task_database() as db:
        first_tasks, first_executions, first_record = await load_state(db)

    assert first_record is not None
    assert [task.status for task in first_tasks] == [BackgroundTaskStatus.CANCELLED, BackgroundTaskStatus.CANCELLED]
    assert [execution.status for execution in first_executions] == list(expected_execution_statuses)
    assert first_record.status == expected_record_status
    assert [(task.session_id, task.audit_record_id, task.audit_execution_record_id) for task in first_tasks] == [("session-1", record_id, execution_id) for execution_id in execution_ids]
    assert all(task.auto_reply is False and task.reply_status == BackgroundTaskReplyStatus.NONE and task.locked_by is None and task.lock_until is None and task.reply_locked_by is None and task.reply_lock_until is None for task in first_tasks)
    for task, execution_id in zip(first_tasks, execution_ids):
        assert task.extra["audit_binding"]["audit_record_id"] == record_id
        assert task.extra["audit_binding"]["audit_execution_record_id"] == execution_id

    first_task_snapshot = [task.model_dump() for task in first_tasks]
    first_execution_snapshot = [(execution.id, execution.status, execution.error, execution.result_summary) for execution in first_executions]
    first_record_snapshot = (first_record.status, first_record.error_reason, first_record.execution_claim_token)

    async with audit_task_database() as db:
        assert await stop_interactive_reply(db, uid="user-1", session_id="session-1") == 0

    async with audit_task_database() as db:
        second_tasks, second_executions, second_record = await load_state(db)
        second_task_snapshot = [task.model_dump() for task in second_tasks]
        second_execution_snapshot = [(execution.id, execution.status, execution.error, execution.result_summary) for execution in second_executions]
        second_record_snapshot = (second_record.status, second_record.error_reason, second_record.execution_claim_token)

        late_finishes = [
            await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution_id,
                status=AuditExecutionStatus.SUCCEEDED,
                result_summary="late success",
            )
            for execution_id in execution_ids
        ]
        final_tasks, final_executions, final_record = await load_state(db)

    assert second_task_snapshot == first_task_snapshot
    assert second_execution_snapshot == first_execution_snapshot
    assert second_record_snapshot == first_record_snapshot
    assert late_finishes == [False, False]
    assert [task.status for task in final_tasks] == [BackgroundTaskStatus.CANCELLED, BackgroundTaskStatus.CANCELLED]
    assert [execution.status for execution in final_executions] == list(expected_execution_statuses)
    assert final_record is not None
    assert final_record.status == expected_record_status
    assert [(task.session_id, task.audit_record_id, task.audit_execution_record_id) for task in final_tasks] == [("session-1", record_id, execution_id) for execution_id in execution_ids]
