import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.api.v1 import chat as chat_api
from app.core import log as log_module
from app.core.background_tasks import manager as manager_module
from app.core.background_tasks import runner as runner_module
from app.core.crud.audit import audit_crud
from app.core.crud.background_task import background_task_crud
from app.core.dispatchers import background as background_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.models.audit import AuditExecutionRecord, AuditExecutionStatus, AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.background_task import BackgroundTask, BackgroundTaskResponse, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.providers.database.time import get_database_timestamp


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class CapturingLog:
    def bind(self, **kwargs):
        return self

    def info(self, message):
        return None

    def warning(self, message):
        return None

    def error(self, message, *, exc_info=False):
        return None


class CapturingToolLog:
    def __init__(self):
        self.messages = []

    def bind(self, **kwargs):
        return self

    def info(self, message):
        self.messages.append(message)


@pytest.fixture
async def audit_task_database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'background-audit.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _seed_bound_tasks(session_factory, count=1):
    async with session_factory() as db:
        record = AuditRecord(
            uid="user-1",
            operator_username="tester",
            session_id="session-1",
            source="web",
            language="zh",
            status=AuditRecordStatus.EXECUTING,
            source_assistant_message_id=1,
            working_directory=".",
            round_arguments_hash="a" * 64,
            tool_count=count,
            execution_claim_token="claim-token",
        )
        db.add(record)
        await db.flush()
        tasks = []
        executions = []
        for index in range(count):
            detail = AuditToolDetail(
                audit_record_id=record.id,
                original_tool_call_id=f"original-{index}",
                turn_index=index,
                tool_name="bound-tool",
                conclusion=AuditToolConclusion.PASSED,
                score=1,
                reason="test",
                arguments_hash="b" * 64,
                arguments_summary="{}",
            )
            db.add(detail)
            await db.flush()
            execution = AuditExecutionRecord(
                audit_record_id=record.id,
                audit_tool_detail_id=detail.id,
                attempt_no=1,
                claim_token="claim-token",
                execution_node="test-node",
                new_tool_call_id=f"new-{index}",
            )
            db.add(execution)
            await db.flush()
            task = BackgroundTask(
                uid="user-1",
                session_id="session-1",
                profile_id=3,
                tool_call_id=f"new-{index}",
                tool_name="bound-tool",
                status=BackgroundTaskStatus.RUNNING,
                arguments={"value": index},
                auto_reply=False,
                audit_record_id=record.id,
                audit_execution_record_id=execution.id,
                extra={
                    "audit_binding": {
                        "audit_record_id": record.id,
                        "audit_execution_record_id": execution.id,
                        "claim_token": "claim-token",
                        "handoff_state": "persisted",
                    }
                },
                locked_by="worker-a",
                lock_until=9_999_999_999,
            )
            db.add(task)
            tasks.append(task)
            executions.append(execution)
        await db.commit()
        return record.id, tasks, executions


@pytest.mark.asyncio
async def test_runner_updates_bound_execution_only_after_real_tool_call_and_sanitizes_result(audit_task_database, monkeypatch):
    async with audit_task_database() as db:
        record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
        task_id = tasks[0].id
        execution_id = executions[0].id
        observed_statuses = []

        class Executor:
            def __init__(self, **kwargs):
                return None

            def set_config(self, cfg):
                return None

            def set_runtime_context(self, **kwargs):
                return None

            async def execute(self, **kwargs):
                current = await db.get(AuditExecutionRecord, execution_id)
                observed_statuses.append(current.status)
                return json.dumps({"status": "success", "token": "SECRET_VALUE", "stdout": "visible output"})

        monkeypatch.setattr(runner_module, "AsyncSessionLocal", lambda: SessionContext(db))
        monkeypatch.setattr(runner_module.profile_crud, "get", _get_profile)
        monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "bound-tool", Executor)

        assert await runner_module._execute_claimed_background_task(task_id, "worker-a", CapturingLog()) is True
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, record_id)
        stored_task = await db.get(BackgroundTask, task_id)

        assert observed_statuses == [AuditExecutionStatus.RUNNING]
        assert stored_execution.status == AuditExecutionStatus.SUCCEEDED
        assert stored_record.status == AuditRecordStatus.SUCCEEDED
        assert stored_task.status == BackgroundTaskStatus.SUCCEEDED
        assert "SECRET_VALUE" not in (stored_execution.result_summary or "")
        assert "visible output" not in (stored_execution.result_summary or "")
        assert "SECRET_VALUE" not in json.dumps(stored_task.result, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "execution_status", "record_status"),
    [
        (json.dumps({"status": "failed", "error": "provider rejected", "password": "SECRET_VALUE"}), AuditExecutionStatus.FAILED, AuditRecordStatus.FAILED),
        ({"status": "failed", "error": "provider rejected", "password": "SECRET_VALUE"}, AuditExecutionStatus.FAILED, AuditRecordStatus.FAILED),
    ],
)
async def test_runner_persists_explicit_tool_failure(audit_task_database, monkeypatch, result, execution_status, record_status):
    async with audit_task_database() as db:
        record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)

        class Executor:
            def __init__(self, **kwargs):
                return None

            def set_config(self, cfg):
                return None

            def set_runtime_context(self, **kwargs):
                return None

            async def execute(self, **kwargs):
                return result

        monkeypatch.setattr(runner_module, "AsyncSessionLocal", lambda: SessionContext(db))
        monkeypatch.setattr(runner_module.profile_crud, "get", _get_profile)
        monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "bound-tool", Executor)

        assert await runner_module._execute_claimed_background_task(tasks[0].id, "worker-a", CapturingLog()) is True
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)
        assert stored_execution.status == execution_status
        assert stored_record.status == record_status
        assert "SECRET_VALUE" not in (stored_execution.result_summary or "")


