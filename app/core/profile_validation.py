from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CHANNEL_MODEL_NOT_FOUND,
    ERR_CHANNEL_NOT_FOUND,
    ERR_CHANNEL_USAGE_MISMATCH,
    ERR_PROFILE_AUDIT_MODEL_NOT_CHAT,
    ERR_PROFILE_CHANNEL_CONFIG_INVALID,
    ERR_PROFILE_NO_CHAT_CHANNEL,
    ERR_PROFILE_NOT_FOUND,
)
from app.core.crud.channel import channel_crud
from app.core.crud.profile import profile_crud
from app.core.exceptions import ParameterException, ResourceNotFoundException
from app.models.channel import ChannelConfig, ModelChannel, ModelUsage, is_channel_model_pending_delete
from app.models.profile import LongTermMemoryOrganizationConfig, Profile, ProfileConfig


async def lock_profile_channel_references(
    db: AsyncSession,
    *,
    configs: dict,
    memory_organization: LongTermMemoryOrganizationConfig | None,
    extra_channel_ids: list[int] | None = None,
) -> dict[int, ModelChannel]:
    """锁定 Profile 配置中所有成对引用的渠道并返回最新渠道对象。"""
    cfg = ProfileConfig.model_validate(configs)
    channel_ids: list[int] = []

    for channel_config in (
        cfg.channel.chat_channel,
        cfg.channel.context_summary_channel,
        cfg.channel.rerank_channel,
        cfg.channel.image_generation_channel,
    ):
        for rule in channel_config.rules:
            if rule.channel_id and rule.model_id:
                channel_ids.append(rule.channel_id)

    if cfg.security.audit_channel_id and cfg.security.audit_model_id:
        channel_ids.append(cfg.security.audit_channel_id)
    if cfg.memory.embedding_channel_id and cfg.memory.embedding_model_id:
        channel_ids.append(cfg.memory.embedding_channel_id)
    if memory_organization and memory_organization.organization_channel_id and memory_organization.organization_model_id:
        channel_ids.append(memory_organization.organization_channel_id)

    if extra_channel_ids:
        channel_ids.extend(extra_channel_ids)

    return await channel_crud.lock_many_for_mutation(
        db,
        channel_ids=channel_ids,
        commit=False,
    )


async def validate_audit_model_config(
    db: AsyncSession,
    security_config: dict,
    *,
    locked_channels: Mapping[int, ModelChannel] | None = None,
) -> None:
    """校验安全审计模型必须指向渠道下的 CHAT 模型。"""
    audit_channel_id = security_config.get("audit_channel_id")
    audit_model_id = security_config.get("audit_model_id")
    if not audit_channel_id or not audit_model_id:
        return

    if locked_channels is None:
        channel = await channel_crud.get(db, audit_channel_id)
    else:
        channel = locked_channels.get(audit_channel_id)
    if not channel:
        raise ParameterException(ERR_PROFILE_AUDIT_MODEL_NOT_CHAT)

    is_chat_model = any(item.get("model_id") == audit_model_id and item.get("usage") == ModelUsage.CHAT and not is_channel_model_pending_delete(item) for item in (channel.model_ids or []))
    if not is_chat_model:
        raise ParameterException(ERR_PROFILE_AUDIT_MODEL_NOT_CHAT)


async def validate_channel_rule_usage(
    db: AsyncSession,
    channel_config_obj: ChannelConfig,
    expected_usage: ModelUsage,
    *,
    locked_channels: Mapping[int, ModelChannel] | None = None,
) -> None:
    """校验渠道规则引用的模型条目用途与所在配置组一致。"""
    for rule in channel_config_obj.rules:
        if locked_channels is None:
            channel = await channel_crud.get(db, rule.channel_id)
        else:
            channel = locked_channels.get(rule.channel_id)
        if not channel:
            raise ParameterException(ERR_CHANNEL_NOT_FOUND)

        model_matches = []
        for item in channel.model_ids or []:
            if str(item.get("model_id")) == rule.model_id:
                model_matches.append(item)
        if not model_matches:
            raise ParameterException(ERR_CHANNEL_MODEL_NOT_FOUND)

        usage_matches = []
        for item in model_matches:
            if str(item.get("usage")) == expected_usage.value:
                usage_matches.append(item)
        if usage_matches:
            if any(not is_channel_model_pending_delete(item) for item in usage_matches):
                continue
            raise ParameterException(ERR_CHANNEL_MODEL_NOT_FOUND)

        actual_usage = str(model_matches[0].get("usage"))
        raise ParameterException(ERR_CHANNEL_USAGE_MISMATCH, expected=expected_usage.value, actual=actual_usage)


async def validate_channel_configs(
    db: AsyncSession,
    channel_config: dict,
    *,
    locked_channels: Mapping[int, ModelChannel] | None = None,
) -> None:
    """校验 channel 配置中的渠道配置合法性。

    - chat_channel：至少需要一条启用规则
    - context_summary_channel：可选，有配置则按 CHAT 用途校验
    - rerank_channel：可选，有配置则校验
    - image_generation_channel：可选，有配置则校验
    - 每条规则必须引用对应用途的模型条目
    """
    channel_usage_map = {
        "chat_channel": ModelUsage.CHAT,
        "context_summary_channel": ModelUsage.CHAT,
        "rerank_channel": ModelUsage.RERANK,
        "image_generation_channel": ModelUsage.IMAGE_GENERATION,
    }

    for channel_key, expected_usage in channel_usage_map.items():
        channel_raw = channel_config.get(channel_key)
        if not channel_raw:
            continue

        try:
            channel_config_obj = ChannelConfig.model_validate(channel_raw)
        except Exception as e:
            raise ParameterException(
                ERR_PROFILE_CHANNEL_CONFIG_INVALID,
                channel_key=channel_key,
                error=str(e),
            ) from e

        await validate_channel_rule_usage(
            db,
            channel_config_obj,
            expected_usage,
            locked_channels=locked_channels,
        )


async def validate_profile_for_assignment(db: AsyncSession, profile: Profile) -> ProfileConfig:
    cfg = ProfileConfig.model_validate(profile.configs)
    if not cfg.channel.chat_channel.rules:
        raise ParameterException(ERR_PROFILE_NO_CHAT_CHANNEL)
    await validate_channel_configs(db, cfg.channel.model_dump())
    return cfg


async def get_validated_profile_for_assignment(db: AsyncSession, *, profile_id: int, uid: str | None) -> Profile:
    profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=profile_id,
        uid=uid,
    )
    if not profile:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    await validate_profile_for_assignment(db, profile)
    return profile
