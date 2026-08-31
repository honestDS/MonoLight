from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.profile.profile import profile_crud
from app.models.memory import LongTermMemoryStore

MIGRATION_ID = "20260820_sync_profile_memory_embedding_v1"


async def migrate(session: AsyncSession) -> None:
    result = await session.execute(
        select(LongTermMemoryStore).where(
            LongTermMemoryStore.active_embedding_revision > 0,
            LongTermMemoryStore.active_embedding_channel_id.is_not(None),
            LongTermMemoryStore.active_embedding_model_id.is_not(None),
            LongTermMemoryStore.active_embedding_model_id != "",
            LongTermMemoryStore.active_embedding_dimensions.is_not(None),
            LongTermMemoryStore.active_embedding_signature.is_not(None),
            LongTermMemoryStore.active_embedding_signature != "",
            LongTermMemoryStore.active_collection_name.is_not(None),
            LongTermMemoryStore.active_collection_name != "",
        )
    )
    for store in result.scalars().all():
        await profile_crud.normalize_memory_selection_by_uid(
            session,
            uid=store.uid,
            embedding_channel_id=store.active_embedding_channel_id,
            embedding_model_id=store.active_embedding_model_id,
            commit=False,
        )
