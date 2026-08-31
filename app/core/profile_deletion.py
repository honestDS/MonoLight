from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_PROFILE_DELETE_PERSISTED_OWNER_REQUIRED
from app.core.crud.knowledge.base import (
    knowledge_base_collection_owner_crud,
    knowledge_base_crud,
    knowledge_base_profile_binding_crud,
)
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.i18n import t
from app.core.session_cleanup import delete_session_data
from app.models.profile import Profile

_PREVIEW_ITEM_LIMIT = 20


def _preview_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(items),
        "items": items[:_PREVIEW_ITEM_LIMIT],
        "omitted_count": max(0, len(items) - _PREVIEW_ITEM_LIMIT),
    }


def _build_confirmation_token(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def build_profile_deletion_impact(
    db: AsyncSession,
    *,
    profile: Profile,
) -> dict[str, Any]:
    if profile.id is None or profile.uid is None:
        raise ValueError(t(ERR_PROFILE_DELETE_PERSISTED_OWNER_REQUIRED))

    sessions = await session_crud.list_by_profile_reference(
        db,
        uid=profile.uid,
        profile_id=profile.id,
    )
    session_ids = [session.session_id for session in sessions]
    message_count = await message_crud.count_by_sessions(
        db,
        uid=profile.uid,
        session_ids=session_ids,
    )
    scheduled_tasks = await scheduled_task_crud.list_by_profile(
        db,
        uid=profile.uid,
        profile_id=profile.id,
    )
    message_platforms = await message_platform_crud.list_by_profile_assignment(
        db,
        uid=profile.uid,
        profile_id=profile.id,
    )
    managed_knowledge_base = await knowledge_base_crud.get_managed_by_profile(
        db,
        uid=profile.uid,
        profile_id=profile.id,
    )
    managed_knowledge_count = 0
    if managed_knowledge_base is not None and managed_knowledge_base.id is not None:
        managed_knowledge_count = await managed_knowledge_item_crud.count_by_knowledge_base(
            db,
            uid=profile.uid,
            knowledge_base_id=managed_knowledge_base.id,
        )
    user_knowledge_bases = await knowledge_base_profile_binding_crud.list_user_knowledge_bases_by_profile(
        db,
        uid=profile.uid,
        profile_id=profile.id,
    )

    token_payload = {
        "profile_id": profile.id,
        "session_ids": session_ids,
        "scheduled_task_ids": [task.id for task in scheduled_tasks],
        "message_platform_ids": [platform.id for platform in message_platforms],
        "managed_knowledge_base_id": getattr(managed_knowledge_base, "id", None),
        "managed_knowledge_count": managed_knowledge_count,
        "user_knowledge_base_ids": [knowledge_base.id for knowledge_base in user_knowledge_bases],
    }
    return {
        "impact_token": _build_confirmation_token(token_payload),
        "profile": {"id": profile.id, "name": profile.name},
        "sessions": {
            **_preview_items(
                [
                    {
                        "session_id": session.session_id,
                        "title": session.title,
                        "source": session.source,
                    }
                    for session in sessions
                ]
            ),
            "message_count": message_count,
        },
        "scheduled_tasks": _preview_items([{"id": task.id, "name": task.name} for task in scheduled_tasks]),
        "message_platforms": _preview_items([{"id": platform.id, "name": platform.name} for platform in message_platforms]),
        "managed_knowledge_base": (
            {
                "count": 1,
                "items": [
                    {
                        "id": managed_knowledge_base.id,
                        "name": managed_knowledge_base.name,
                        "knowledge_count": managed_knowledge_count,
                    }
                ],
                "omitted_count": 0,
            }
            if managed_knowledge_base is not None
            else {"count": 0, "items": [], "omitted_count": 0}
        ),
        "user_knowledge_base_bindings": _preview_items([{"id": knowledge_base.id, "name": knowledge_base.name} for knowledge_base in user_knowledge_bases]),
    }


async def execute_profile_deletion(
    db: AsyncSession,
    *,
    profile: Profile,
    impact: dict[str, Any],
) -> None:
    if profile.id is None or profile.uid is None:
        raise ValueError(t(ERR_PROFILE_DELETE_PERSISTED_OWNER_REQUIRED))

    managed = impact["managed_knowledge_base"]["items"]
    if managed:
        knowledge_base = await knowledge_base_crud.get(
            db,
            managed[0]["id"],
        )
        if knowledge_base is not None:
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

    for session in impact["sessions"]["items"]:
        await delete_session_data(
            db,
            session_id=session["session_id"],
            uid=profile.uid,
            is_admin=True,
        )
    if impact["sessions"]["omitted_count"]:
        remaining_sessions = await session_crud.list_by_profile_reference(
            db,
            uid=profile.uid,
            profile_id=profile.id,
        )
        preview_ids = {item["session_id"] for item in impact["sessions"]["items"]}
        for session in remaining_sessions:
            if session.session_id in preview_ids:
                continue
            await delete_session_data(
                db,
                session_id=session.session_id,
                uid=profile.uid,
                is_admin=True,
            )

    await scheduled_task_crud.delete_by_profile(
        db,
        uid=profile.uid,
        profile_id=profile.id,
        commit=False,
    )
    await message_platform_crud.clear_profile_assignment(
        db,
        uid=profile.uid,
        profile_id=profile.id,
        commit=False,
    )
    await profile_crud.delete_locked(db, profile=profile, commit=False)


__all__ = ["build_profile_deletion_impact", "execute_profile_deletion"]
