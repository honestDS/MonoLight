import pytest
from sqlalchemy import update
from sqlmodel import select

from app.core.crud.task.background import background_task_crud
from app.models.audit import AuditExecutionRecord, AuditExecutionStatus, AuditRecord, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.providers.database.time import get_database_timestamp
from tests.integration.background_tasks.test_background_task_audit_binding import _seed_bound_tasks
from tests.integration.background_tasks.test_background_task_audit_binding import audit_task_database as _audit_task_database

audit_task_database = _audit_task_database


@pytest.mark.asyncio
async def test_session_cleanup_marks_expired_bound_running_task_unknown_before_retaining_it(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        database_now = await get_database_timestamp(db)
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[0].id).values(lock_until=database_now - 1))
        await db.commit()
        retained = await background_task_crud.cleanup_by_session(db, session_id="session-1", uid="user-1")

    async with audit_task_database() as db:
        stored_task = await db.get(BackgroundTask, tasks[0].id)
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert retained == 0
    assert stored_task.status == BackgroundTaskStatus.CANCELLED
    assert stored_task.session_id == f"deleted-session:{tasks[0].id}"
    assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
async def test_session_cleanup_closes_mixed_bound_tasks_and_is_idempotent(audit_task_database):
    """验证运行任务在前、待执行任务在后的同轮清理及重复清理终态。"""
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)

    async with audit_task_database() as db:
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[1].id).values(status=BackgroundTaskStatus.PENDING, locked_by=None, lock_until=None))
        await db.commit()
        stored_tasks = [await db.get(BackgroundTask, task.id) for task in tasks]
        assert [task.status for task in stored_tasks] == [BackgroundTaskStatus.RUNNING, BackgroundTaskStatus.PENDING]
        assert await background_task_crud.cleanup_by_session(db, session_id="session-1", uid="user-1") == 0

    async with audit_task_database() as db:
        stored_record = await db.get(AuditRecord, record_id)
        stored_executions = [await db.get(AuditExecutionRecord, execution.id) for execution in executions]
        remaining_tasks = list((await db.execute(select(BackgroundTask))).scalars().all())

        assert stored_record is not None
        assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN
        assert [execution.status for execution in stored_executions] == [AuditExecutionStatus.EXECUTION_UNKNOWN, AuditExecutionStatus.CANCELLED]
        assert all(task.status not in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING} for task in remaining_tasks)
        assert len(remaining_tasks) == 1
        assert remaining_tasks[0].status == BackgroundTaskStatus.CANCELLED
        assert remaining_tasks[0].session_id == f"deleted-session:{tasks[0].id}"

        assert await background_task_crud.cleanup_by_session(db, session_id="session-1", uid="user-1") == 0

    async with audit_task_database() as db:
        stored_record = await db.get(AuditRecord, record_id)
        stored_executions = [await db.get(AuditExecutionRecord, execution.id) for execution in executions]
        remaining_tasks = list((await db.execute(select(BackgroundTask))).scalars().all())

    assert stored_record is not None
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert [execution.status for execution in stored_executions] == [AuditExecutionStatus.EXECUTION_UNKNOWN, AuditExecutionStatus.CANCELLED]
    assert len(remaining_tasks) == 1
    assert remaining_tasks[0].status == BackgroundTaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_session_cleanup_does_not_rollback_running_close_when_pending_audit_is_already_closed(audit_task_database):
    """验证待执行审计已终止时不会回滚此前完成的运行任务关闭。"""
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)

    async with audit_task_database() as db:
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[1].id).values(status=BackgroundTaskStatus.PENDING, locked_by=None, lock_until=None))
        await db.execute(update(AuditExecutionRecord).where(AuditExecutionRecord.id == executions[1].id).values(status=AuditExecutionStatus.CANCELLED))
        await db.commit()
        assert await background_task_crud.cleanup_by_session(db, session_id="session-1", uid="user-1") == 0

    async with audit_task_database() as db:
        stored_record = await db.get(AuditRecord, record_id)
        stored_executions = [await db.get(AuditExecutionRecord, execution.id) for execution in executions]
        remaining_tasks = list((await db.execute(select(BackgroundTask))).scalars().all())

    assert stored_record is not None
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert [execution.status for execution in stored_executions] == [AuditExecutionStatus.EXECUTION_UNKNOWN, AuditExecutionStatus.CANCELLED]
    assert all(task.status not in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING} for task in remaining_tasks)
    assert {task.status for task in remaining_tasks} == {BackgroundTaskStatus.CANCELLED}
