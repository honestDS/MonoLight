import json
from typing import Any

from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage


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
        input_payload,
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
