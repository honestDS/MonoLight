from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CHAT_PROVIDER_DISABLED,
    ERR_LLM_PROVIDER_NOT_CONFIGURED,
    ERR_PROFILE_NOT_FOUND,
    ERR_PROVIDER_EMBEDDING_ONLY,
)
from app.core.crud.provider import provider_crud
from app.core.exceptions import (
    LLMException,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)
from app.models.provider import ModelUsage


async def validate_profile_and_cfg(db: AsyncSession, profile: Profile) -> ProfileConfig:
    if not profile:
        raise LLMException(message=ERR_PROFILE_NOT_FOUND)

    cfg = ProfileConfig.model_validate(profile.configs)

    # Note: 移除了不再使用的直接 provider 属性检查，因为 dispatcher 会处理具体的 Provider 对象。
    # 我们只校验配置是否存在。
    provider_id = cfg.provider.provider_id
    if not provider_id or provider_id <= 0:
        raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

    chat_provider = await provider_crud.get(db, provider_id)
    if chat_provider and chat_provider.usage == ModelUsage.EMBEDDING:
        raise LLMException(message=ERR_PROVIDER_EMBEDDING_ONLY)
    # 对话模型被禁用时拒绝调用，避免使用已停用的主对话模型
    if chat_provider and not chat_provider.is_active:
        raise LLMException(message=ERR_CHAT_PROVIDER_DISABLED)

    return cfg
