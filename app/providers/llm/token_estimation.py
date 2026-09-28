import json
from typing import Any

from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage


def _redact_audio_payload_data(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_audio_payload_data(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted = {key: _redact_audio_payload_data(item) for key, item in value.items()}
    if value.get("type") == "input_audio" and isinstance(redacted.get("input_audio"), dict):
        redacted["input_audio"]["data"] = "[audio-data]"
    return redacted


def estimate_request_tokens_locally(
    transformer: Any,
    *,
    model_id: str | None,
    protocol: str,
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
) -> int:
    """Estimate only where a pre-request local size decision is unavoidable.

    The Transformer owns the provider payload shape. This estimate must not be
    used as authoritative session usage, a context-summary trigger, or a hard
    request limit.
    """
    input_payload = transformer.build_input_token_payload(
        model_id=model_id,
        messages=messages,
        tools=tools,
    )
    serialized_payload = json.dumps(
        _redact_audio_payload_data(input_payload),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    raw_local_tokens = estimate_tokens(
        serialized_payload,
        model_id=model_id,
        protocol=protocol,
    )
    return raw_local_tokens
