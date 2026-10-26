from app.core.audit.confirmation import update_confirmation_message_status
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN, ERR_BACKGROUND_TASK_LEASE_MAX_ATTEMPTS_EXCEEDED
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.session_reply_queue.executor_audit import (
    _mark_audit_execution_unknown_reliably,
    get_bound_audit_execution,
)
from app.core.utils.time import get_local_time
from app.providers.database import AsyncSessionLocal


async def clear_background_tasks_on_startup() -> int:
    startup_at = get_local_time()
    error = t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)
    async with AsyncSessionLocal() as db:
        startup_audit_record_ids = await background_task_crud.list_startup_audit_record_ids(db, before=startup_at)
        startup_work_items = await session_reply_work_item_crud.list_startup_cancellation_work_items(db, before=startup_at)
        background_task_count = await background_task_crud.clear_all_for_startup(db, before=startup_at, commit=False)
        reliably_handled_audit_record_ids: set[int] = set()

        for work in startup_work_items:
            binding = get_bound_audit_execution(work)
            if binding is None:
                continue
            audit_record_id, claim_token = binding
            marked = await _mark_audit_execution_unknown_reliably(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
                error_reason=error,
                commit=False,
            )
            if marked:
                reliably_handled_audit_record_ids.add(audit_record_id)

        for audit_record_id in startup_audit_record_ids - reliably_handled_audit_record_ids:
            await update_confirmation_message_status(db, audit_record_id=audit_record_id, commit=False)

        summary_count = await session_reply_work_item_crud.cancel_background_tool_summaries(db, error=error, before=startup_at, commit=False)
        external_work_count = await external_work_crud.cancel_all_waiting(db, error=error, before=startup_at, commit=False)
        await db.commit()
    return background_task_count + summary_count + external_work_count


async def recover_pending_background_task_replies() -> int:
    async with AsyncSessionLocal() as db:
        return await background_task_crud.recover_expired_replies(db)


async def recover_pending_background_tasks() -> list[int]:
    offset = 0
    limit = 100
    reply_task_ids: list[int] = []
    max_attempts_error = t(ERR_BACKGROUND_TASK_LEASE_MAX_ATTEMPTS_EXCEEDED)
    while True:
        async with AsyncSessionLocal() as db:
            profiles = await profile_crud.get_multi_all(db, skip=offset, limit=limit)
            for profile in profiles:
                reply_task_ids.extend(
                    await background_task_crud.requeue_expired_running(
                        db,
                        profile_id=profile.id,
                        max_attempts_error=max_attempts_error,
                    )
                )

        if len(profiles) < limit:
            return reply_task_ids
        offset += limit
