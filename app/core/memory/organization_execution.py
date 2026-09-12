from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    MEMORY_ORGANIZE_CONTEXT_SAFETY_MARGIN_TOKENS,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.memory.errors import MemoryValidationError
from app.core.memory.organization_types import (
    MemoryOrganizationSnapshotItem,
)
from app.core.utils.context_budget import measure_context_request_usage
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import normalize_model_custom_headers
from app.models.channel import (
    MODEL_PROTOCOLS_BY_USAGE,
    ModelProtocol,
    ModelUsage,
    resolve_model_protocol,
)
from app.models.message import InternalResponse
from app.providers.llm.client import LLMClient

from .organization_config import _is_valid_organization_base_url
from .organization_contracts import (
    MemoryOrganizationContextExceededError,
    MemoryOrganizationExecutionBudget,
    MemoryOrganizationExecutionPayload,
    MemoryOrganizationExecutionRequest,
    MemoryOrganizationModelConfig,
    MemoryOrganizationPlanCheckpoint,
    MemoryOrganizationSnapshot,
)
from .organization_payload import (
    _ORGANIZATION_MODEL_FIELDS,
    _ORGANIZATION_PAYLOAD_FIELDS,
    _ORGANIZATION_PLAN_CHECKPOINT_FIELDS,
    _ORGANIZATION_SNAPSHOT_FIELDS,
    _ORGANIZATION_TRIGGERS,
    _raise_organization_payload_invalid,
    _strict_non_negative_integer,
    _strict_number,
    _strict_positive_integer,
    _strict_text,
)
from .organization_snapshot import build_organization_snapshot_digest
from .organization_validation import (
    _build_organization_execution_messages,
    calculate_organization_required_output_tokens,
)

__all__ = [
    "restore_organization_execution_payload",
    "build_organization_execution_request",
    "call_organization_model",
    "is_external_context_length_error",
    "execute_organization_model",
]

_CONTEXT_LENGTH_ERROR_TERMS = (
    "context length exceeded",
    "maximum context length",
    "max context length",
    "context window",
    "context limit exceeded",
    "too many tokens",
    "prompt too long",
    "prompt is too long",
    "input too long",
    "input is too long",
    "input length exceeded",
    "token limit exceeded",
)


def _restore_organization_snapshot(value: Any) -> MemoryOrganizationSnapshot:
    if not isinstance(value, dict) or set(value) != _ORGANIZATION_SNAPSHOT_FIELDS:
        _raise_organization_payload_invalid()

    digest = value.get("digest")
    count = value.get("count")
    active_embedding_revision = value.get("active_embedding_revision")
    index_revision = value.get("index_revision")
    policy_version = value.get("policy_version")
    raw_items = value.get("items")
    if not _strict_text(digest) or not _strict_non_negative_integer(count) or not _strict_positive_integer(active_embedding_revision) or not _strict_non_negative_integer(index_revision) or not _strict_positive_integer(policy_version) or not isinstance(raw_items, list):
        _raise_organization_payload_invalid()

    items: list[MemoryOrganizationSnapshotItem] = []
    seen_memory_ids: set[int] = set()
    for raw_item in raw_items:
        try:
            item = MemoryOrganizationSnapshotItem.model_validate(raw_item)
        except Exception as exc:
            raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        if item.memory_id in seen_memory_ids:
            _raise_organization_payload_invalid()
        seen_memory_ids.add(item.memory_id)
        items.append(item)

    if count != len(items):
        _raise_organization_payload_invalid()
    if [item.memory_id for item in items] != sorted(item.memory_id for item in items):
        _raise_organization_payload_invalid()

    expected_digest = build_organization_snapshot_digest(
        items,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
    )
    if digest != expected_digest:
        _raise_organization_payload_invalid()
    return MemoryOrganizationSnapshot(
        digest=digest,
        count=count,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
        items=tuple(items),
    )


def _restore_organization_plan_checkpoint(value: Any) -> MemoryOrganizationPlanCheckpoint:
    if not isinstance(value, dict) or set(value) != _ORGANIZATION_PLAN_CHECKPOINT_FIELDS:
        _raise_organization_payload_invalid()
    model_output = value.get("model_output")
    usage = value.get("usage")
    finish_reason = value.get("finish_reason")
    if not isinstance(model_output, str) or not isinstance(usage, dict) or (finish_reason is not None and not isinstance(finish_reason, str)):
        _raise_organization_payload_invalid()
    return MemoryOrganizationPlanCheckpoint(
        model_output=model_output,
        usage=dict(usage),
        finish_reason=finish_reason,
    )


