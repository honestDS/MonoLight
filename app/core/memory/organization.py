from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
    MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS,
    MEMORY_ORGANIZE_POLICY_VERSION,
)
from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_record_crud, memory_store_crud
from app.core.memory.errors import MemoryConflictError, MemoryValidationError
from app.core.memory.normalization import _normalize_dedupe_key, _normalize_uid, _validate_commit
from app.core.memory.organization_types import (
    MemoryOrganizationConflict,
    MemoryOrganizationKeep,
    MemoryOrganizationMerge,
    MemoryOrganizationPlan,
    MemoryOrganizationPlanItem,
    MemoryOrganizationSnapshotItem,
    MemoryOrganizationSourceReference,
    MemoryOrganizationTarget,
    MemoryOrganizationUpdate,
)
from app.core.utils.http_proxy import get_channel_http_proxy
from app.models.channel import ChannelModelItem, ModelUsage, resolve_model_protocol
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryRecord,
    LongTermMemoryStore,
)


class MemoryOrganizationPinPolicyStatus(StrEnum):
    MERGE = "merge"
    INVALID_PRIMARY = "invalid_primary"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class MemoryOrganizationPinPolicyResult:
    status: MemoryOrganizationPinPolicyStatus
    primary_memory_id: int | None
    pinned_memory_ids: tuple[int, ...]
    tombstone_memory_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MemoryOrganizationSnapshot:
    digest: str
    count: int
    active_embedding_revision: int
    index_revision: int
    policy_version: int
    items: tuple[MemoryOrganizationSnapshotItem, ...]

    def to_job_snapshot(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "count": self.count,
            "active_embedding_revision": self.active_embedding_revision,
            "index_revision": self.index_revision,
            "policy_version": self.policy_version,
            "items": [item.model_dump(mode="json") for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class MemoryOrganizationModelConfig:
    """Immutable organization model settings detached from database entities."""

    channel_id: int
    channel_name: str
    model_id: str
    usage: str
    protocol: str
    context_window_k: int
    context_window_tokens: int
    max_tokens: int
    snapshot_count: int
    required_output_tokens: int
    policy_version: int
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    http_proxy: str | None = field(default=None, repr=False)
    custom_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    temperature: float = 0.7
    top_p: float | None = None
    timeout: float = MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "custom_headers", MappingProxyType(dict(self.custom_headers)))

    def to_job_snapshot(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "model_id": self.model_id,
            "usage": self.usage,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "http_proxy": self.http_proxy,
            "custom_headers": dict(self.custom_headers),
            "temperature": self.temperature,
            "top_p": self.top_p,
            "timeout": self.timeout,
            "context_window_k": self.context_window_k,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "snapshot_count": self.snapshot_count,
            "required_output_tokens": self.required_output_tokens,
            "policy_version": self.policy_version,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "model_id": self.model_id,
            "usage": self.usage,
            "protocol": self.protocol,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "timeout": self.timeout,
            "context_window_k": self.context_window_k,
            "context_window_tokens": self.context_window_tokens,
            "max_tokens": self.max_tokens,
            "snapshot_count": self.snapshot_count,
            "required_output_tokens": self.required_output_tokens,
            "policy_version": self.policy_version,
        }


def calculate_organization_required_output_tokens(snapshot_count: int) -> int:
    if isinstance(snapshot_count, bool) or not isinstance(snapshot_count, int):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field="snapshot_count")
    if snapshot_count < 0:
        raise MemoryValidationError(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="snapshot_count")
    return snapshot_count * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS)


def build_organization_snapshot_items(
    records: Iterable[LongTermMemoryRecord],
) -> tuple[MemoryOrganizationSnapshotItem, ...]:
    return tuple(
        MemoryOrganizationSnapshotItem.model_validate(
            {
                "memory_id": record.id,
                "expected_version": record.version,
                "memory_key": record.memory_key,
                "memory_type": record.memory_type,
                "content": record.content,
                "content_token_count": record.content_token_count,
                "pinned": record.pinned,
            }
        )
        for record in sorted(records, key=lambda item: item.id or 0)
    )


