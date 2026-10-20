from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.audit.audit import audit_crud
from app.core.crud.knowledge.base import knowledge_base_collection_owner_crud, knowledge_base_crud
from app.core.crud.memory.store import memory_embedding_revision_crud, memory_store_crud
from app.core.crud.message_platform.outbox import message_platform_outbox_crud
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.profile.prompt import prompt_crud
from app.core.crud.session.session import session_crud
from app.core.crud.system.log import system_log_crud
from app.core.crud.task.background import background_task_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.crud.terminal.session import terminal_session_crud
from app.core.session.cleanup import delete_session_data


async def delete_user_data(db: AsyncSession, *, uid: str) -> list[str]:
    prompts = await prompt_crud.get_all_by_uid(db, uid=uid, lock_for_update=True)
    profiles = await profile_crud.get_all_by_uid(db, uid=uid, lock_for_update=True)
    memory_stores = await memory_store_crud.get_all_by_uid(db, uid=uid, lock_for_update=True)
    knowledge_bases = await knowledge_base_crud.get_all_by_uid(db, uid=uid, lock_for_update=True)
    memory_revisions = await memory_embedding_revision_crud.get_all_by_uid(db, uid=uid, lock_for_update=True)
    sessions = await session_crud.get_all_by_uid(db, uid=uid, lock_for_update=True)
    session_ids = [session.session_id for session in sessions]
    profile_ids = {profile.id for profile in profiles if profile.id is not None}
    prompt_ids = {prompt.id for prompt in prompts if prompt.id is not None}

    for knowledge_base in knowledge_bases:
        await knowledge_base_collection_owner_crud.enqueue(
            db,
            knowledge_base_id=knowledge_base.id,
            collection_names=(
                knowledge_base.collection_name,
                knowledge_base.active_collection_name,
                knowledge_base.target_collection_name,
                knowledge_base.old_collection_name,
            ),
            commit=False,
        )
    await knowledge_base_crud.delete_by_uid(db, uid=uid)

    collection_names = sorted(
        {
            name
            for memory_store in memory_stores
            for name in (
                memory_store.active_collection_name,
                memory_store.target_collection_name,
                memory_store.old_collection_name,
            )
            if name
        }
        | {name for memory_revision in memory_revisions for name in (memory_revision.from_collection, memory_revision.to_collection) if name}
    )
    for collection_name in collection_names:
        await knowledge_base_collection_owner_crud.requeue_orphan(
            db,
            collection_name=collection_name,
            commit=False,
        )

    for session_id in session_ids:
        await delete_session_data(
            db,
            session_id=session_id,
            uid=uid,
            is_admin=False,
        )

    await session_crud.unlink_profile_references(db, profile_ids=profile_ids, excluded_uid=uid)
    await scheduled_task_crud.unlink_profile_references(db, profile_ids=profile_ids, excluded_uid=uid)
    await message_platform_crud.unlink_profile_references(db, profile_ids=profile_ids, excluded_uid=uid)
    await profile_crud.unlink_prompt_references(db, prompt_ids=prompt_ids, excluded_uid=uid)

    await background_task_crud.delete_by_uid(db, uid=uid)
    await scheduled_task_crud.delete_by_uid(db, uid=uid)
    await message_platform_crud.delete_by_uid(db, uid=uid)
    await audit_crud.delete_by_uid(db, uid=uid)
    await system_log_crud.delete_by_uid(db, uid=uid)
    await message_platform_outbox_crud.delete_by_uid(db, uid=uid)
    await terminal_session_crud.delete_by_uid(db, uid=uid)
    await session_crud.delete_by_uid(db, uid=uid)
    await memory_store_crud.delete_by_uid(db, uid=uid)
    await profile_crud.delete_by_uid(db, uid=uid)
    await prompt_crud.delete_by_uid(db, uid=uid)
    return session_ids