def _restore_organization_model_config(
    value: Any,
    *,
    snapshot: MemoryOrganizationSnapshot,
) -> MemoryOrganizationModelConfig:
    if not isinstance(value, dict) or set(value) != _ORGANIZATION_MODEL_FIELDS:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    channel_id = value.get("channel_id")
    channel_name = value.get("channel_name")
    model_id = value.get("model_id")
    usage = value.get("usage")
    raw_protocol = value.get("protocol")
    base_url = value.get("base_url")
    api_key = value.get("api_key")
    raw_http_proxy = value.get("http_proxy")
    raw_custom_headers = value.get("custom_headers")
    temperature = _strict_number(value.get("temperature"), minimum=0, maximum=2)
    raw_top_p = value.get("top_p")
    top_p = None if raw_top_p is None else _strict_number(raw_top_p, minimum=0, maximum=1)
    timeout = _strict_number(value.get("timeout"), minimum=0.000001)
    context_window_k = value.get("context_window_k")
    context_window_tokens = value.get("context_window_tokens")
    max_tokens = value.get("max_tokens")
    snapshot_count = value.get("snapshot_count")
    required_output_tokens = value.get("required_output_tokens")
    policy_version = value.get("policy_version")

    if (
        not _strict_positive_integer(channel_id)
        or not _strict_text(channel_name)
        or not _strict_text(model_id)
        or usage != ModelUsage.CHAT.value
        or not isinstance(raw_protocol, str)
        or not _strict_text(base_url)
        or not _is_valid_organization_base_url(base_url)
        or not _strict_text(api_key)
        or temperature is None
        or (raw_top_p is not None and top_p is None)
        or timeout is None
        or not _strict_positive_integer(context_window_k)
        or not _strict_positive_integer(context_window_tokens)
        or context_window_tokens != context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
        or not _strict_positive_integer(max_tokens)
        or not _strict_non_negative_integer(snapshot_count)
        or snapshot_count != snapshot.count
        or not _strict_non_negative_integer(required_output_tokens)
        or required_output_tokens != calculate_organization_required_output_tokens(snapshot.count)
        or not _strict_positive_integer(policy_version)
        or policy_version != snapshot.policy_version
        or max_tokens < required_output_tokens
    ):
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    try:
        protocol_enum = ModelProtocol(raw_protocol.upper())
        if protocol_enum not in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.CHAT]:
            raise ValueError(t(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID))
        protocol = resolve_model_protocol({"protocol": protocol_enum.value})
    except (KeyError, TypeError, ValueError) as exc:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID) from exc
    if protocol != raw_protocol:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    if not isinstance(raw_custom_headers, dict):
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)
    try:
        custom_headers = normalize_model_custom_headers(raw_custom_headers)
        http_proxy = get_channel_http_proxy({"http_proxy": raw_http_proxy})
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID) from exc
    if custom_headers != raw_custom_headers or http_proxy != raw_http_proxy:
        raise MemoryValidationError(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID)

    return MemoryOrganizationModelConfig(
        channel_id=channel_id,
        channel_name=channel_name,
        model_id=model_id,
        usage=usage,
        protocol=protocol,
        context_window_k=context_window_k,
        context_window_tokens=context_window_tokens,
        max_tokens=max_tokens,
        snapshot_count=snapshot_count,
        required_output_tokens=required_output_tokens,
        policy_version=policy_version,
        base_url=base_url,
        api_key=api_key,
        http_proxy=http_proxy,
        custom_headers=custom_headers,
        temperature=temperature,
        top_p=top_p,
        timeout=timeout,
    )


