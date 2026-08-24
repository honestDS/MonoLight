import json
from copy import deepcopy

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.core.crud.profile import profile_crud
from app.models.channel import ChannelModelItem, ModelUsage

# 渠道用途映射：统一定义，避免重复
CHANNEL_USAGE_MAP = {
    "chat_channel": ModelUsage.CHAT.value,
    "context_summary_channel": ModelUsage.CHAT.value,
    "rerank_channel": ModelUsage.RERANK.value,
    "image_generation_channel": ModelUsage.IMAGE_GENERATION.value,
}


def _is_same_channel(rule: dict, channel_id: int) -> bool:
    return str(rule.get("channel_id")) == str(channel_id)


def _get_existing_model_ids_by_usage(model_ids: list[dict]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}
    for item in model_ids:
        item_usage = str(item.get("usage"))
        item_model_id = item.get("model_id")
        if item_model_id and item_usage in result:
            result[item_usage].add(str(item_model_id))
    return result


def _clean_channel_rules_from_configs(
    configs: dict,
    channel_id: int,
    model_ids: list[dict],
) -> int:
    existing_model_ids_by_usage = _get_existing_model_ids_by_usage(model_ids)
    channel_config = configs.get("channel") or {}
    removed_count = 0
    profile_changed = False

    for channel_key, usage in CHANNEL_USAGE_MAP.items():
        channel = channel_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue

        existing_model_ids = existing_model_ids_by_usage.get(usage, set())
        old_rules = channel["rules"]
        new_rules = []
        rules_changed = False
        for rule in old_rules:
            if _is_same_channel(rule, channel_id) and str(rule.get("model_id")) not in existing_model_ids:
                removed_count += 1
                rules_changed = True
                continue

            new_rules.append(rule)

        if rules_changed:
            channel["rules"] = new_rules
            profile_changed = True

    return removed_count if profile_changed else 0


async def _remove_unavailable_channel_rules(
    db: AsyncSession,
    channel_id: int,
    model_ids: list[dict],
    *,
    apply_changes: bool = True,
) -> int:
    """批量清理Profile中失效的渠道规则。

    采用游标分页避免 offset 窗口错位，调用方负责统一提交事务。
    """
    batch_size = 100
    last_id = 0
    total_removed = 0

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = deepcopy(profile.configs or {})
            profile_removed_count = _clean_channel_rules_from_configs(configs, channel_id, model_ids)
            if profile_removed_count > 0:
                total_removed += profile_removed_count
                if apply_changes:
                    profile.configs = configs
                    flag_modified(profile, "configs")
                    db.add(profile)
                    batch_changed = True

        if apply_changes and batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_removed


def _model_entry_signature(item: dict) -> str:
    normalized = ChannelModelItem.model_validate(item).model_dump(exclude={"model_id"})
    return json.dumps(normalized, sort_keys=True, default=str)


def _collect_channel_rule_model_ids(configs: dict, channel_id: int) -> dict[str, set[str]]:
    channel_config = configs.get("channel") or {}
    referenced_model_ids_by_usage: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}

    for channel_key, usage in CHANNEL_USAGE_MAP.items():
        channel = channel_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue
        for rule in channel["rules"]:
            if _is_same_channel(rule, channel_id) and rule.get("model_id"):
                referenced_model_ids_by_usage[usage].add(str(rule["model_id"]))

    return referenced_model_ids_by_usage


def _build_model_id_rename_index(old_model_ids: list[dict], new_model_ids: list[dict]) -> dict[str, dict]:
    old_by_usage_and_id: dict[tuple[str, str], dict] = {}
    for item in old_model_ids:
        item_usage = item.get("usage")
        item_model_id = item.get("model_id")
        if item_usage and item_model_id:
            old_by_usage_and_id[(str(item_usage), str(item_model_id))] = item

    old_ids_by_usage: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}
    for item in old_model_ids:
        item_usage = str(item.get("usage"))
        item_model_id = item.get("model_id")
        if item_model_id and item_usage in old_ids_by_usage:
            old_ids_by_usage[item_usage].add(str(item_model_id))

    new_ids_by_usage: dict[str, set[str]] = {usage.value: set() for usage in ModelUsage}
    for item in new_model_ids:
        item_usage = str(item.get("usage"))
        item_model_id = item.get("model_id")
        if item_model_id and item_usage in new_ids_by_usage:
            new_ids_by_usage[item_usage].add(str(item_model_id))

    new_by_usage_and_signature: dict[tuple[str, str], list[dict]] = {}
    for item in new_model_ids:
        usage = str(item.get("usage"))
        model_id = item.get("model_id")
        if usage not in new_ids_by_usage or not model_id:
            continue
        signature = _model_entry_signature(item)
        new_by_usage_and_signature.setdefault((usage, signature), []).append(item)

    disappeared_old_ids_by_group: dict[tuple[str, str], set[str]] = {}
    for (usage, old_model_id), old_item in old_by_usage_and_id.items():
        if usage not in new_ids_by_usage or old_model_id in new_ids_by_usage[usage]:
            continue
        signature = _model_entry_signature(old_item)
        disappeared_old_ids_by_group.setdefault((usage, signature), set()).add(old_model_id)

    added_new_ids_by_group: dict[tuple[str, str], set[str]] = {}
    for (usage, signature), items in new_by_usage_and_signature.items():
        for item in items:
            new_model_id = str(item["model_id"])
            if new_model_id not in old_ids_by_usage[usage]:
                added_new_ids_by_group.setdefault((usage, signature), set()).add(new_model_id)

    return {
        "old_by_usage_and_id": old_by_usage_and_id,
        "old_ids_by_usage": old_ids_by_usage,
        "new_ids_by_usage": new_ids_by_usage,
        "new_by_usage_and_signature": new_by_usage_and_signature,
        "disappeared_old_ids_by_group": disappeared_old_ids_by_group,
        "added_new_ids_by_group": added_new_ids_by_group,
    }


