from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation_lifecycle import expire_confirmation_by_session
from app.core.constants import ERR_SUBAGENT_CONTEXT_INVALID
from app.core.crud.session.reply_work_item import session_reply_work_item_crud as reply_work_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.log import get_logger
from app.core.session_reply_queue.child_delivery import resume_child_work_for_parent
from app.core.session_reply_queue.child_lifecycle import finish_child_work

logger = get_logger(__name__)

__all__ = ["reconcile_child_work"]


async def reconcile_child_work(db: AsyncSession) -> int:
    after_id = 0
    while True:
        rounds = await subagent_crud.list_unfinished_rounds(db, after_id=after_id, limit=100)
        if not rounds:
            break

        round_snapshots = [
            (
                int(round_row.child_work_id),
                int(round_row.current_work_id),
                bool(round_row.waiting_confirmation),
            )
            for round_row in rounds
        ]

        for child_work_id, current_work_id, waiting_confirmation in round_snapshots:
            after_id = max(after_id, child_work_id)
            work = None
            try:
                work = await reply_work_crud.get(db, current_work_id)
                if work is None:
                    logger.bind(
                        child_work_id=child_work_id,
                        root_work_id=None,
                        current_work_id=current_work_id,
                    ).error(ERR_SUBAGENT_CONTEXT_INVALID)
                    await db.rollback()
                    continue

                if waiting_confirmation:
                    await expire_confirmation_by_session(
                        db,
                        uid=work.uid,
                        session_id=work.session_id,
                    )
                await finish_child_work(db, work=work)
                await db.commit()
            except Exception:
                logger.bind(
                    child_work_id=child_work_id,
                    current_work_id=current_work_id,
                ).exception("Child work reconciliation failed")
                await db.rollback()

        if len(rounds) < 100:
            break

    awakened_count = 0
    after_id = 0
    while True:
        parent_work_ids = await subagent_crud.list_pending_parent_ids(db, after_id=after_id, limit=100)
        if not parent_work_ids:
            break

        for parent_work_id in parent_work_ids:
            after_id = max(after_id, parent_work_id)
            try:
                if await resume_child_work_for_parent(db, parent_work_id=parent_work_id):
                    awakened_count += 1
            except Exception:
                logger.bind(parent_work_id=parent_work_id).exception("Child work parent resumption failed")

        if len(parent_work_ids) < 100:
            break

    return awakened_count