def build_organization_snapshot_digest(
    items: Iterable[MemoryOrganizationSnapshotItem],
    *,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> str:
    ordered_items = tuple(sorted(items, key=lambda item: item.memory_id))
    digest_payload = {
        "active_embedding_revision": active_embedding_revision,
        "index_revision": index_revision,
        "items": [item.model_dump(mode="json") for item in ordered_items],
        "policy_version": policy_version,
    }
    return hashlib.sha256(canonical_json_dumps(digest_payload).encode("utf-8")).hexdigest()


def build_organization_snapshot(
    records: Iterable[LongTermMemoryRecord],
    *,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> MemoryOrganizationSnapshot:
    items = build_organization_snapshot_items(records)
    digest = build_organization_snapshot_digest(
        items,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
    )
    return MemoryOrganizationSnapshot(
        digest=digest,
        count=len(items),
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
        items=items,
    )


def build_organization_dedupe_key(
    uid: str,
    *,
    snapshot_digest: str,
    policy_version: int,
    caller_dedupe_key: str | None = None,
) -> str:
    normalized_uid = _normalize_uid(uid)
    normalized_caller_key = _normalize_dedupe_key(caller_dedupe_key) if caller_dedupe_key is not None else None
    payload = {
        "caller_dedupe_key": normalized_caller_key,
        "policy_version": policy_version,
        "scope": "memory_organization",
        "snapshot_digest": snapshot_digest,
        "uid": normalized_uid,
    }
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"memory_organization:{digest}"


def build_organization_job_payload(
    snapshot: MemoryOrganizationSnapshot,
    organization_model: MemoryOrganizationModelConfig,
) -> dict[str, Any]:
    payload = {
        "trigger": "manual",
        "snapshot": snapshot.to_job_snapshot(),
        "organization_model": organization_model.to_job_snapshot(),
    }
    canonical_json_dumps(payload)
    return payload


def validate_organization_submission_store(store: LongTermMemoryStore) -> None:
    required = (
        store.active_embedding_channel_id,
        store.active_embedding_model_id,
        store.active_embedding_dimensions,
        store.active_embedding_signature,
        store.active_collection_name,
    )
    if (
        isinstance(store.active_embedding_revision, bool)
        or not isinstance(store.active_embedding_revision, int)
        or store.active_embedding_revision < 1
        or any(value is None or (isinstance(value, str) and not value.strip()) for value in required)
        or isinstance(store.active_embedding_channel_id, bool)
        or not isinstance(store.active_embedding_channel_id, int)
        or store.active_embedding_channel_id < 1
        or isinstance(store.active_embedding_dimensions, bool)
        or not isinstance(store.active_embedding_dimensions, int)
        or store.active_embedding_dimensions < 1
    ):
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)

    try:
        index_status = LongTermMemoryIndexStatus(store.index_status)
        migration_status = LongTermMemoryMigrationStatus(store.migration_status) if store.migration_status is not None else None
    except (TypeError, ValueError) as exc:
        raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT) from exc
    if index_status != LongTermMemoryIndexStatus.READY or migration_status in {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }:
        raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)


def _raise_organization_config_invalid() -> None:
    raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)


def _validate_organization_selection(
    channel_id: Any,
    model_id: Any,
    *,
    missing_error: str = ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
) -> tuple[int | None, str | None]:
    if channel_id is None and model_id is None:
        if missing_error == ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED:
            raise MemoryValidationError(missing_error)
        return None, None
    if channel_id is None or model_id is None:
        _raise_organization_config_invalid()
    if isinstance(channel_id, bool) or not isinstance(channel_id, int) or channel_id < 1:
        _raise_organization_config_invalid()
    if not isinstance(model_id, str) or not model_id.strip() or model_id != model_id.strip():
        _raise_organization_config_invalid()
    return channel_id, model_id


def _is_valid_organization_base_url(base_url: Any) -> bool:
    if not isinstance(base_url, str) or not base_url or base_url != base_url.strip():
        return False
    if any(character.isspace() or ord(character) < 32 for character in base_url):
        return False
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    return True


def _is_positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


