from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.account.user import user_crud
from app.core.crud.knowledge.base import knowledge_base_collection_owner_crud
from app.core.session_cleanup import delete_session_data


async def delete_user_data(db: AsyncSession, *, uid: str) -> None:
    await user_crud.lock_owned_configuration(db, uid=uid)
    session_ids, knowledge_bases, memory_stores, memory_revisions = await user_crud.get_deletion_state(db, uid=uid)

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
    await user_crud.delete_owned_knowledge_data(db, uid=uid)

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

    await user_crud.unlink_configuration_references(db, uid=uid)
    await user_crud.delete_owned_session_data(db, uid=uid)
    await user_crud.delete_owned_memory_data(db, uid=uid)
    await user_crud.delete_owned_configuration(db, uid=uid)
