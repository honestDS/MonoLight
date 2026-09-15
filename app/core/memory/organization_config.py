from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED,
    MEMORY_ORGANIZE_CONTEXT_SAFETY_MARGIN_TOKENS,
    MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
    MEMORY_ORGANIZE_POLICY_VERSION,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.memory.errors import MemoryConflictError, MemoryValidationError
from app.core.memory.normalization import (
    _normalize_uid,
    _validate_commit,
)
from app.core.memory.organization_types import (
    MemoryOrganizationSnapshotItem,
)
from app.core.utils.http_proxy import get_channel_http_proxy
from app.models.channel import (
    ChannelModelItem,
    ModelUsage,
    resolve_model_protocol,
)
from app.models.memory import (
    LongTermMemoryStore,
)

from .organization_contracts import MemoryOrganizationModelConfig
from .organization_validation import (
    calculate_organization_required_input_tokens,
    calculate_organization_required_output_tokens,
)

__all__ = [
    "build_organization_model_config_for_channel_values",
    "load_organization_model_config",
    "load_organization_model_config_for_store",
    "get_organization_settings",
    "update_organization_settings",
]


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


def _raise_organization_context_exceeded(*, required_tokens: int, available_tokens: int) -> None:
    raise MemoryValidationError(
        ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
        params={"required_tokens": required_tokens, "available_tokens": available_tokens},
        data={
            "required_tokens": required_tokens,
            "available_tokens": available_tokens,
        },
    )


def build_organization_model_config_for_channel_values(
    store: LongTermMemoryStore,
    *,
    channel_id: Any,
    channel_name: Any,
    channel_is_active: Any,
    base_url: Any,
    api_key: Any,
    http_proxy: Any,
    model_ids: Any,
    model_id: Any,
    snapshot_count: int,
    snapshot_items: Iterable[MemoryOrganizationSnapshotItem] | None = None,
) -> MemoryOrganizationModelConfig:
    normalized_channel_id, normalized_model_id = _validate_organization_selection(channel_id, model_id)
    if normalized_channel_id is None or normalized_model_id is None:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED)
    normalized_snapshot_items = tuple(snapshot_items) if snapshot_items is not None else None
    required_output_tokens = calculate_organization_required_output_tokens(snapshot_count)
    if normalized_snapshot_items is not None and len(normalized_snapshot_items) != snapshot_count:
        _raise_organization_config_invalid()

    if channel_is_active is not True or not _is_valid_organization_base_url(base_url):
        _raise_organization_config_invalid()

    try:
        normalized_http_proxy = get_channel_http_proxy({"http_proxy": http_proxy})
    except Exception:
        _raise_organization_config_invalid()
    if not isinstance(api_key, str) or not api_key.strip():
        _raise_organization_config_invalid()

    selected_item: ChannelModelItem | None = None
    context_window_k: Any = None
    max_tokens: Any = None
    for raw_item in model_ids or []:
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
        _raise_organization_context_exceeded(required_tokens=required_output_tokens, available_tokens=max_tokens)

    context_window_tokens = context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
    if normalized_snapshot_items is not None:
        required_input_tokens = calculate_organization_required_input_tokens(normalized_snapshot_items)
        available_input_tokens = context_window_tokens - required_output_tokens - MEMORY_ORGANIZE_CONTEXT_SAFETY_MARGIN_TOKENS
        if required_input_tokens > available_input_tokens:
            _raise_organization_context_exceeded(
                required_tokens=required_input_tokens,
                available_tokens=available_input_tokens,
            )
    return MemoryOrganizationModelConfig(
        channel_id=normalized_channel_id,
        channel_name=channel_name,
        model_id=selected_item.model_id,
        usage=selected_item.usage.value,
        protocol=resolve_model_protocol({"protocol": selected_item.protocol.value}),
        base_url=base_url,
        api_key=api_key,
        http_proxy=normalized_http_proxy,
        custom_headers=selected_item.advanced_settings.custom_headers,
        temperature=selected_item.temperature if selected_item.temperature is not None else 0.7,
        top_p=selected_item.top_p,
        reasoning_effort=selected_item.reasoning_effort,
        timeout=MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
        context_window_k=context_window_k,
        context_window_tokens=context_window_tokens,
        max_tokens=max_tokens,
        snapshot_count=snapshot_count,
        required_output_tokens=required_output_tokens,
        policy_version=store.organization_policy_version,
    )


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

    channel = await channel_crud.get(db, normalized_channel_id)
    if channel is None:
        _raise_organization_config_invalid()
    if channel.is_active is not True or not _is_valid_organization_base_url(channel.base_url):
        _raise_organization_config_invalid()

    try:
        api_key = channel.get_decrypted_api_key()
        http_proxy = getattr(channel, "http_proxy", None)
    except Exception:
        _raise_organization_config_invalid()
    return build_organization_model_config_for_channel_values(
        store,
        channel_id=normalized_channel_id,
        channel_name=channel.name,
        channel_is_active=channel.is_active,
        base_url=channel.base_url,
        api_key=api_key,
        http_proxy=http_proxy,
        model_ids=channel.model_ids,
        model_id=normalized_model_id,
        snapshot_count=snapshot_count,
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
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "auto_organize_enabled"})
    if (organization_channel_id is None) != (organization_model_id is None):
        _raise_organization_config_invalid()
    if organization_channel_id is not None and organization_model_id is not None:
        _validate_organization_selection(organization_channel_id, organization_model_id)

    try:
        if organization_channel_id is not None and organization_model_id is not None:
            channel = await channel_crud.lock_for_mutation(
                db,
                channel_id=organization_channel_id,
                commit=False,
            )
            if channel is None:
                _raise_organization_config_invalid()
        store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        active_count = await memory_record_crud.count_active(db, uid=normalized_uid)
        if organization_channel_id is not None and organization_model_id is not None:
            await _load_organization_model_config_for_selection(
                db,
                store=store,
                channel_id=organization_channel_id,
                model_id=organization_model_id,
                snapshot_count=active_count,
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
        if auto_organize_enabled and active_count >= updated_store.organize_trigger_records:
            from app.core.memory_jobs.manager import memory_job_manager

            await memory_job_manager.submit_auto_organization(db, uid=normalized_uid, commit=False)
        if normalized_commit:
            await db.commit()
        return await get_organization_settings(db, uid=normalized_uid, snapshot_count=active_count)
    except Exception:
        await db.rollback()
        raise
