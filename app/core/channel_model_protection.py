"""渠道模型引用保护检查。"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KB_CHANNEL_IN_USE,
    ERR_KB_MODEL_IDENTITY_IN_USE,
    ERR_MEMORY_CHANNEL_IN_USE,
    ERR_MEMORY_MODEL_IDENTITY_IN_USE,
)
from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.crud.memory import memory_reference_crud, memory_store_crud
from app.core.exceptions import ParameterException
from app.core.memory.channel_protection import list_memory_channel_references
from app.core.memory.errors import MemoryValidationError
from app.core.memory.organization import build_organization_model_config_for_channel_values
from app.core.utils.channel_profile_sync import (
    _build_model_id_rename_index,
    _compute_model_id_renames,
    _model_entry_signature,
)
from app.models.channel import ChannelModelItem, ModelUsage


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _model_key(item: dict[str, Any]) -> tuple[object, object] | None:
    if not isinstance(item, dict):
        return None
    model_id = item.get("model_id")
    usage = _value(item.get("usage"))
    if not isinstance(model_id, str) or not model_id or not isinstance(usage, str) or not usage:
        return None
    return model_id, usage


def _identity(item: dict[str, Any]) -> tuple[object, ...]:
    model_id, usage = _model_key(item) or (None, None)
    identity = (model_id, usage, _value(item.get("protocol")))
    if usage == ModelUsage.EMBEDDING.value:
        return (*identity, item.get("embedding_dimensions"))
    return identity


@dataclass(frozen=True, slots=True)
class MemoryOrganizationModelUpdateImpact:
    synced_settings: int = 0
    retained_settings: int = 0
    disabled_settings: int = 0

    @property
    def has_impact(self) -> bool:
        return bool(self.synced_settings or self.retained_settings or self.disabled_settings)


def _safe_model_entry_signature(item: object) -> str | None:
    try:
        return _model_entry_signature(item)  # type: ignore[arg-type]
    except Exception:
        return None


def _unique_model_entry_signature(
    model_ids: list[dict[str, Any]],
    *,
    model_id: str,
    usage: str,
) -> str | None:
    matches = [item for item in model_ids if _model_key(item) == (model_id, usage)]
    if len(matches) != 1:
        return None
    return _safe_model_entry_signature(matches[0])


def _safe_memory_organization_model_runtime_signature(item: object) -> str | None:
    try:
        normalized = ChannelModelItem.model_validate(item)
        if normalized.usage != ModelUsage.CHAT:
            return None
        return json.dumps(
            {
                "usage": normalized.usage.value,
                "protocol": normalized.protocol.value if normalized.protocol is not None else None,
                "is_enabled": normalized.is_enabled,
                "context_window_k": normalized.context_window_k,
                "max_tokens": normalized.max_tokens,
                "temperature": normalized.temperature,
                "top_p": normalized.top_p,
                "advanced_settings": normalized.advanced_settings.model_dump(mode="json"),
            },
            sort_keys=True,
            default=str,
        )
    except Exception:
        return None


def _unique_memory_organization_model_runtime_signature(
    model_ids: list[dict[str, Any]],
    *,
    model_id: str,
    usage: str,
) -> str | None:
    matches = [item for item in model_ids if _model_key(item) == (model_id, usage)]
    if len(matches) != 1:
        return None
    return _safe_memory_organization_model_runtime_signature(matches[0])


def _valid_model_entries_for_rename(model_ids: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[object, object], int] = {}
    for item in model_ids:
        key = _model_key(item)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return [item for item in model_ids if (key := _model_key(item)) is not None and counts[key] == 1 and _safe_model_entry_signature(item) is not None]


def _compute_memory_organization_model_id_renames(
    referenced_model_ids: set[str],
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
) -> dict[str, str]:
    valid_old_model_ids = _valid_model_entries_for_rename(old_model_ids)
    valid_new_model_ids = _valid_model_entries_for_rename(new_model_ids)
    try:
        renames = _compute_model_id_renames(
            valid_old_model_ids,
            valid_new_model_ids,
            {ModelUsage.CHAT.value: referenced_model_ids},
            _build_model_id_rename_index(valid_old_model_ids, valid_new_model_ids),
        )
    except Exception:
        return {}

    old_model_keys = {_model_key(item) for item in old_model_ids if _model_key(item) is not None}
    old_counts = {key: sum(_model_key(item) == key for item in old_model_ids) for key in old_model_keys}
    new_counts = {key: sum(_model_key(item) == key for item in new_model_ids) for key in {_model_key(item) for item in new_model_ids if _model_key(item) is not None}}
    old_model_ids_by_usage = {model_id for model_id, usage in old_model_keys if usage == ModelUsage.CHAT.value}

    return {old_model_id: new_model_id for old_model_id, new_model_id in renames.get(ModelUsage.CHAT.value, {}).items() if old_counts.get((old_model_id, ModelUsage.CHAT.value)) == 1 and new_counts.get((new_model_id, ModelUsage.CHAT.value)) == 1 and new_model_id not in old_model_ids_by_usage}


def _find_model_identity_update_conflict(
    referenced_model_keys: set[tuple[str, str]],
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
) -> str | None:
    old_models = {key: item for item in old_model_ids if (key := _model_key(item)) is not None}
    new_models = {key: item for item in new_model_ids if (key := _model_key(item)) is not None}

    for model_id, usage in sorted(referenced_model_keys):
        model_key = (model_id, usage)
        old_model = old_models.get(model_key)
        new_model = new_models.get(model_key)
        if old_model is None or new_model is None or _identity(old_model) != _identity(new_model):
            return model_id
    return None


def find_model_identity_update_conflict(
    referenced_model_ids: set[str],
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
) -> str | None:
    return _find_model_identity_update_conflict(
        {(model_id, ModelUsage.EMBEDDING.value) for model_id in referenced_model_ids},
        old_model_ids,
        new_model_ids,
    )


async def assert_channel_model_identity_update_allowed(
    db: AsyncSession,
    channel_id: int,
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
    *,
    allow_adaptable_memory_organization_settings: bool = False,
) -> None:
    memory_references = await list_memory_channel_references(db, channel_id=channel_id)
    memory_model_keys = {(reference.model_id, reference.usage) for reference in memory_references if reference.model_id is not None and not (allow_adaptable_memory_organization_settings and reference.is_adaptable and reference.usage == ModelUsage.CHAT.value)}
    conflict_model_id = _find_model_identity_update_conflict(memory_model_keys, old_model_ids, new_model_ids)
    if conflict_model_id is not None:
        raise ParameterException(ERR_MEMORY_MODEL_IDENTITY_IN_USE, model_id=conflict_model_id)

    knowledge_bases = await knowledge_base_crud.list_by_embedding_channel_id(db, embedding_channel_id=channel_id)
    knowledge_base_model_ids = {knowledge_base.embedding_model_id for knowledge_base in knowledge_bases}
    conflict_model_id = find_model_identity_update_conflict(knowledge_base_model_ids, old_model_ids, new_model_ids)
    if conflict_model_id is not None:
        raise ParameterException(ERR_KB_MODEL_IDENTITY_IN_USE, model_id=conflict_model_id)


async def adapt_memory_organization_settings_for_channel_model_update(
    db: AsyncSession,
    *,
    channel_id: int,
    channel_name: Any,
    channel_is_active: Any,
    base_url: Any,
    api_key: Any,
    http_proxy: Any,
    old_model_ids: list[dict[str, Any]],
    new_model_ids: list[dict[str, Any]],
    apply_changes: bool,
    api_key_loader: Callable[[], str] | None = None,
) -> MemoryOrganizationModelUpdateImpact:
    stores = await memory_reference_crud.list_all_stores_for_admin(db)
    channel_stores = [store for store in stores if store.organization_channel_id == channel_id and isinstance(store.organization_model_id, str) and store.organization_model_id]
    if not channel_stores:
        return MemoryOrganizationModelUpdateImpact()

    referenced_model_ids = {store.organization_model_id for store in channel_stores}
    renames = _compute_memory_organization_model_id_renames(
        referenced_model_ids,
        old_model_ids,
        new_model_ids,
    )

    affected_stores: list[tuple[Any, str, str | None]] = []
    for store in channel_stores:
        old_model_id = store.organization_model_id
        new_model_id = renames.get(old_model_id)
        if new_model_id is not None:
            affected_stores.append((store, "synced", new_model_id))
            continue

        old_signature = _unique_memory_organization_model_runtime_signature(
            old_model_ids,
            model_id=old_model_id,
            usage=ModelUsage.CHAT.value,
        )
        new_signature = _unique_memory_organization_model_runtime_signature(
            new_model_ids,
            model_id=old_model_id,
            usage=ModelUsage.CHAT.value,
        )
        if old_signature is None or old_signature != new_signature:
            affected_stores.append((store, "validate", old_model_id))

    if not affected_stores:
        return MemoryOrganizationModelUpdateImpact()

    resolved_api_key = api_key if isinstance(api_key, str) else (api_key_loader() if api_key_loader is not None else api_key)

    active_record_counts = await memory_reference_crud.count_active_records_by_uids(
        db,
        uids={store.uid for store, _, _ in affected_stores},
    )
    synced_settings = 0
    retained_settings = 0
    disabled_settings = 0

    for store, impact_type, model_id in affected_stores:
        try:
            build_organization_model_config_for_channel_values(
                store,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_is_active=channel_is_active,
                base_url=base_url,
                api_key=resolved_api_key,
                http_proxy=http_proxy,
                model_ids=new_model_ids,
                model_id=model_id,
                snapshot_count=active_record_counts.get(store.uid, 0),
            )
        except MemoryValidationError:
            disabled_settings += 1
            if apply_changes:
                await memory_store_crud.update_by_uid(
                    db,
                    uid=store.uid,
                    obj_in={
                        "auto_organize_enabled": False,
                        "organization_channel_id": None,
                        "organization_model_id": None,
                    },
                    commit=False,
                )
            continue

        if impact_type == "synced":
            synced_settings += 1
            if apply_changes:
                await memory_store_crud.update_by_uid(
                    db,
                    uid=store.uid,
                    obj_in={"organization_model_id": model_id},
                    commit=False,
                )
        else:
            retained_settings += 1

    return MemoryOrganizationModelUpdateImpact(
        synced_settings=synced_settings,
        retained_settings=retained_settings,
        disabled_settings=disabled_settings,
    )


async def assert_channel_not_referenced(db: AsyncSession, channel_id: int) -> None:
    if await list_memory_channel_references(db, channel_id=channel_id):
        raise ParameterException(ERR_MEMORY_CHANNEL_IN_USE)

    if await knowledge_base_crud.list_by_embedding_channel_id(db, embedding_channel_id=channel_id):
        raise ParameterException(ERR_KB_CHANNEL_IN_USE)
