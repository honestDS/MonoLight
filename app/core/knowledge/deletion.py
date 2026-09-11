from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_KB_NOT_FOUND, ERR_SESSION_NO_PERMISSION, LOG_KNOWLEDGE_BASE_DELETE_WITH_ACTIVE_ORGANIZATION
from app.core.crud.knowledge.base import (
    knowledge_base_collection_owner_crud,
    knowledge_base_crud,
)
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.crud.profile.profile import profile_crud
from app.core.exceptions import ForbiddenException, ResourceNotFoundException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.knowledge_base import KnowledgeBaseType

logger = get_logger(__name__)


async def delete_owned_knowledge_base(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    requester_uid: str,
    is_superuser: bool,
    commit: bool = True,
) -> None:
    snapshot = await knowledge_base_crud.get(db, knowledge_base_id)
    if snapshot is None:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
    if snapshot.uid != requester_uid and not is_superuser:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

    owner_uid = snapshot.uid
    try:
        if snapshot.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED:
            profile_id = snapshot.managed_profile_id
            if profile_id is None:
                raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
            profile = await profile_crud.lock_for_runtime_use(
                db,
                profile_id=profile_id,
                uid=owner_uid,
            )
            if profile is None:
                raise ResourceNotFoundException(ERR_KB_NOT_FOUND)

        knowledge_base = await knowledge_base_crud.lock_owned_by_id(
            db,
            uid=owner_uid,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None:
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)

        if knowledge_base.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED:
            locked_count = await managed_knowledge_item_crud.count_organization_locked(
                db,
                uid=owner_uid,
                knowledge_base_id=knowledge_base.id,
            )
            if locked_count:
                logger.bind(knowledge_base_id=knowledge_base.id, uid=owner_uid, locked_count=locked_count).warning(t(LOG_KNOWLEDGE_BASE_DELETE_WITH_ACTIVE_ORGANIZATION, locked_count=locked_count))

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
        await knowledge_base_crud.delete_locked(
            db,
            knowledge_base=knowledge_base,
            commit=False,
        )
        if commit:
            await db.commit()
    except Exception:
        if commit and db.in_transaction():
            await db.rollback()
        raise


__all__ = ["delete_owned_knowledge_base"]
