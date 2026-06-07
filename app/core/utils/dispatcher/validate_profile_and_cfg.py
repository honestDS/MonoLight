from app.core.constants import (
    ERR_LLM_PROVIDER_NOT_CONFIGURED,
    ERR_PROFILE_NOT_FOUND,
    ERR_PROVIDER_EMBEDDING_ONLY,
)
from app.core.exceptions import (
    LLMException,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)
from app.models.provider import ModelUsage


def validate_profile_and_cfg(profile: Profile) -> ProfileConfig:
    if not profile:
        raise LLMException(message=ERR_PROFILE_NOT_FOUND)

    cfg = ProfileConfig.model_validate(profile.configs)

    # Note: 移除了不再使用的直接 provider 属性检查，因为 dispatcher 会处理具体的 Provider 对象。
    # 我们只校验配置是否存在。
    provider_id = cfg.provider.provider_id
    if not provider_id or provider_id <= 0:
        raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

    return cfg
