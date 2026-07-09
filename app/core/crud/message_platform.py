from typing import Any

from sqlalchemy import func
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
    async def get_by_name(self, db: AsyncSession, name: str) -> MessagePlatform | None:
        result = await db.execute(select(MessagePlatform).where(MessagePlatform.name == name))
        return result.scalars().first()

    async def list_platforms(self, db: AsyncSession, *, skip: int = 0, limit: int = 100) -> list[MessagePlatform]:
        result = await db.execute(select(MessagePlatform).order_by(MessagePlatform.id.desc()).offset(skip).limit(limit))
        return result.scalars().all()

    async def count_platforms(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(MessagePlatform))
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

    async def get_platform_for_session(self, db: AsyncSession, *, uid: str, session_id: str, source: str) -> MessagePlatform | None:
        if source != "weixin-openclaw":
            return None

        user_id = _parse_weixin_openclaw_session_user_id(session_id)
        session_result = await db.execute(select(ChatSession).where(ChatSession.session_id == session_id, ChatSession.uid == uid))
        session = session_result.scalars().first()
        if session is not None and session.reply_target_source != source:
            return None

        result = await db.execute(
            select(MessagePlatform)
            .where(
                MessagePlatform.is_enabled.is_(True),
                MessagePlatform.platform_type == MessagePlatformType.WEIXIN_OPENCLAW,
                MessagePlatform.status == MessagePlatformStatus.CONNECTED,
                MessagePlatform.uid == uid,
            )
            .order_by(MessagePlatform.id.asc())
        )
        platforms = result.scalars().all()
        if not user_id:
            return platforms[0] if len(platforms) == 1 else None
        for platform in platforms:
            context_tokens = (platform.state or {}).get("context_tokens")
            if isinstance(context_tokens, dict) and str(context_tokens.get(user_id) or "").strip():
                return platform
        return platforms[0] if len(platforms) == 1 else None

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
        await db.commit()
        await db.refresh(platform)
        return platform


message_platform_crud = CRUDMessagePlatform(MessagePlatform)