async def _load_organization_model_config_for_selection(
    db: AsyncSession,
    *,
    store: LongTermMemoryStore,
    channel_id: Any,
    model_id: Any,
    snapshot_count: int,
) -> MemoryOrganizationModelConfig:
    normalized_channel_id, normalized_model_id = _validate_organization_selection(channel_id, model_id)
    if normalized_channel_id is None or normalized_model_id is None:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)
    required_output_tokens = calculate_organization_required_output_tokens(snapshot_count)

    channel = await channel_crud.get(db, normalized_channel_id)
    if channel is None or channel.is_active is not True or not _is_valid_organization_base_url(channel.base_url):
        _raise_organization_config_invalid()

    try:
        api_key = channel.get_decrypted_api_key()
        http_proxy = get_channel_http_proxy(channel)
    except Exception:
        _raise_organization_config_invalid()
    if not isinstance(api_key, str) or not api_key.strip():
        _raise_organization_config_invalid()

    selected_item: ChannelModelItem | None = None
    context_window_k: Any = None
    max_tokens: Any = None
    for raw_item in channel.model_ids or []:
        if not isinstance(raw_item, dict) or raw_item.get("model_id") != normalized_model_id:
            continue
        try:
            item = ChannelModelItem.model_validate(raw_item)
        except Exception:
            continue
        if item.model_id != normalized_model_id or item.usage != ModelUsage.CHAT or item.is_enabled is not True:
            continue
        raw_context_window_k = raw_item.get("context_window_k")
        raw_max_tokens = raw_item.get("max_tokens")
        if not _is_positive_integer(raw_context_window_k) or not _is_positive_integer(raw_max_tokens):
            continue
        selected_item = item
        context_window_k = raw_context_window_k
        max_tokens = raw_max_tokens
        break

    if selected_item is None:
        _raise_organization_config_invalid()
    if max_tokens < required_output_tokens:
        raise MemoryValidationError(
            ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
            params={"required_tokens": required_output_tokens, "available_tokens": max_tokens},
            data={
                "required_tokens": required_output_tokens,
                "available_tokens": max_tokens,
            },
        )

    context_window_tokens = context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
    return MemoryOrganizationModelConfig(
        channel_id=normalized_channel_id,
        channel_name=channel.name,
        model_id=selected_item.model_id,
        usage=selected_item.usage.value,
        protocol=resolve_model_protocol({"protocol": selected_item.protocol.value}),
        base_url=channel.base_url,
        api_key=api_key,
        http_proxy=http_proxy,
        custom_headers=selected_item.advanced_settings.custom_headers,
        temperature=selected_item.temperature if selected_item.temperature is not None else 0.7,
        top_p=selected_item.top_p,
        timeout=MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
        context_window_k=context_window_k,
        context_window_tokens=context_window_tokens,
        max_tokens=max_tokens,
        snapshot_count=snapshot_count,
        required_output_tokens=required_output_tokens,
        policy_version=store.organization_policy_version,
    )


async def load_organization_model_config(
    db: AsyncSession,
    *,
    uid: str,
    snapshot_count: int,
) -> MemoryOrganizationModelConfig:
    normalized_uid = _normalize_uid(uid)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    if store is None:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)
    return await _load_organization_model_config_for_selection(
        db,
        store=store,
        channel_id=store.organization_channel_id,
        model_id=store.organization_model_id,
        snapshot_count=snapshot_count,
    )


async def load_organization_model_config_for_store(
    db: AsyncSession,
    *,
    store: LongTermMemoryStore,
    snapshot_count: int,
) -> MemoryOrganizationModelConfig:
    return await _load_organization_model_config_for_selection(
        db,
        store=store,
        channel_id=store.organization_channel_id,
        model_id=store.organization_model_id,
        snapshot_count=snapshot_count,
    )


async def get_organization_settings(
    db: AsyncSession,
    *,
    uid: str,
    snapshot_count: int,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    required_output_tokens = calculate_organization_required_output_tokens(snapshot_count)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    if store is None:
        return {
            "auto_organize_enabled": False,
            "channel_id": None,
            "model_id": None,
            "policy_version": MEMORY_ORGANIZE_POLICY_VERSION,
            "last_job_id": None,
            "last_run_at": None,
            "error": None,
            "snapshot_count": snapshot_count,
            "required_output_tokens": required_output_tokens,
            "model": None,
            "validation_error": None,
        }

    validation_error = None
    model = None
    has_selection = store.organization_channel_id is not None or store.organization_model_id is not None
    if has_selection or store.auto_organize_enabled:
        try:
            model = (await load_organization_model_config(db, uid=normalized_uid, snapshot_count=snapshot_count)).to_public_dict()
        except MemoryValidationError as exc:
            validation_error = exc.message

    return {
        "auto_organize_enabled": store.auto_organize_enabled,
        "channel_id": store.organization_channel_id,
        "model_id": store.organization_model_id,
        "policy_version": store.organization_policy_version,
        "last_job_id": store.organization_last_job_id,
        "last_run_at": store.organization_last_run_at,
        "error": store.organization_error,
        "snapshot_count": snapshot_count,
        "required_output_tokens": model["required_output_tokens"] if model is not None else required_output_tokens,
        "model": model,
        "validation_error": validation_error,
    }


async def update_organization_settings(
    db: AsyncSession,
    *,
    uid: str,
    auto_organize_enabled: bool,
    organization_channel_id: int | None,
    organization_model_id: str | None,
    commit: bool = True,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_commit = _validate_commit(commit)
    if not isinstance(auto_organize_enabled, bool):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field="auto_organize_enabled")
    if (organization_channel_id is None) != (organization_model_id is None):
        _raise_organization_config_invalid()
    if organization_channel_id is not None and organization_model_id is not None:
        _validate_organization_selection(organization_channel_id, organization_model_id)

    try:
        store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        if organization_channel_id is not None and organization_model_id is not None:
            await _load_organization_model_config_for_selection(
                db,
                store=store,
                channel_id=organization_channel_id,
                model_id=organization_model_id,
                snapshot_count=0,
            )
        elif auto_organize_enabled:
            raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)

        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=normalized_uid,
            auto_organize_enabled=auto_organize_enabled,
            organization_channel_id=organization_channel_id,
            organization_model_id=organization_model_id,
            commit=False,
        )
        if updated_store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        if normalized_commit:
            await db.commit()
        active_count = await memory_record_crud.count_active(db, uid=normalized_uid)
        return await get_organization_settings(db, uid=normalized_uid, snapshot_count=active_count)
    except Exception:
        await db.rollback()
        raise