@pytest.mark.asyncio
async def test_executor_exception_marks_bound_audit_unknown_and_never_requeues(audit_task_database, monkeypatch):
    async with audit_task_database() as db:
        record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)

        class Executor:
            def __init__(self, **kwargs):
                return None

            def set_config(self, cfg):
                return None

            def set_runtime_context(self, **kwargs):
                return None

            async def execute(self, **kwargs):
                raise RuntimeError("secret=SECRET_VALUE")

        monkeypatch.setattr(runner_module, "AsyncSessionLocal", lambda: SessionContext(db))
        monkeypatch.setattr(runner_module.profile_crud, "get", _get_profile)
        monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "bound-tool", Executor)

        assert await runner_module._execute_claimed_background_task(tasks[0].id, "worker-a", CapturingLog()) is False
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)
        stored_task = await db.get(BackgroundTask, tasks[0].id)
        assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN
        assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN
        assert stored_task.status == BackgroundTaskStatus.FAILED
        assert stored_task.extra["audit_execution_unknown"] is True

        current_time = await get_database_timestamp(db)
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[0].id).values(status=BackgroundTaskStatus.RUNNING, locked_by="stale-worker", lock_until=current_time - 1))
        await db.commit()
        assert await background_task_crud.requeue_expired_running(db, profile_id=3, max_attempts_error="retry") == []
        assert await background_task_crud.try_claim(db, task_id=tasks[0].id, worker_id="worker-b") is None


@pytest.mark.asyncio
async def test_unknown_target_freezes_the_whole_audit_round_and_blocks_late_success(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)
    async with audit_task_database() as db:
        marked = await audit_crud.mark_execution_unknown(
            db,
            audit_record_id=record_id,
            execution_record_id=executions[0].id,
            claim_token="claim-token",
            error_reason="result unknown",
        )
        late_finish = await audit_crud.finish_execution_attempt(
            db,
            execution_record_id=executions[1].id,
            status=AuditExecutionStatus.SUCCEEDED,
            result_summary="must not be accepted",
        )
        late_round_finish = await audit_crud.finish_execution_round_if_complete(
            db,
            audit_record_id=record_id,
            claim_token="claim-token",
        )
        stored_record = await db.get(AuditRecord, record_id)
        stored_executions = [await db.get(AuditExecutionRecord, item.id) for item in executions]
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[1].id).values(status=BackgroundTaskStatus.PENDING, locked_by=None, lock_until=None))
        await db.commit()
        blocked_claim = await background_task_crud.try_claim(db, task_id=tasks[1].id, worker_id="worker-b")

    assert marked is True
    assert late_finish is False
    assert late_round_finish is None
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert all(item.status == AuditExecutionStatus.EXECUTION_UNKNOWN for item in stored_executions)
    assert blocked_claim is None


