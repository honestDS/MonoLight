from collections.abc import Iterable

from sqlalchemy import and_, delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_KB_COLLECTION_OWNER_CONFLICT
from app.core.crud.base import CRUDBase
from app.core.i18n import t
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseCreate,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    KnowledgeBaseUpdate,
)


class CRUDKnowledgeBase(CRUDBase[KnowledgeBase, KnowledgeBaseCreate, KnowledgeBaseUpdate]):
    async def get_managed_by_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
    ) -> KnowledgeBase | None:
        result = await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.uid == uid,
                KnowledgeBase.managed_profile_id == profile_id,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
            )
        )
        return result.scalars().first()

    async def lock_managed_by_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
    ) -> KnowledgeBase | None:
        result = await db.execute(
            update(KnowledgeBase)
            .where(
                KnowledgeBase.uid == uid,
                KnowledgeBase.managed_profile_id == profile_id,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
            )
            .values(updated_at=KnowledgeBase.updated_at)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await db.flush()
        refreshed = await db.execute(
            select(KnowledgeBase)
            .where(
                KnowledgeBase.uid == uid,
                KnowledgeBase.managed_profile_id == profile_id,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()

    async def mark_managed_initial_index_ready(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        active_collection_name: str,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(KnowledgeBase)
            .where(
                KnowledgeBase.uid == uid,
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
                KnowledgeBase.active_collection_name == active_collection_name,
                KnowledgeBase.index_status == KnowledgeBaseIndexStatus.PENDING,
            )
            .values(index_status=KnowledgeBaseIndexStatus.READY)
            .execution_options(synchronize_session=False)
        )
        changed = (result.rowcount or 0) == 1
        if not changed:
            current = await db.execute(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.uid == uid,
                    KnowledgeBase.id == knowledge_base_id,
                    KnowledgeBase.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED,
                    KnowledgeBase.active_collection_name == active_collection_name,
                )
            )
            if current.scalar_one_or_none() is None:
                if commit:
                    await db.rollback()
                return False
        if commit:
            await db.commit()
        else:
            await db.flush()
        return True

    async def list_by_embedding_channel_reference(
        self,
        db: AsyncSession,
        *,
        embedding_channel_id: int,
    ) -> list[KnowledgeBase]:
        """列出当前生效或目标嵌入配置引用指定渠道的知识库。"""
        active_incomplete = or_(
            KnowledgeBase.active_embedding_channel_id.is_(None),
            KnowledgeBase.active_embedding_model_id.is_(None),
            KnowledgeBase.active_collection_name.is_(None),
        )
        result = await db.execute(
            select(KnowledgeBase).where(
                or_(
                    and_(
                        ~active_incomplete,
                        KnowledgeBase.active_embedding_channel_id == embedding_channel_id,
                    ),
                    KnowledgeBase.target_embedding_channel_id == embedding_channel_id,
                    and_(
                        active_incomplete,
                        KnowledgeBase.embedding_channel_id == embedding_channel_id,
                    ),
                )
            )
        )
        return list(result.scalars().all())


knowledge_base_crud = CRUDKnowledgeBase(KnowledgeBase)


class CRUDKnowledgeBaseProfileBinding:
    async def list_user_knowledge_bases_by_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
    ) -> list[KnowledgeBase]:
        result = await db.execute(
            select(KnowledgeBase)
            .join(
                KnowledgeBaseProfileBinding,
                KnowledgeBaseProfileBinding.knowledge_base_id == KnowledgeBase.id,
            )
            .where(
                KnowledgeBaseProfileBinding.uid == uid,
                KnowledgeBaseProfileBinding.profile_id == profile_id,
                KnowledgeBase.uid == uid,
                KnowledgeBase.knowledge_base_type == KnowledgeBaseType.USER,
            )
            .order_by(KnowledgeBase.id.asc())
        )
        return list(result.scalars().all())

    async def get(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        profile_id: int,
    ) -> KnowledgeBaseProfileBinding | None:
        result = await db.execute(
            select(KnowledgeBaseProfileBinding).where(
                KnowledgeBaseProfileBinding.uid == uid,
                KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseProfileBinding.profile_id == profile_id,
            )
        )
        return result.scalars().first()

    async def lock(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        profile_id: int,
    ) -> KnowledgeBaseProfileBinding | None:
        result = await db.execute(
            update(KnowledgeBaseProfileBinding)
            .where(
                KnowledgeBaseProfileBinding.uid == uid,
                KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseProfileBinding.profile_id == profile_id,
            )
            .values(profile_id=KnowledgeBaseProfileBinding.profile_id)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await db.flush()
        refreshed = await db.execute(
            select(KnowledgeBaseProfileBinding)
            .where(
                KnowledgeBaseProfileBinding.uid == uid,
                KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base_id,
                KnowledgeBaseProfileBinding.profile_id == profile_id,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        profile_id: int,
    ) -> KnowledgeBaseProfileBinding:
        binding = KnowledgeBaseProfileBinding(
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            profile_id=profile_id,
        )
        db.add(binding)
        await db.flush()
        await db.refresh(binding)
        return binding


knowledge_base_profile_binding_crud = CRUDKnowledgeBaseProfileBinding()


class CRUDKnowledgeBaseCollectionOwner:
    async def enqueue(
        self,
        db: AsyncSession,
        *,
        knowledge_base_id: int,
        collection_names: Iterable[str | None],
        commit: bool = False,
    ) -> list[str]:
        names = list(dict.fromkeys(name for name in collection_names if name))
        if not names:
            return []

        result = await db.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name.in_(names)))
        owners_by_name = {owner.collection_name: owner for owner in result.scalars().all()}

        for name in names:
            owner = owners_by_name.get(name)
            if owner is not None and owner.knowledge_base_id != knowledge_base_id:
                raise ValueError(t(ERR_KB_COLLECTION_OWNER_CONFLICT, collection_name=name))

        for name in names:
            owner = owners_by_name.get(name)
            if owner is None:
                db.add(
                    KnowledgeBaseCollectionOwner(
                        collection_name=name,
                        knowledge_base_id=knowledge_base_id,
                    )
                )

        await db.flush()
        if commit:
            await db.commit()

        return names

    async def list_pending(
        self,
        db: AsyncSession,
        *,
        limit: int = 100,
    ) -> list[KnowledgeBaseCollectionOwner]:
        if limit <= 0:
            return []

        result = await db.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.knowledge_base_id.is_(None)).order_by(KnowledgeBaseCollectionOwner.updated_at, KnowledgeBaseCollectionOwner.collection_name).limit(limit))
        return list(result.scalars().all())

    async def mark_succeeded(
        self,
        db: AsyncSession,
        *,
        collection_name: str,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            delete(KnowledgeBaseCollectionOwner).where(
                KnowledgeBaseCollectionOwner.collection_name == collection_name,
                KnowledgeBaseCollectionOwner.knowledge_base_id.is_(None),
            )
        )
        if commit:
            await db.commit()

        return result.rowcount > 0

    async def mark_failed(
        self,
        db: AsyncSession,
        *,
        collection_name: str,
        error: str,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(KnowledgeBaseCollectionOwner)
            .where(
                KnowledgeBaseCollectionOwner.collection_name == collection_name,
                KnowledgeBaseCollectionOwner.knowledge_base_id.is_(None),
            )
            .values(
                cleanup_attempt_count=KnowledgeBaseCollectionOwner.cleanup_attempt_count + 1,
                cleanup_error=error,
                updated_at=func.now(),
            )
        )
        if commit:
            await db.commit()

        return result.rowcount > 0


knowledge_base_collection_owner_crud = CRUDKnowledgeBaseCollectionOwner()