def _compute_model_id_renames(
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    referenced_model_ids: dict[str, set[str]] | None = None,
    rename_index: dict[str, dict] | None = None,
) -> dict[str, dict[str, str]]:
    # 仅对配置实际引用且已消失的旧模型 ID，按变更组唯一匹配新模型。
    index = rename_index or _build_model_id_rename_index(old_model_ids, new_model_ids)
    old_by_usage_and_id = index["old_by_usage_and_id"]
    old_ids_by_usage = index["old_ids_by_usage"]
    new_ids_by_usage = index["new_ids_by_usage"]
    disappeared_old_ids_by_group = index.get("disappeared_old_ids_by_group", {})
    added_new_ids_by_group = index.get("added_new_ids_by_group", {})

    referenced_ids_by_usage = referenced_model_ids or old_ids_by_usage
    renames: dict[str, dict[str, str]] = {}

    for usage, model_ids in referenced_ids_by_usage.items():
        if usage not in old_ids_by_usage:
            continue
        for old_model_id in model_ids:
            if old_model_id in new_ids_by_usage[usage]:
                continue

            old_item = old_by_usage_and_id.get((usage, old_model_id))
            if not old_item:
                continue

            signature = _model_entry_signature(old_item)
            group = (usage, signature)
            old_ids = disappeared_old_ids_by_group.get(group, set())
            new_ids = added_new_ids_by_group.get(group, set())
            if len(old_ids) != 1 or len(new_ids) != 1:
                continue

            renames.setdefault(usage, {})[old_model_id] = next(iter(new_ids))

    return renames


def _apply_model_id_renames_to_configs(
    configs: dict,
    channel_id: int,
    renames: dict[str, dict[str, str]],
) -> int:
    channel_config = configs.get("channel") or {}
    updated_count = 0

    for channel_key, usage in CHANNEL_USAGE_MAP.items():
        rename_map = renames.get(usage)
        if not rename_map:
            continue
        channel = channel_config.get(channel_key)
        if not channel or not isinstance(channel.get("rules"), list):
            continue
        for rule in channel["rules"]:
            if _is_same_channel(rule, channel_id):
                old_model_id = str(rule.get("model_id"))
                if old_model_id in rename_map:
                    rule["model_id"] = rename_map[old_model_id]
                    updated_count += 1

    return updated_count


def _sync_channel_model_id_renames_in_configs(
    configs: dict,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    rename_index: dict[str, dict],
) -> int:
    referenced_model_ids = _collect_channel_rule_model_ids(configs, channel_id)
    renames = _compute_model_id_renames(
        old_model_ids,
        new_model_ids,
        referenced_model_ids,
        rename_index,
    )
    if not renames:
        return 0

    return _apply_model_id_renames_to_configs(configs, channel_id, renames)


async def _sync_channel_model_id_renames(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    *,
    apply_changes: bool = True,
) -> int:
    """批量同步Profile中的模型ID重命名。

    采用游标分页避免 offset 窗口错位，调用方负责统一提交事务。
    """
    batch_size = 100
    last_id = 0
    total_updated = 0
    rename_index = _build_model_id_rename_index(old_model_ids, new_model_ids)

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = deepcopy(profile.configs or {})
            profile_updated_count = _sync_channel_model_id_renames_in_configs(
                configs,
                channel_id,
                old_model_ids,
                new_model_ids,
                rename_index,
            )
            if profile_updated_count > 0:
                total_updated += profile_updated_count
                if apply_changes:
                    profile.configs = configs
                    flag_modified(profile, "configs")
                    db.add(profile)
                    batch_changed = True

        if apply_changes and batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_updated


def _get_chat_model_ids(model_ids: list[dict]) -> set[str]:
    result: set[str] = set()
    for item in model_ids:
        if str(item.get("usage")) == ModelUsage.CHAT.value and item.get("model_id"):
            result.add(str(item.get("model_id")))
    return result


def _clear_unavailable_audit_model_refs_from_configs(
    configs: dict,
    channel_id: int,
    available_chat_model_ids: set[str],
) -> int:
    security_config = configs.get("security") if isinstance(configs.get("security"), dict) else {}
    audit_channel_id = security_config.get("audit_channel_id")
    audit_model_id = security_config.get("audit_model_id")
    if str(audit_channel_id) != str(channel_id):
        return 0

    if audit_model_id and str(audit_model_id) in available_chat_model_ids:
        return 0

    if audit_channel_id is None and audit_model_id is None:
        return 0

    security_config["audit_channel_id"] = None
    security_config["audit_model_id"] = None
    configs["security"] = security_config
    return 1


