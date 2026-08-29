from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_KB_NOT_FOUND
from app.core.crud.knowledge_base import (
    knowledge_base_crud,
    knowledge_base_profile_binding_crud,
)
from app.core.exceptions import ResourceNotFoundException


async def get_user_knowledge_base_ids_for_profile(
    db: AsyncSession,
    *,
    uid: str | None,
    profile_id: int,
) -> list[int]:
    if uid is None:
        return []
    knowledge_bases = await knowledge_base_profile_binding_crud.list_user_knowledge_bases_by_profile(
        db,
        uid=uid,
        profile_id=profile_id,
    )
    return [knowledge_base.id for knowledge_base in knowledge_bases if knowledge_base.id is not None]


async def replace_user_knowledge_base_bindings(
    db: AsyncSession,
    *,
    uid: str | None,
    profile_id: int,
    knowledge_base_ids: list[int] | None,
) -> list[int] | None:
    if knowledge_base_ids is None:
        return None

    normalized_ids = list(dict.fromkeys(knowledge_base_ids))
    if uid is None:
        if normalized_ids:
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
        return []

    knowledge_bases = await knowledge_base_crud.list_user_by_ids(
        db,
        uid=uid,
        knowledge_base_ids=normalized_ids,
    )
    if len(knowledge_bases) != len(normalized_ids):
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)

    await knowledge_base_profile_binding_crud.delete_user_by_profile(
        db,
        uid=uid,
        profile_id=profile_id,
        commit=False,
    )
    for knowledge_base_id in normalized_ids:
        await knowledge_base_profile_binding_crud.create(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            profile_id=profile_id,
        )
    return normalized_ids


__all__ = [
    "get_user_knowledge_base_ids_for_profile",
    "replace_user_knowledge_base_bindings",
]
