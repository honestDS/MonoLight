from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile, ProfileConfig

MIGRATION_ID = "20260820_add_profile_recall_settings_v1"


async def migrate(session: AsyncSession) -> None:
    result = await session.execute(select(Profile))
    for profile in result.scalars().all():
        normalized_configs = ProfileConfig.model_validate(profile.configs or {}).model_dump(mode="json")
        if normalized_configs != profile.configs:
            profile.configs = normalized_configs
            session.add(profile)