def _deduplicate_memory_ids(memory_ids: Iterable[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    ordered_ids: list[int] = []
    for memory_id in memory_ids:
        if memory_id not in seen:
            seen.add(memory_id)
            ordered_ids.append(memory_id)
    return tuple(ordered_ids)


def evaluate_organization_merge_pins(
    source_memory_ids: Iterable[int],
    primary_memory_id: int | None,
    pinned_memory_ids: Iterable[int],
) -> MemoryOrganizationPinPolicyResult:
    """Evaluate merge candidates against the server-side pin snapshot."""
    source_ids = _deduplicate_memory_ids(source_memory_ids)
    pinned_ids = _deduplicate_memory_ids(pinned_memory_ids)
    source_id_set = set(source_ids)

    if primary_memory_id not in source_id_set:
        raise ValueError("primary_memory_id must belong to source_memory_ids")
    if not set(pinned_ids).issubset(source_id_set):
        raise ValueError("pinned_memory_ids must be a subset of source_memory_ids")

    if len(pinned_ids) > 1:
        return MemoryOrganizationPinPolicyResult(
            status=MemoryOrganizationPinPolicyStatus.CONFLICT,
            primary_memory_id=None,
            pinned_memory_ids=pinned_ids,
            tombstone_memory_ids=(),
        )

    if len(pinned_ids) == 1 and pinned_ids[0] != primary_memory_id:
        return MemoryOrganizationPinPolicyResult(
            status=MemoryOrganizationPinPolicyStatus.INVALID_PRIMARY,
            primary_memory_id=pinned_ids[0],
            pinned_memory_ids=pinned_ids,
            tombstone_memory_ids=(),
        )

    tombstone_ids = tuple(memory_id for memory_id in source_ids if memory_id != primary_memory_id)
    return MemoryOrganizationPinPolicyResult(
        status=MemoryOrganizationPinPolicyStatus.MERGE,
        primary_memory_id=primary_memory_id,
        pinned_memory_ids=pinned_ids,
        tombstone_memory_ids=tombstone_ids,
    )


__all__ = [
    "MemoryOrganizationConflict",
    "MemoryOrganizationKeep",
    "MemoryOrganizationMerge",
    "MemoryOrganizationModelConfig",
    "MemoryOrganizationPinPolicyResult",
    "MemoryOrganizationPinPolicyStatus",
    "MemoryOrganizationPlan",
    "MemoryOrganizationPlanItem",
    "MemoryOrganizationSnapshot",
    "MemoryOrganizationSnapshotItem",
    "MemoryOrganizationSourceReference",
    "MemoryOrganizationTarget",
    "MemoryOrganizationUpdate",
    "calculate_organization_required_output_tokens",
    "build_organization_dedupe_key",
    "build_organization_job_payload",
    "build_organization_snapshot",
    "build_organization_snapshot_digest",
    "build_organization_snapshot_items",
    "evaluate_organization_merge_pins",
    "get_organization_settings",
    "load_organization_model_config",
    "load_organization_model_config_for_store",
    "update_organization_settings",
    "validate_organization_submission_store",
]
