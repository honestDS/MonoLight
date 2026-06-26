from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.providers.database import AsyncSessionLocal


async def recover_pending_background_tasks() -> None:
    from app.core.background_tasks.manager import background_task_manager
    from app.core.background_tasks.reply_trigger import trigger_background_task_reply

    async with AsyncSessionLocal() as db:
        profiles = await profile_crud.get_multi(db, limit=100)
        for profile in profiles:
            reply_task_ids = await background_task_crud.requeue_expired_running(db, profile_id=profile.id)
            await background_task_manager.schedule(profile)
            for task_id in reply_task_ids:
                await trigger_background_task_reply(task_id)
