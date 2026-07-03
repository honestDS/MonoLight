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
