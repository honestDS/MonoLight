"""渠道模型引用保护检查。"""

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CHANNEL_MODEL_LIFECYCLE_MANAGED,
    ERR_CHANNEL_MODEL_LOCKED,
    ERR_KB_CHANNEL_IN_USE,
    ERR_KB_MODEL_IDENTITY_IN_USE,
    ERR_MEMORY_CHANNEL_IN_USE,
    ERR_MEMORY_MODEL_IDENTITY_IN_USE,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_reference_crud, memory_store_crud
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import ParameterException
from app.core.memory.channel_protection import list_memory_channel_references
from app.core.memory.errors import MemoryValidationError
from app.core.memory.organization import (
    build_organization_model_config_for_channel_values,
    build_organization_snapshot_items,
)
from app.core.utils.channel_profile_sync import (
    _build_model_id_rename_index,
    _clear_unavailable_audit_model_refs,
    _compute_model_id_renames,
    _model_entry_signature,
    _remove_unavailable_channel_rules,
)
from app.models.channel import (
    ChannelModelItem,
    ChannelModelLifecycleStatus,
    ModelUsage,
    is_channel_model_pending_delete,
)
from app.models.memory import LongTermMemoryMutationOperation, LongTermMemoryMutationStatus


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
    concurrently_disabled_settings: int = 0
    deferred_settings: int = 0
    pending_deletion_models: int = 0
    confirmation_fingerprints: tuple[str, ...] = ()

    @property
    def has_impact(self) -> bool:
        return bool(self.synced_settings or self.retained_settings or self.disabled_settings or self.concurrently_disabled_settings or self.deferred_settings or self.pending_deletion_models)


@dataclass(frozen=True, slots=True)
class ChannelModelUpdatePreparation:
    persisted_model_ids: list[dict[str, Any]]
    available_model_ids: list[dict[str, Any]]
    profile_old_model_ids: list[dict[str, Any]]
    newly_pending_model_ids: frozenset[str]
    active_job_uids_by_model_id: dict[str, frozenset[str]]
    newly_pending_model_count: int


def _safe_channel_model_signature(item: object) -> str | None:
    try:
        normalized = ChannelModelItem.model_validate(item).model_dump(mode="json")
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return None


def _parse_organization_job_model_reference(payload: object) -> tuple[int, str] | None:
    if not isinstance(payload, dict):
        return None

    for field_name in ("organization_model", "model_config"):
        model_config = payload.get(field_name)
        if not isinstance(model_config, dict):
            continue

        raw_channel_id = model_config.get("channel_id")
        if isinstance(raw_channel_id, bool):
            continue
        if isinstance(raw_channel_id, int):
            parsed_channel_id = raw_channel_id
        elif isinstance(raw_channel_id, str) and raw_channel_id.isdecimal():
            try:
                parsed_channel_id = int(raw_channel_id)
            except ValueError:
                continue
        else:
            continue
        if parsed_channel_id <= 0:
            continue

        model_id = model_config.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            continue
        return parsed_channel_id, model_id

    return None


