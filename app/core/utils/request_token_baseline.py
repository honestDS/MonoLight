import json
from typing import Any

from app.core.utils.context_messages import is_context_summary_message, message_token_text
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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
