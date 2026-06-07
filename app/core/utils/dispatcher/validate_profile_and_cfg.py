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

    if not profile.provider:
        raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

    if profile.provider.usage == ModelUsage.EMBEDDING:
        raise LLMException(message=ERR_PROVIDER_EMBEDDING_ONLY)

    return cfg