async def prepare_channel_model_update(
    db: AsyncSession,
    *,
    channel_id: int,
    old_model_ids: list[dict[str, Any]],
    requested_model_ids: list[dict[str, Any]],
) -> ChannelModelUpdatePreparation:
    persisted_old_model_ids = deepcopy(old_model_ids)
    requested_model_ids_copy = deepcopy(requested_model_ids)

    old_model_by_key: dict[tuple[object, object], dict[str, Any]] = {}
    for item in persisted_old_model_ids:
        key = _model_key(item)
        if key is not None:
            old_model_by_key.setdefault(key, item)

    requested_model_by_key: dict[tuple[object, object], dict[str, Any]] = {}
    for item in requested_model_ids_copy:
        key = _model_key(item)
        if key is not None:
            requested_model_by_key.setdefault(key, item)

    old_chat_model_by_id = {model_id: item for item in persisted_old_model_ids if (key := _model_key(item)) is not None and key[1] == ModelUsage.CHAT.value and (model_id := key[0]) is not None}

    active_job_uids: dict[str, set[str]] = {}
    active_job_statuses = {
        LongTermMemoryMutationStatus.PENDING.value,
        LongTermMemoryMutationStatus.RUNNING.value,
        LongTermMemoryMutationStatus.RETRY.value,
    }
    active_organization_jobs = await memory_job_crud.list_active_organization_jobs_for_admin(db)
    for job in active_organization_jobs:
        operation = getattr(job, "operation", None)
        if operation is not None and _value(operation) != LongTermMemoryMutationOperation.ORGANIZE.value:
            continue
        status = getattr(job, "status", None)
        if status is not None and _value(status) not in active_job_statuses:
            continue

        reference = _parse_organization_job_model_reference(getattr(job, "payload", None))
        if reference is None:
            continue
        referenced_channel_id, model_id = reference
        if referenced_channel_id != channel_id or model_id not in old_chat_model_by_id:
            continue

        active_job_uids.setdefault(model_id, set())
        job_uid = getattr(job, "uid", None)
        if isinstance(job_uid, str) and job_uid:
            active_job_uids[model_id].add(job_uid)

    persisted_model_ids: list[dict[str, Any]] = []
    handled_pending_keys: set[tuple[object, object]] = set()
    for requested_item in requested_model_ids_copy:
        key = _model_key(requested_item)
        old_item = old_model_by_key.get(key) if key is not None else None
        requested_is_pending = is_channel_model_pending_delete(requested_item)

        if requested_is_pending and (old_item is None or not is_channel_model_pending_delete(old_item)):
            raise ParameterException(ERR_CHANNEL_MODEL_LIFECYCLE_MANAGED)

        if old_item is not None and is_channel_model_pending_delete(old_item):
            if not requested_is_pending:
                raise ParameterException(ERR_CHANNEL_MODEL_LOCKED, model_id=old_item.get("model_id"))
            old_signature = _safe_channel_model_signature(old_item)
            requested_signature = _safe_channel_model_signature(requested_item)
            if old_signature is None or requested_signature is None or old_signature != requested_signature:
                raise ParameterException(ERR_CHANNEL_MODEL_LOCKED, model_id=old_item.get("model_id"))
            persisted_model_ids.append(deepcopy(old_item))
            if key is not None:
                handled_pending_keys.add(key)
            continue

        if key is not None and key[1] == ModelUsage.CHAT.value and key[0] in active_job_uids and old_item is not None:
            old_signature = _safe_channel_model_signature(old_item)
            requested_signature = _safe_channel_model_signature(requested_item)
            if old_signature is None or requested_signature is None or old_signature != requested_signature:
                raise ParameterException(ERR_CHANNEL_MODEL_LOCKED, model_id=key[0])

        persisted_model_ids.append(deepcopy(requested_item))

    for old_item in persisted_old_model_ids:
        if not is_channel_model_pending_delete(old_item):
            continue
        key = _model_key(old_item)
        if key is not None and key in handled_pending_keys:
            continue
        persisted_model_ids.append(deepcopy(old_item))
        if key is not None:
            handled_pending_keys.add(key)

    newly_pending_model_ids: set[str] = set()
    for model_id in active_job_uids:
        old_item = old_chat_model_by_id[model_id]
        key = (model_id, ModelUsage.CHAT.value)
        if key in requested_model_by_key or is_channel_model_pending_delete(old_item):
            continue

        pending_item = deepcopy(old_item)
        pending_item["lifecycle_status"] = ChannelModelLifecycleStatus.PENDING_DELETE.value
        pending_item["is_enabled"] = False
        persisted_model_ids.append(pending_item)
        newly_pending_model_ids.add(model_id)

    available_model_ids = [deepcopy(item) for item in persisted_model_ids if not is_channel_model_pending_delete(item)]
    profile_old_model_ids = []
    for old_item in persisted_old_model_ids:
        key = _model_key(old_item)
        if key is not None and key[1] == ModelUsage.CHAT.value and is_channel_model_pending_delete(old_item):
            continue
        profile_old_model_ids.append(deepcopy(old_item))

    return ChannelModelUpdatePreparation(
        persisted_model_ids=persisted_model_ids,
        available_model_ids=available_model_ids,
        profile_old_model_ids=profile_old_model_ids,
        newly_pending_model_ids=frozenset(newly_pending_model_ids),
        active_job_uids_by_model_id={model_id: frozenset(active_job_uids[model_id]) for model_id in newly_pending_model_ids},
        newly_pending_model_count=len(newly_pending_model_ids),
    )


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

    knowledge_bases = await knowledge_base_crud.list_by_embedding_channel_reference(
        db,
        embedding_channel_id=channel_id,
    )
    knowledge_base_model_ids: set[str] = set()
    for knowledge_base in knowledge_bases:
        active_embedding = resolve_active_knowledge_base_embedding(knowledge_base)
        if active_embedding.channel_id == channel_id:
            knowledge_base_model_ids.add(active_embedding.model_id)
        if knowledge_base.target_embedding_channel_id == channel_id and knowledge_base.target_embedding_model_id:
            knowledge_base_model_ids.add(knowledge_base.target_embedding_model_id)
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
    pending_deletion_uids_by_model_id: dict[str, frozenset[str]] | None = None,
    pending_deletion_models: int = 0,
) -> MemoryOrganizationModelUpdateImpact:
    pending_deletion_uids_by_model_id = pending_deletion_uids_by_model_id or {}
    confirmation_fingerprints: list[str] = []
    for pending_model_id in sorted(pending_deletion_uids_by_model_id):
        pending_uids = pending_deletion_uids_by_model_id[pending_model_id]
        confirmation_fingerprints.append(
            json.dumps(
                {
                    "pending_model_id": pending_model_id,
                    "active_job_uids": sorted(pending_uids),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )

    stores = await memory_reference_crud.list_all_stores_for_admin(db)
    channel_stores = [store for store in stores if store.organization_channel_id == channel_id and isinstance(store.organization_model_id, str) and store.organization_model_id]
    if not channel_stores:
        return MemoryOrganizationModelUpdateImpact(
            pending_deletion_models=pending_deletion_models,
            confirmation_fingerprints=tuple(sorted(confirmation_fingerprints)),
        )

    referenced_model_ids = {store.organization_model_id for store in channel_stores}
    renames = _compute_memory_organization_model_id_renames(
        referenced_model_ids,
        old_model_ids,
        new_model_ids,
    )

    affected_stores: list[tuple[Any, str, str, str]] = []
    for store in channel_stores:
        expected_model_id = store.organization_model_id
        new_model_id = renames.get(expected_model_id)
        if new_model_id is not None:
            affected_stores.append((store, "synced", expected_model_id, new_model_id))
            continue

        if expected_model_id in pending_deletion_uids_by_model_id:
            impact_type = "deferred" if store.uid in pending_deletion_uids_by_model_id[expected_model_id] else "disable"
            affected_stores.append((store, impact_type, expected_model_id, expected_model_id))
            continue

        old_signature = _unique_memory_organization_model_runtime_signature(
            old_model_ids,
            model_id=expected_model_id,
            usage=ModelUsage.CHAT.value,
        )
        new_signature = _unique_memory_organization_model_runtime_signature(
            new_model_ids,
            model_id=expected_model_id,
            usage=ModelUsage.CHAT.value,
        )
        if old_signature is None or old_signature != new_signature:
            affected_stores.append((store, "validate", expected_model_id, expected_model_id))

    if not affected_stores:
        return MemoryOrganizationModelUpdateImpact(
            pending_deletion_models=pending_deletion_models,
            confirmation_fingerprints=tuple(sorted(confirmation_fingerprints)),
        )

    has_regular_affected_stores = any(impact_type not in {"deferred", "disable"} for _, impact_type, _, _ in affected_stores)
    resolved_api_key = api_key if isinstance(api_key, str) else (api_key_loader() if has_regular_affected_stores and api_key_loader is not None else api_key)

    active_records_by_uid = await memory_reference_crud.list_organization_records_by_uids(
        db,
        uids={store.uid for store, _, _, _ in affected_stores},
    )
    synced_settings = 0
    retained_settings = 0
    disabled_settings = 0
    concurrently_disabled_settings = 0
    deferred_settings = 0

    async def recover_failed_conditional_update(
        store_uid: str,
        snapshot_items: list[Any],
    ) -> None:
        nonlocal disabled_settings, concurrently_disabled_settings

        current_store = await memory_store_crud.lock_for_mutation(
            db,
            uid=store_uid,
            commit=False,
        )
        if current_store is None or current_store.organization_channel_id != channel_id:
            return

        try:
            build_organization_model_config_for_channel_values(
                current_store,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_is_active=channel_is_active,
                base_url=base_url,
                api_key=resolved_api_key,
                http_proxy=http_proxy,
                model_ids=new_model_ids,
                model_id=current_store.organization_model_id,
                snapshot_count=len(snapshot_items),
                snapshot_items=snapshot_items,
            )
        except MemoryValidationError:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=store_uid,
                auto_organize_enabled=False,
                organization_channel_id=None,
                organization_model_id=None,
                commit=False,
            )
            if updated is not None:
                disabled_settings += 1
                concurrently_disabled_settings += 1

    async def recover_failed_pending_update(
        store_uid: str,
        *,
        expected_model_id: str,
        update_values: dict[str, Any],
    ) -> bool:
        current_store = await memory_store_crud.lock_for_mutation(
            db,
            uid=store_uid,
            commit=False,
        )
        if current_store is None or current_store.organization_channel_id != channel_id or current_store.organization_model_id != expected_model_id:
            return False

        updated = await memory_store_crud.update_by_uid(
            db,
            uid=store_uid,
            commit=False,
            **update_values,
        )
        return updated is not None

    for store, impact_type, expected_model_id, model_id in affected_stores:
        if impact_type in {"deferred", "disable"}:
            if apply_changes:
                update_values = {"auto_organize_enabled": False}
                if impact_type == "disable":
                    update_values.update(
                        {
                            "organization_channel_id": None,
                            "organization_model_id": None,
                        }
                    )
                updated = await memory_store_crud.update_auto_organize_if_channel_and_model(
                    db,
                    uid=store.uid,
                    expected_channel_id=channel_id,
                    expected_model_id=expected_model_id,
                    obj_in=update_values,
                    commit=False,
                )
                if not updated and not await recover_failed_pending_update(
                    store.uid,
                    expected_model_id=expected_model_id,
                    update_values=update_values,
                ):
                    continue

            if impact_type == "deferred":
                deferred_settings += 1
            else:
                disabled_settings += 1
            confirmation_fingerprints.append(
                json.dumps(
                    {
                        "uid": store.uid,
                        "auto_organize_enabled": store.auto_organize_enabled,
                        "expected_model_id": expected_model_id,
                        "result": impact_type,
                        "model_id": model_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
            continue

        snapshot_items = build_organization_snapshot_items(active_records_by_uid.get(store.uid, []))
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
                snapshot_count=len(snapshot_items),
                snapshot_items=snapshot_items,
            )
        except MemoryValidationError:
            if apply_changes:
                updated = await memory_store_crud.update_auto_organize_if_channel_and_model(
                    db,
                    uid=store.uid,
                    expected_channel_id=channel_id,
                    expected_model_id=expected_model_id,
                    obj_in={
                        "auto_organize_enabled": False,
                        "organization_channel_id": None,
                        "organization_model_id": None,
                    },
                    commit=False,
                )
                if not updated:
                    await recover_failed_conditional_update(store.uid, snapshot_items)
                    continue
            disabled_settings += 1
            confirmation_fingerprints.append(
                json.dumps(
                    {
                        "uid": store.uid,
                        "auto_organize_enabled": store.auto_organize_enabled,
                        "expected_model_id": expected_model_id,
                        "result": "disabled",
                        "model_id": model_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
            )
            continue

        if impact_type == "synced":
            if apply_changes:
                updated = await memory_store_crud.update_auto_organize_if_channel_and_model(
                    db,
                    uid=store.uid,
                    expected_channel_id=channel_id,
                    expected_model_id=expected_model_id,
                    obj_in={"organization_model_id": model_id},
                    commit=False,
                )
                if not updated:
                    await recover_failed_conditional_update(store.uid, snapshot_items)
                    continue
            synced_settings += 1
            confirmation_result = "synced"
        else:
            retained_settings += 1
            confirmation_result = "retained"
        confirmation_fingerprints.append(
            json.dumps(
                {
                    "uid": store.uid,
                    "auto_organize_enabled": store.auto_organize_enabled,
                    "expected_model_id": expected_model_id,
                    "result": confirmation_result,
                    "model_id": model_id,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )

    return MemoryOrganizationModelUpdateImpact(
        synced_settings=synced_settings,
        retained_settings=retained_settings,
        disabled_settings=disabled_settings,
        concurrently_disabled_settings=concurrently_disabled_settings,
        deferred_settings=deferred_settings,
        pending_deletion_models=pending_deletion_models,
        confirmation_fingerprints=tuple(sorted(confirmation_fingerprints)),
    )


async def finalize_pending_channel_model_deletions_for_organization_job(
    db: AsyncSession,
    *,
    job: Any,
) -> int:
    reference = _parse_organization_job_model_reference(getattr(job, "payload", None))
    if reference is None:
        return 0
    channel_id, model_id = reference

    channel = await channel_crud.lock_for_mutation(
        db,
        channel_id=channel_id,
        commit=False,
    )
    if channel is None:
        return 0

    model_ids = list(channel.model_ids or [])
    if not any(_model_key(item) == (model_id, ModelUsage.CHAT.value) and is_channel_model_pending_delete(item) for item in model_ids):
        return 0

    job_uid = getattr(job, "uid", None)
    await memory_store_crud.update_auto_organize_if_channel_and_model(
        db,
        uid=job_uid,
        expected_channel_id=channel_id,
        expected_model_id=model_id,
        obj_in={
            "auto_organize_enabled": False,
            "organization_channel_id": None,
            "organization_model_id": None,
        },
        commit=False,
    )

    active_organization_jobs = await memory_job_crud.list_active_organization_jobs_for_admin(db)
    for active_job in active_organization_jobs:
        active_job_uid = getattr(active_job, "uid", None)
        if active_job is job or (isinstance(job_uid, str) and job_uid and active_job_uid == job_uid):
            continue
        if _parse_organization_job_model_reference(getattr(active_job, "payload", None)) == (channel_id, model_id):
            return 0

    for store in await memory_reference_crud.list_all_stores_for_admin(db):
        await memory_store_crud.update_auto_organize_if_channel_and_model(
            db,
            uid=store.uid,
            expected_channel_id=channel_id,
            expected_model_id=model_id,
            obj_in={
                "auto_organize_enabled": False,
                "organization_channel_id": None,
                "organization_model_id": None,
            },
            commit=False,
        )

    remaining_model_ids = [deepcopy(item) for item in model_ids if not (_model_key(item) == (model_id, ModelUsage.CHAT.value) and is_channel_model_pending_delete(item))]
    deleted_count = len(model_ids) - len(remaining_model_ids)
    channel.model_ids = remaining_model_ids
    await _remove_unavailable_channel_rules(db, channel_id, remaining_model_ids)
    await _clear_unavailable_audit_model_refs(db, channel_id, remaining_model_ids)
    db.add(channel)
    await db.flush()
    return deleted_count


async def assert_channel_not_referenced(db: AsyncSession, channel_id: int) -> None:
    if await list_memory_channel_references(db, channel_id=channel_id):
        raise ParameterException(ERR_MEMORY_CHANNEL_IN_USE)

    if await knowledge_base_crud.list_by_embedding_channel_reference(
        db,
        embedding_channel_id=channel_id,
    ):
        raise ParameterException(ERR_KB_CHANNEL_IN_USE)
