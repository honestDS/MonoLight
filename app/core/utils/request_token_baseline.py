import json
from typing import Any

from app.core.utils.context_messages import is_context_summary_message, message_token_text
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole

PROVIDER_REQUEST_ID_METADATA_KEY = "_provider_request_id"
PROVIDER_INPUT_TOKENS_METADATA_KEY = "_provider_input_tokens"
PROVIDER_CACHED_TOKENS_METADATA_KEY = "_provider_cached_tokens"
PROVIDER_OUTPUT_TOKENS_METADATA_KEY = "_provider_output_tokens"


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def build_provider_request_usage_metadata(provider_request_id: str, provider_metrics: dict[str, Any]) -> dict[str, str | int]:
    request_id = provider_request_id.strip() if isinstance(provider_request_id, str) else ""
    if not request_id or len(request_id) > 64:
        raise ValueError("provider_request_id must be a non-empty string of at most 64 characters")

    input_tokens = provider_metrics.get("input_tokens")
    if provider_metrics.get("input_tokens_source") != "provider" or not _is_positive_int(input_tokens):
        input_tokens = 0

    cached_tokens = provider_metrics.get("cached_tokens")
    cached_tokens = min(cached_tokens, input_tokens) if _is_non_negative_int(cached_tokens) and input_tokens > 0 else 0

    output_tokens = provider_metrics.get("output_tokens")
    output_tokens = output_tokens if _is_non_negative_int(output_tokens) else 0

    return {
        PROVIDER_REQUEST_ID_METADATA_KEY: request_id,
        PROVIDER_INPUT_TOKENS_METADATA_KEY: input_tokens,
        PROVIDER_CACHED_TOKENS_METADATA_KEY: cached_tokens,
        PROVIDER_OUTPUT_TOKENS_METADATA_KEY: output_tokens,
    }


def extract_provider_request_usage(metadata: Any) -> tuple[str, int, int, int] | None:
    if not isinstance(metadata, dict):
        return None

    request_id = metadata.get(PROVIDER_REQUEST_ID_METADATA_KEY)
    request_id = request_id.strip() if isinstance(request_id, str) else ""
    if not request_id or len(request_id) > 64:
        return None

    input_tokens = metadata.get(PROVIDER_INPUT_TOKENS_METADATA_KEY)
    cached_tokens = metadata.get(PROVIDER_CACHED_TOKENS_METADATA_KEY)
    output_tokens = metadata.get(PROVIDER_OUTPUT_TOKENS_METADATA_KEY)
    if not (_is_non_negative_int(input_tokens) and _is_non_negative_int(cached_tokens) and _is_non_negative_int(output_tokens)):
        return None
    if cached_tokens > input_tokens or (input_tokens == 0 and cached_tokens == 0 and output_tokens == 0):
        return None

    return request_id, input_tokens, cached_tokens, output_tokens