@pytest.mark.asyncio
async def test_stale_owner_cannot_release_bound_task_or_mark_audit_unknown(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        released = await background_task_crud.release_claim(db, task_id=tasks[0].id, worker_id="worker-b")
        stored_task = await db.get(BackgroundTask, tasks[0].id)
        stored_record = await db.get(AuditRecord, record_id)
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)

    assert released is False
    assert stored_task.status == BackgroundTaskStatus.RUNNING
    assert stored_task.locked_by == "worker-a"
    assert stored_record.status == AuditRecordStatus.EXECUTING
    assert stored_execution.status == AuditExecutionStatus.RUNNING


@pytest.mark.asyncio
async def test_old_lease_release_after_renewal_does_not_mark_bound_task_unknown(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        database_now = await get_database_timestamp(db)
        await db.execute(update(BackgroundTask).where(BackgroundTask.id == tasks[0].id).values(lock_until=database_now + 60))
        await db.commit()
        stale_task = await background_task_crud.get(db, tasks[0].id)
        stale_lock_until = stale_task.lock_until
        assert await background_task_crud.renew_lease(db, task_id=tasks[0].id, worker_id="worker-a", lease_seconds=300)
        released = await background_task_crud.release_claim(
            db,
            task_id=tasks[0].id,
            worker_id="worker-a",
            expected_lock_until=stale_lock_until,
        )
        stored_task = await background_task_crud.get(db, tasks[0].id)
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert released is False
    assert stored_task.status == BackgroundTaskStatus.RUNNING
    assert stored_execution.status == AuditExecutionStatus.RUNNING
    assert stored_record.status == AuditRecordStatus.EXECUTING


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
        cancelled = await background_task_crud.cancel_user_task(db, task_id=tasks[0].id, uid="user-1")
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert cancelled.status == BackgroundTaskStatus.CANCELLED
    assert stored_execution.status == AuditExecutionStatus.CANCELLED
    assert stored_record.status == AuditRecordStatus.CANCELLED


@pytest.mark.asyncio
async def test_concurrent_round_finish_has_one_terminal_winner(audit_task_database):
    record_id, _tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)
    async with audit_task_database() as db:
        for execution in executions:
            await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution.id,
                status=AuditExecutionStatus.SUCCEEDED,
                result_summary="success",
            )

    async def finish_once():
        async with audit_task_database() as db:
            return await audit_crud.finish_execution_round_if_complete(
                db,
                audit_record_id=record_id,
                claim_token="claim-token",
            )

    results = await asyncio.gather(finish_once(), finish_once())
    async with audit_task_database() as db:
        stored_record = await db.get(AuditRecord, record_id)

    assert results.count(AuditRecordStatus.SUCCEEDED) == 1
    assert results.count(None) == 1
    assert stored_record.status == AuditRecordStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_concurrent_last_execution_finish_always_closes_the_round(audit_task_database):
    record_id, _tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)

    async def finish_execution(execution_id):
        async with audit_task_database() as db:
            finished = await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution_id,
                status=AuditExecutionStatus.SUCCEEDED,
                result_summary="success",
            )
            round_status = await audit_crud.finish_execution_round_if_complete(
                db,
                audit_record_id=record_id,
                claim_token="claim-token",
            )
            return finished, round_status

    results = await asyncio.gather(*(finish_execution(execution.id) for execution in executions))

    async with audit_task_database() as db:
        stored_record = await db.get(AuditRecord, record_id)
        stored_executions = [await db.get(AuditExecutionRecord, execution.id) for execution in executions]

    assert all(finished for finished, _round_status in results)
    assert [round_status for _finished, round_status in results].count(AuditRecordStatus.SUCCEEDED) == 1
    assert [round_status for _finished, round_status in results].count(None) == 1
    assert stored_record.status == AuditRecordStatus.SUCCEEDED
    assert all(execution.status == AuditExecutionStatus.SUCCEEDED for execution in stored_executions)