def _sync_audit_model_id_renames_in_configs(
    configs: dict,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    rename_index: dict[str, dict],
) -> int:
    security_config = configs.get("security") if isinstance(configs.get("security"), dict) else {}
    audit_model_id = security_config.get("audit_model_id")
    if str(security_config.get("audit_channel_id")) != str(channel_id) or not audit_model_id:
        return 0

    renames = _compute_model_id_renames(
        old_model_ids,
        new_model_ids,
        {ModelUsage.CHAT.value: {str(audit_model_id)}},
        rename_index,
    )
    new_model_id = renames.get(ModelUsage.CHAT.value, {}).get(str(audit_model_id))
    if not new_model_id:
        return 0

    security_config["audit_model_id"] = new_model_id
    configs["security"] = security_config
    return 1


async def _sync_audit_model_id_renames(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    *,
    apply_changes: bool = True,
) -> int:
    """同步引用该渠道的 Profile 审计模型重命名，调用方负责统一提交事务。"""
    batch_size = 100
    last_id = 0
    total_updated = 0
    rename_index = _build_model_id_rename_index(old_model_ids, new_model_ids)

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = deepcopy(profile.configs or {})
            profile_updated_count = _sync_audit_model_id_renames_in_configs(
                configs,
                channel_id,
                old_model_ids,
                new_model_ids,
                rename_index,
            )
            if profile_updated_count == 0:
                continue

            total_updated += profile_updated_count
            if apply_changes:
                profile.configs = configs
                flag_modified(profile, "configs")
                db.add(profile)
                batch_changed = True

        if apply_changes and batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_updated


async def _clear_unavailable_audit_model_refs(
    db: AsyncSession,
    channel_id: int,
    model_ids: list[dict],
    *,
    apply_changes: bool = True,
) -> int:
    """清理引用已不可用审计模型的 Profile 安全配置，调用方负责统一提交事务。"""
    available_chat_model_ids = _get_chat_model_ids(model_ids)
    batch_size = 100
    last_id = 0
    total_cleared = 0

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        batch_changed = False
        for profile in profiles:
            configs = deepcopy(profile.configs or {})
            profile_cleared_count = _clear_unavailable_audit_model_refs_from_configs(
                configs,
                channel_id,
                available_chat_model_ids,
            )
            if profile_cleared_count == 0:
                continue

            total_cleared += profile_cleared_count
            if apply_changes:
                profile.configs = configs
                flag_modified(profile, "configs")
                db.add(profile)
                batch_changed = True

        if apply_changes and batch_changed:
            await db.flush()

        last_id = profiles[-1].id or last_id

    return total_cleared


async def _preview_channel_model_update_impacts(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict],
    new_model_ids: list[dict],
    *,
    confirmation_fingerprints: list[str] | None = None,
) -> dict[str, int]:
    impacts = {
        "synced_profile_rules": 0,
        "removed_profile_rules": 0,
        "synced_audit_refs": 0,
        "cleared_audit_refs": 0,
    }
    batch_size = 100
    last_id = 0
    rename_index = _build_model_id_rename_index(old_model_ids, new_model_ids)
    available_chat_model_ids = _get_chat_model_ids(new_model_ids)

    while True:
        result = await db.execute(select(profile_crud.model).where(profile_crud.model.id > last_id).order_by(profile_crud.model.id.asc()).limit(batch_size))
        profiles = list(result.scalars().all())
        if not profiles:
            break

        for profile in profiles:
            configs = deepcopy(profile.configs or {})
            original_configs = deepcopy(configs) if confirmation_fingerprints is not None else None
            synced_profile_rules = _sync_channel_model_id_renames_in_configs(
                configs,
                channel_id,
                old_model_ids,
                new_model_ids,
                rename_index,
            )
            removed_profile_rules = _clean_channel_rules_from_configs(
                configs,
                channel_id,
                new_model_ids,
            )
            synced_audit_refs = _sync_audit_model_id_renames_in_configs(
                configs,
                channel_id,
                old_model_ids,
                new_model_ids,
                rename_index,
            )
            cleared_audit_refs = _clear_unavailable_audit_model_refs_from_configs(
                configs,
                channel_id,
                available_chat_model_ids,
            )
            impacts["synced_profile_rules"] += synced_profile_rules
            impacts["removed_profile_rules"] += removed_profile_rules
            impacts["synced_audit_refs"] += synced_audit_refs
            impacts["cleared_audit_refs"] += cleared_audit_refs

            profile_impact_count = synced_profile_rules + removed_profile_rules + synced_audit_refs + cleared_audit_refs
            if confirmation_fingerprints is not None and profile_impact_count > 0:
                confirmation_fingerprints.append(
                    json.dumps(
                        {
                            "profile_id": profile.id,
                            "before": original_configs,
                            "after": configs,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                )

        last_id = profiles[-1].id or last_id

    return impacts
