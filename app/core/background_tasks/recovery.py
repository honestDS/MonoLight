from app.core.background_tasks.reply_trigger import trigger_background_task_reply
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.providers.database import AsyncSessionLocal


async def recover_pending_background_tasks() -> None:
    offset = 0
    limit = 100
    while True:
        async with AsyncSessionLocal() as db:
            profiles = await profile_crud.get_multi_all(db, skip=offset, limit=limit)
            reply_task_ids = []
            for profile in profiles:
                reply_task_ids.extend(await background_task_crud.requeue_expired_running(db, profile_id=profile.id))

        for task_id in reply_task_ids:
            await trigger_background_task_reply(task_id)

        if len(profiles) < limit:
            return
        offset += limit