@pytest.mark.asyncio
async def test_failed_execution_waits_for_the_other_task_then_closes_failed_round(audit_task_database):
    record_id, _tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)

    async with audit_task_database() as db:
        assert await audit_crud.finish_execution_attempt(
            db,
            execution_record_id=executions[0].id,
            status=AuditExecutionStatus.FAILED,
            error="tool failed",
        )
        assert (
            await audit_crud.finish_execution_round_if_complete(
                db,
                audit_record_id=record_id,
                claim_token="claim-token",
            )
            is None
        )
        stored_record = await db.get(AuditRecord, record_id)
        stored_first = await db.get(AuditExecutionRecord, executions[0].id)
        stored_second = await db.get(AuditExecutionRecord, executions[1].id)
        assert stored_record.status == AuditRecordStatus.EXECUTING
        assert stored_first.status == AuditExecutionStatus.FAILED
        assert stored_second.status == AuditExecutionStatus.RUNNING

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
            == AuditRecordStatus.FAILED
        )

        stored_record = await db.get(AuditRecord, record_id)

    assert stored_record.status == AuditRecordStatus.FAILED


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
        cancelled = await background_task_crud.cancel_user_task(db, task_id=tasks[0].id, uid="user-1")
        stored_execution = await db.get(AuditExecutionRecord, executions[0].id)
        stored_record = await db.get(AuditRecord, record_id)

    assert cancelled.status == BackgroundTaskStatus.CANCELLED
    assert stored_execution.status == AuditExecutionStatus.EXECUTION_UNKNOWN
    assert stored_record.status == AuditRecordStatus.EXECUTION_UNKNOWN


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


@pytest.mark.asyncio
async def test_multiple_bound_background_tasks_close_one_audit_round_only_after_all_finish(audit_task_database, monkeypatch):
    async with audit_task_database() as db:
        record_id, tasks, executions = await _seed_bound_tasks(audit_task_database, count=2)

        class Executor:
            def __init__(self, **kwargs):
                return None

            def set_config(self, cfg):
                return None

            def set_runtime_context(self, **kwargs):
                return None

            async def execute(self, **kwargs):
                return json.dumps({"status": "success"})

        monkeypatch.setattr(runner_module, "AsyncSessionLocal", lambda: SessionContext(db))
        monkeypatch.setattr(runner_module.profile_crud, "get", _get_profile)
        monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "bound-tool", Executor)

        assert await runner_module._execute_claimed_background_task(tasks[0].id, "worker-a", CapturingLog()) is True
        assert (await db.get(AuditRecord, record_id)).status == AuditRecordStatus.EXECUTING
        assert await runner_module._execute_claimed_background_task(tasks[1].id, "worker-a", CapturingLog()) is True
        assert (await db.get(AuditRecord, record_id)).status == AuditRecordStatus.SUCCEEDED
        for execution in executions:
            assert (await db.get(AuditExecutionRecord, execution.id)).status == AuditExecutionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_duplicate_audited_submission_returns_the_same_persisted_task(audit_task_database):
    record_id, tasks, executions = await _seed_bound_tasks(audit_task_database)
    async with audit_task_database() as db:
        manager = manager_module.BackgroundTaskManager()
        profile = SimpleNamespace(id=3)
        first = await manager.submit(
            db,
            uid="user-1",
            session_id="session-1",
            profile=profile,
            tool_call_id="new-0",
            tool_name="bound-tool",
            arguments={"value": 0},
        )
        second = await manager.submit(
            db,
            uid="user-1",
            session_id="session-1",
            profile=profile,
            tool_call_id="new-0",
            tool_name="bound-tool",
            arguments={"value": 0},
        )
        task_rows = (await db.execute(select(BackgroundTask).where(BackgroundTask.audit_execution_record_id == executions[0].id))).scalars().all()

    assert first.id == second.id == tasks[0].id
    assert len(task_rows) == 1


def _profile():
    return SimpleNamespace(id=3, uid="user-1", configs={})


def test_background_task_response_excludes_internal_arguments_and_binding():
    task = BackgroundTask(
        id=1,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_call_id="call-1",
        tool_name="bound-tool",
        arguments={"password": "SECRET_VALUE"},
        extra={
            "submission_context": [{"role": "user", "content": "secret context"}],
            "audit_binding": {"claim_token": "claim-token"},
        },
    )

    payload = BackgroundTaskResponse.model_validate(task).model_dump(mode="json")

    assert set(payload) == {
        "id",
        "uid",
        "session_id",
        "profile_id",
        "tool_call_id",
        "tool_name",
        "status",
        "result",
        "error",
        "auto_reply",
        "reply_status",
        "attempt_count",
        "created_at",
        "started_at",
        "finished_at",
    }
    assert "arguments" not in payload
    assert "extra" not in payload
    assert "audit_record_id" not in payload
    assert "audit_execution_record_id" not in payload
    assert "SECRET_VALUE" not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_background_task_api_routes_use_the_restricted_response(monkeypatch):
    task = BackgroundTask(
        id=2,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_call_id="call-2",
        tool_name="execute_shell",
        arguments={"command": "cat secret.txt"},
        extra={
            "submission_context": [{"role": "user", "content": "private context"}],
            "audit_binding": {"audit_record_id": 7, "claim_token": "claim-token"},
        },
    )

    async def list_user_tasks(_db, **kwargs):
        assert kwargs["uid"] == "user-1"
        return [task]

    async def get_user_task(_db, **kwargs):
        assert kwargs == {"task_id": 2, "uid": "user-1"}
        return task

    monkeypatch.setattr(chat_api.background_task_crud, "list_user_tasks", list_user_tasks)
    monkeypatch.setattr(chat_api.background_task_crud, "get_user_task", get_user_task)
    current_user = SimpleNamespace(uid="user-1")

    list_response = await chat_api.list_background_tasks(page=1, size=20, db=object(), current_user=current_user)
    detail_response = await chat_api.get_background_task(task_id=2, db=object(), current_user=current_user)

    for response in (list_response, detail_response):
        serialized = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
        assert "private context" not in serialized
        assert "claim-token" not in serialized
        assert "cat secret.txt" not in serialized
        assert "extra" not in serialized
        assert "arguments" not in serialized