def extract_provider_token_metrics(usage: Any) -> dict[str, int | float]:
    if not isinstance(usage, dict):
        return {}

    metrics: dict[str, Any] = {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    cached_tokens = usage.get("cached_tokens")

    if _is_positive_int(prompt_tokens):
        metrics["input_tokens"] = prompt_tokens
        metrics["input_tokens_source"] = "provider"
        cached_tokens_for_rate = cached_tokens if _is_non_negative_int(cached_tokens) else 0
        metrics["cache_hit_rate"] = min(cached_tokens_for_rate, prompt_tokens) / prompt_tokens
    if _is_non_negative_int(completion_tokens):
        metrics["output_tokens"] = completion_tokens
    if _is_non_negative_int(cached_tokens):
        metrics["cached_tokens"] = cached_tokens
    return metrics


def extract_session_cache_token_totals(metadata: Any) -> tuple[int, int]:
    if not isinstance(metadata, dict):
        return 0, 0

    total_input_tokens = metadata.get("total_input_tokens")
    total_cached_tokens = metadata.get("total_cached_tokens")
    if _is_non_negative_int(total_input_tokens) and _is_non_negative_int(total_cached_tokens) and total_cached_tokens <= total_input_tokens:
        return total_input_tokens, total_cached_tokens

    input_tokens = metadata.get("input_tokens")
    if metadata.get("input_tokens_source") != "provider" or not _is_positive_int(input_tokens):
        return 0, 0

    cached_tokens = metadata.get("cached_tokens")
    cached_tokens = cached_tokens if _is_non_negative_int(cached_tokens) else 0
    return input_tokens, min(cached_tokens, input_tokens)


def merge_session_cache_token_totals(
    metadata: Any,
    *,
    total_input_tokens: Any = 0,
    total_cached_tokens: Any = 0,
) -> tuple[int, int]:
    persisted_input_tokens, persisted_cached_tokens = extract_session_cache_token_totals(metadata)
    if not (_is_non_negative_int(total_input_tokens) and _is_non_negative_int(total_cached_tokens) and total_cached_tokens <= total_input_tokens):
        total_input_tokens, total_cached_tokens = 0, 0

    return (
        max(persisted_input_tokens, total_input_tokens),
        max(persisted_cached_tokens, total_cached_tokens),
    )


def build_session_cache_metrics(total_input_tokens: Any, total_cached_tokens: Any) -> dict[str, int | float]:
    if not (_is_non_negative_int(total_input_tokens) and _is_non_negative_int(total_cached_tokens) and total_cached_tokens <= total_input_tokens):
        total_input_tokens, total_cached_tokens = 0, 0

    return {
        "total_input_tokens": total_input_tokens,
        "total_cached_tokens": total_cached_tokens,
        "cache_hit_rate": total_cached_tokens / total_input_tokens if total_input_tokens > 0 else 0.0,
    }


def accumulate_session_cache_metrics(
    provider_metrics: dict[str, Any],
    *,
    total_input_tokens: Any = 0,
    total_cached_tokens: Any = 0,
) -> tuple[int, int]:
    if not (_is_non_negative_int(total_input_tokens) and _is_non_negative_int(total_cached_tokens) and total_cached_tokens <= total_input_tokens):
        total_input_tokens, total_cached_tokens = 0, 0

    input_tokens = provider_metrics.get("input_tokens")
    if provider_metrics.get("input_tokens_source") == "provider" and _is_positive_int(input_tokens):
        cached_tokens = provider_metrics.get("cached_tokens")
        cached_tokens = cached_tokens if _is_non_negative_int(cached_tokens) else 0
        total_input_tokens += input_tokens
        total_cached_tokens += min(cached_tokens, input_tokens)

    provider_metrics.update(build_session_cache_metrics(total_input_tokens, total_cached_tokens))
    return total_input_tokens, total_cached_tokens


def extract_reusable_token_metrics(metadata: Any) -> dict[str, int | float]:
    if not isinstance(metadata, dict):
        return {}

    metrics: dict[str, int | float] = {}
    for field in ("output_tokens", "cached_tokens"):
        value = metadata.get(field)
        if _is_non_negative_int(value):
            metrics[field] = value

    cache_hit_rate = metadata.get("cache_hit_rate")
    if isinstance(cache_hit_rate, (int, float)) and not isinstance(cache_hit_rate, bool) and 0 <= cache_hit_rate <= 1:
        metrics["cache_hit_rate"] = cache_hit_rate
    return metrics


def extract_session_total_output_tokens(metadata: Any) -> int:
    if not isinstance(metadata, dict):
        return 0

    total_output_tokens = metadata.get("total_output_tokens")
    if _is_non_negative_int(total_output_tokens):
        return total_output_tokens
    output_tokens = metadata.get("output_tokens")
    return output_tokens if _is_non_negative_int(output_tokens) else 0


def _positive_message_ids(messages: list[InternalMessage]) -> list[int]:
    return [message.id for message in messages if _is_positive_int(message.id)]


def _estimate_incremental_message_tokens(message: InternalMessage) -> int:
    tokens = estimate_tokens(message_token_text(message))
    environment_prompt = message.environment_prompt
    if isinstance(environment_prompt, str) and environment_prompt and (not isinstance(message.content, str) or environment_prompt not in message.content):
        tokens += estimate_tokens(environment_prompt)
    return tokens


def build_request_token_baseline(
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
    *,
    model_id: str,
    protocol: str,
    context_summary_revision: int,
    context_content_revision: int,
) -> dict[str, Any]:
    message_ids = _positive_message_ids(messages)
    baseline: dict[str, Any] = {
        "model_id": model_id,
        "protocol": protocol,
        "context_summary_revision": context_summary_revision,
        "context_content_revision": context_content_revision,
        "system_tokens": sum(estimate_tokens(message_token_text(message)) for message in messages if message.role == MessageRole.SYSTEM),
        "tools_tokens": estimate_tokens(json.dumps(tools or [], ensure_ascii=False, separators=(",", ":"), default=str)),
    }
    if message_ids:
        baseline["request_message_min_id"] = min(message_ids)
        baseline["request_message_max_id"] = max(message_ids)
    return baseline


def estimate_incremental_input_tokens(
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
    metadata: Any,
    *,
    model_id: str,
    protocol: str,
    context_summary_revision: int,
    context_content_revision: int,
) -> int | None:
    if not isinstance(metadata, dict) or metadata.get("input_tokens_source") != "provider":
        return None

    input_tokens = metadata.get("input_tokens")
    previous_min_id = metadata.get("request_message_min_id")
    previous_max_id = metadata.get("request_message_max_id")
    if not _is_positive_int(input_tokens) or not _is_positive_int(previous_min_id) or not _is_positive_int(previous_max_id) or previous_min_id > previous_max_id:
        return None

    baseline = build_request_token_baseline(
        messages,
        tools,
        model_id=model_id,
        protocol=protocol,
        context_summary_revision=context_summary_revision,
        context_content_revision=context_content_revision,
    )
    if metadata.get("model_id") != model_id or metadata.get("protocol") != protocol:
        return None
    for field in ("context_summary_revision", "context_content_revision", "system_tokens", "tools_tokens"):
        value = metadata.get(field)
        if not _is_non_negative_int(value) or value != baseline[field]:
            return None

    current_ids = _positive_message_ids(messages)
    if not current_ids or previous_max_id not in current_ids or min(current_ids) != previous_min_id:
        return None

    incremental_tokens = input_tokens
    for message in messages:
        if message.role == MessageRole.SYSTEM or is_context_summary_message(message):
            continue
        if (_is_positive_int(message.id) and message.id > previous_max_id) or message.id is None:
            incremental_tokens += _estimate_incremental_message_tokens(message)
    return incremental_tokens
