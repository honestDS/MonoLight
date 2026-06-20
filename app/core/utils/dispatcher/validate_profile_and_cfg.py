"""Profile 配置校验（渠道管理架构适配）"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_LLM_CHANNEL_NOT_CONFIGURED,
    ERR_PROFILE_NOT_FOUND,
)
from app.core.exceptions import (
    LLMException,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


async def validate_profile_and_cfg(db: AsyncSession, profile: Profile) -> ProfileConfig:
    """校验 Profile 配置并返回解析后的 ProfileConfig。

    校验点：
    - Profile 存在性
    - chat_channel 配置完整性（至少有一个规则）
    """
    if not profile:
        raise LLMException(message=ERR_PROFILE_NOT_FOUND)

    cfg = ProfileConfig.model_validate(profile.configs)

    # 校验 chat_channel 存在且配置有效
    chat_channel = cfg.channel.chat_channel
    if not chat_channel or not chat_channel.rules:
        raise LLMException(message=ERR_LLM_CHANNEL_NOT_CONFIGURED)

    return cfg