def test_tool_result_log_is_sanitized_and_truncated(monkeypatch):
    logger = CapturingToolLog()
    monkeypatch.setattr(log_module, "logger", logger)

    log_module.LogManager.log_tool_result(
        1,
        json.dumps({"token": "SECRET_VALUE", "stdout": "raw-output-" * 500}),
        "session-1",
        "user-1",
    )

    assert len(logger.messages) == 1
    assert "SECRET_VALUE" not in logger.messages[0]
    assert len(logger.messages[0]) < 2200


@pytest.mark.asyncio
async def test_background_without_audit_configuration_executes_tool_without_audit_binding(monkeypatch):
    profile = SimpleNamespace(id=3)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        security=SimpleNamespace(audit_channel_id=None, audit_model_id=None),
        tool=SimpleNamespace(max_parallel_tools=5),
    )
    tool_call = InternalToolCall(
        id="call-send",
        name="send_file_to_user",
        arguments={"files": [{"path": "/tmp/generated.png"}]},
    )
    responses = [
        InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]), model="chat-model"),
        InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="后台总结"), model="chat-model"),
    ]
    processed_calls = []

    async def get_user(*_args, **_kwargs):
        return SimpleNamespace(username="tester")

    async def validate_profile(*_args, **_kwargs):
        return cfg

    async def get_tools(*_args, **_kwargs):
        return (
            [
                {
                    "type": "function",
                    "function": {
                        "name": "send_file_to_user",
                        "description": "Send a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            None,
        )

    async def generate_chat(*_args, **_kwargs):
        return responses.pop(0), None, {}, None, {"context_window_k": 128, "max_tokens": 256, "chat_timeout": 30}

    async def process_tool(current_tool_call, *_args, **_kwargs):
        processed_calls.append(current_tool_call.id)
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=current_tool_call.id, content='{"status":"success"}')

    async def save_tool_response(_db, _session_id, _uid, _profile_id, tool_response, messages, turn_messages):
        messages.append(tool_response)
        turn_messages.append(tool_response)
        return SimpleNamespace(id=10)

    async def save(*_args, **_kwargs):
        return None

    async def fail_audit_binding(*_args, **_kwargs):
        raise AssertionError("unconfigured background audit must not create a binding")

    monkeypatch.setattr(background_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(background_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(background_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", generate_chat)
    monkeypatch.setattr(background_module, "process_single_tool_with_isolated_db", process_tool)
    monkeypatch.setattr(background_module, "save_tool_response", save_tool_response)
    monkeypatch.setattr(background_module, "save_assistant_message", save)
    monkeypatch.setattr(background_module, "prevalidate_tool_round", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(background_module, "extract_files_to_user", lambda _responses: [])
    monkeypatch.setattr(background_module.audit_crud, "claim_passed_for_execution", fail_audit_binding)
    monkeypatch.setattr(background_module.audit_crud, "create_execution_attempt", fail_audit_binding)

    final_message, _turn_messages, files = await BackgroundDispatcherMixin._generate_reply_from_history(
        object(),
        uid="user-1",
        session_id="session-1",
        profile=profile,
        call_context="background_task_proactive_reply",
        allow_tools=True,
    )

    assert final_message.content == "后台总结"
    assert processed_calls == ["call-send"]
    assert files == []


async def _get_profile(_db, _profile_id):
    return _profile()