def restore_organization_execution_payload(payload: Any) -> MemoryOrganizationExecutionPayload:
    if not isinstance(payload, dict) or set(payload) not in {
        _ORGANIZATION_PAYLOAD_FIELDS,
        _ORGANIZATION_PAYLOAD_FIELDS | {"plan_checkpoint"},
    }:
        _raise_organization_payload_invalid()
    trigger = payload.get("trigger")
    if not isinstance(trigger, str) or trigger not in _ORGANIZATION_TRIGGERS:
        _raise_organization_payload_invalid()

    snapshot = _restore_organization_snapshot(payload.get("snapshot"))
    organization_model = _restore_organization_model_config(
        payload.get("organization_model"),
        snapshot=snapshot,
    )
    plan_checkpoint = _restore_organization_plan_checkpoint(payload.get("plan_checkpoint")) if "plan_checkpoint" in payload else None
    return MemoryOrganizationExecutionPayload(
        trigger=trigger,
        snapshot=snapshot,
        organization_model=organization_model,
        plan_checkpoint=plan_checkpoint,
    )


def build_organization_execution_request(payload: Any) -> MemoryOrganizationExecutionRequest:
    restored = restore_organization_execution_payload(payload)
    messages = _build_organization_execution_messages(restored.snapshot.items)
    usage = measure_context_request_usage(
        messages=list(messages),
        context_window_k=restored.organization_model.context_window_k,
        max_tokens=restored.organization_model.required_output_tokens,
        tools=None,
        safety_margin_tokens=MEMORY_ORGANIZE_CONTEXT_SAFETY_MARGIN_TOKENS,
    )
    budget = MemoryOrganizationExecutionBudget(
        required_input_tokens=usage.required_input_tokens,
        available_input_tokens=(usage.budget.context_window_tokens - usage.budget.output_tokens - usage.budget.safety_margin_tokens),
        context_window_tokens=usage.budget.context_window_tokens,
        max_output_tokens=usage.budget.output_tokens,
        safety_margin_tokens=usage.budget.safety_margin_tokens,
        system_tokens=usage.system_tokens,
        non_system_tokens=usage.non_system_tokens,
        message_tokens=usage.message_tokens,
        tools_tokens=usage.budget.tools_tokens,
    )
    return MemoryOrganizationExecutionRequest(
        trigger=restored.trigger,
        snapshot=restored.snapshot,
        organization_model=restored.organization_model,
        messages=messages,
        budget=budget,
        plan_checkpoint=restored.plan_checkpoint,
    )


async def call_organization_model(request: MemoryOrganizationExecutionRequest) -> InternalResponse:
    if request.budget.exceeds_hard_window:
        raise MemoryOrganizationContextExceededError(request.budget)
    if request.snapshot.count == 0:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    model = request.organization_model
    return await LLMClient.generate(
        api_key=model.api_key,
        base_url=model.base_url,
        model_id=model.model_id,
        messages=list(request.messages),
        temperature=model.temperature,
        top_p=model.top_p,
        max_tokens=request.budget.max_output_tokens,
        tools=None,
        protocol=model.protocol,
        timeout=model.timeout,
        request_context_tokens=request.budget.required_input_tokens,
        http_proxy=model.http_proxy,
        custom_headers=dict(model.custom_headers),
    )


def is_external_context_length_error(exc: BaseException) -> bool:
    visited: set[int] = set()

    def visit(value: Any) -> bool:
        if value is None or id(value) in visited:
            return False
        visited.add(id(value))
        if isinstance(value, str):
            normalized = value.lower().replace("_", " ").replace("-", " ")
            if any(term in normalized for term in _CONTEXT_LENGTH_ERROR_TERMS):
                return True
            return False
        if isinstance(value, Mapping):
            return any(visit(key) or visit(item) for key, item in value.items())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(visit(item) for item in value)
        if isinstance(value, BaseException):
            if not isinstance(value, LLMException) and not isinstance(exc, LLMException):
                return visit(str(value))
            if visit(str(value)):
                return True
            for attribute in (
                "message",
                "kwargs",
                "cause",
                "data",
                "code",
                "type",
                "detail",
                "__cause__",
                "__context__",
            ):
                if visit(getattr(value, attribute, None)):
                    return True
            return False
        for attribute in ("detail", "code", "type", "message"):
            if visit(getattr(value, attribute, None)):
                return True
        return visit(str(value)) if not isinstance(value, (int, float, bool)) else False

    if not isinstance(exc, LLMException):
        return False
    return visit(exc)


execute_organization_model = call_organization_model
