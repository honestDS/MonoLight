from app.core import constants
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.core.i18n import t
from app.providers.database import AsyncSessionLocal


async def recover_pending_background_task_replies() -> int:
    async with AsyncSessionLocal() as db:
        return await background_task_crud.recover_expired_replies(db)


async def recover_pending_background_tasks() -> list[int]:
    offset = 0
    limit = 100
    reply_task_ids: list[int] = []
    max_attempts_error = t(constants.ERR_BACKGROUND_TASK_LEASE_MAX_ATTEMPTS_EXCEEDED)
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
