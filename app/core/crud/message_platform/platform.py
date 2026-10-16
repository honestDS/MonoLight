from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.message_platform import (
    MessagePlatform,
    MessagePlatformCreate,
    MessagePlatformStatus,
    MessagePlatformType,
    MessagePlatformUpdate,
)
from app.models.session import ChatSession

WEIXIN_OPENCLAW_SESSION_PREFIX = "weixin-openclaw:"


def _parse_weixin_openclaw_session_user_id(session_id: str) -> str:
    if not session_id.startswith(WEIXIN_OPENCLAW_SESSION_PREFIX):
        return ""
    return session_id[len(WEIXIN_OPENCLAW_SESSION_PREFIX) :].strip()


class CRUDMessagePlatform(CRUDBase[MessagePlatform, MessagePlatformCreate, MessagePlatformUpdate]):
    async def list_by_profile_assignment(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
    ) -> list[MessagePlatform]:
        result = await db.execute(select(MessagePlatform).where(MessagePlatform.uid == uid, MessagePlatform.profile_id == profile_id).order_by(MessagePlatform.id.asc()))
        return list(result.scalars().all())

    async def clear_profile_assignment(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        commit: bool = False,
    ) -> int:
        result = await db.execute(update(MessagePlatform).where(MessagePlatform.uid == uid, MessagePlatform.profile_id == profile_id).values(profile_id=None).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        else:
            await db.flush()
        return result.rowcount or 0

    async def unlink_profile_references(
        self,
        db: AsyncSession,
        *,
        profile_ids: set[int],
        excluded_uid: str,
    ) -> int:
        if not profile_ids:
            return 0
        result = await db.execute(
            update(MessagePlatform)
            .where(
                or_(MessagePlatform.uid != excluded_uid, MessagePlatform.uid.is_(None)),
                MessagePlatform.profile_id.in_(profile_ids),
            )
            .values(profile_id=None)
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        return result.rowcount or 0

    async def get_by_name(self, db: AsyncSession, name: str, *, uid: str | None) -> MessagePlatform | None:
        result = await db.execute(select(MessagePlatform).where(MessagePlatform.name == name, MessagePlatform.uid == uid))
        return result.scalars().first()

    async def get_for_update(
        self,
        db: AsyncSession,
        *,
        platform_id: int,
        uid: str | None = None,
    ) -> MessagePlatform | None:
        conditions = [MessagePlatform.id == platform_id]
        if uid is not None:
            conditions.append(MessagePlatform.uid == uid)
        await db.execute(update(MessagePlatform).where(*conditions).values(updated_at=MessagePlatform.updated_at).execution_options(synchronize_session=False))
        await db.flush()
        result = await db.execute(select(MessagePlatform).where(*conditions).with_for_update().execution_options(populate_existing=True))
        return result.scalars().first()

    async def has_profile_assignment(self, db: AsyncSession, profile_id: int) -> bool:
        result = await db.execute(select(MessagePlatform.id).where(MessagePlatform.profile_id == profile_id).limit(1))
        return result.scalar() is not None

    async def list_platforms(
        self,
        db: AsyncSession,
        *,
        skip: int = 0,
        limit: int = 100,
        uid: str | None = None,
    ) -> list[MessagePlatform]:
        query = select(MessagePlatform)
        if uid is not None:
            query = query.where(MessagePlatform.uid == uid)
        result = await db.execute(query.order_by(MessagePlatform.id.desc()).offset(skip).limit(limit))
        return result.scalars().all()

    async def count_platforms(self, db: AsyncSession, *, uid: str | None = None) -> int:
        query = select(func.count()).select_from(MessagePlatform)
        if uid is not None:
            query = query.where(MessagePlatform.uid == uid)
        result = await db.execute(query)
        return result.scalar() or 0

    async def list_enabled(self, db: AsyncSession) -> list[MessagePlatform]:
        result = await db.execute(select(MessagePlatform).where(MessagePlatform.is_enabled.is_(True)).order_by(MessagePlatform.id.asc()))
        return result.scalars().all()

    async def list_enabled_by_type(self, db: AsyncSession, platform_type: MessagePlatformType) -> list[MessagePlatform]:
        result = await db.execute(select(MessagePlatform).where(MessagePlatform.is_enabled.is_(True), MessagePlatform.platform_type == platform_type).order_by(MessagePlatform.id.asc()))
        return result.scalars().all()

    async def list_pollable(self, db: AsyncSession) -> list[MessagePlatform]:
        result = await db.execute(
            select(MessagePlatform)
            .where(
                MessagePlatform.is_enabled.is_(True),
                MessagePlatform.platform_type == MessagePlatformType.WEIXIN_OPENCLAW,
                MessagePlatform.status == MessagePlatformStatus.CONNECTED,
            )
            .order_by(MessagePlatform.id.asc())
        )
        return result.scalars().all()

    async def get_platforms_for_sessions(
        self,
        db: AsyncSession,
        *,
        sessions: Sequence[Any],
        require_available: bool = True,
    ) -> dict[str, MessagePlatform]:
        candidates = [session for session in sessions if session.source == "weixin-openclaw" and (session.message_platform_id is not None or (session.message_platform_user_id or "").strip() or _parse_weixin_openclaw_session_user_id(session.session_id))]
        if not candidates:
            return {}

        owner_uids = {session.uid for session in candidates}
        bound_platform_ids = {session.message_platform_id for session in candidates if session.message_platform_id is not None}
        legacy_owner_uids = {session.uid for session in candidates if session.message_platform_id is None}
        result = await db.execute(
            select(MessagePlatform).where(
                MessagePlatform.uid.in_(owner_uids),
                MessagePlatform.platform_type == MessagePlatformType.WEIXIN_OPENCLAW,
                or_(MessagePlatform.id.in_(bound_platform_ids), MessagePlatform.uid.in_(legacy_owner_uids)),
            )
        )
        platforms = result.scalars().all()

        platforms_by_id = {platform.id: platform for platform in platforms}
        platforms_by_uid: dict[str, list[MessagePlatform]] = {}
        platforms_by_uid_user: dict[tuple[str, str], list[MessagePlatform]] = {}
        for platform in platforms:
            platforms_by_uid.setdefault(platform.uid, []).append(platform)
            context_tokens = (platform.state or {}).get("context_tokens")
            if isinstance(context_tokens, dict):
                for user_id, context_token in context_tokens.items():
                    if str(context_token or "").strip():
                        platforms_by_uid_user.setdefault((platform.uid, user_id), []).append(platform)

        resolved: dict[str, MessagePlatform] = {}
        for session in candidates:
            selected_platform = None
            if session.message_platform_id is not None:
                platform = platforms_by_id.get(session.message_platform_id)
                if platform is not None and platform.uid == session.uid:
                    selected_platform = platform
            else:
                user_id = (session.message_platform_user_id or "").strip() or _parse_weixin_openclaw_session_user_id(session.session_id)
                if user_id:
                    matching_platforms = platforms_by_uid_user.get((session.uid, user_id), [])
                    if len(matching_platforms) == 1:
                        selected_platform = matching_platforms[0]
                    elif not matching_platforms:
                        uid_platforms = platforms_by_uid.get(session.uid, [])
                        if len(uid_platforms) == 1:
                            selected_platform = uid_platforms[0]

            if selected_platform is None:
                continue
            if require_available and (not selected_platform.is_enabled or selected_platform.status != MessagePlatformStatus.CONNECTED):
                continue
            resolved[session.session_id] = selected_platform

        return resolved

    async def get_platform_for_session(self, db: AsyncSession, *, uid: str, session_id: str, source: str) -> MessagePlatform | None:
        if source != "weixin-openclaw":
            return None

        session_result = await db.execute(select(ChatSession).where(ChatSession.session_id == session_id, ChatSession.uid == uid))
        session = session_result.scalars().first()
        if session is None or session.source != source:
            return None

        platforms_by_session = await self.get_platforms_for_sessions(db, sessions=[session])
        return platforms_by_session.get(session_id)

    async def update_runtime_state(
        self,
        db: AsyncSession,
        *,
        platform: MessagePlatform,
        status: MessagePlatformStatus | None = None,
        config: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        account_id: str | None = None,
        last_error: str | None = None,
        commit: bool = True,
    ) -> MessagePlatform:
        if status is not None:
            platform.status = status
        if config is not None:
            merged_config = dict(platform.config or {})
            merged_config.update(config)
            platform.config = merged_config
        if state is not None:
            merged_state = dict(platform.state or {})
            merged_state.update(state)
            platform.state = merged_state
        if account_id is not None:
            platform.account_id = account_id
        if last_error is not None:
            platform.last_error = last_error
        db.add(platform)
        await db.flush()
        await db.refresh(platform)
        if commit:
            await db.commit()
        return platform


message_platform_crud = CRUDMessagePlatform(MessagePlatform)
