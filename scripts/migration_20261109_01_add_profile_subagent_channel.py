import copy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.profile import Profile, ProfileConfig

MIGRATION_ID = "20261109_01_add_profile_subagent_channel_v1"


async def migrate(session: AsyncSession) -> None:
    result = await session.execute(select(Profile))
    profiles = result.scalars().all()
    for profile in profiles:
        configs = copy.deepcopy(profile.configs or {})
        channel = configs.get("channel")
        if isinstance(channel, dict) and "subagent_channel" not in channel:
            chat_channel = channel.get("chat_channel")
            if isinstance(chat_channel, dict):
                channel["subagent_channel"] = copy.deepcopy(chat_channel)
            else:
                channel["subagent_channel"] = {}
            normalized_configs = ProfileConfig.model_validate(configs).model_dump(mode="json")
            profile.configs = normalized_configs
            flag_modified(profile, "configs")
            session.add(profile)
