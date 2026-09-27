import json
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import LLMException
from app.core.utils.token_calibration import apply_token_calibration
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage


@dataclass(frozen=True)
class RequestTokenEstimate:
    input_tokens: int
    source: str
    raw_local_tokens: int | None = None


_UNSUPPORTED_PROVIDER_COUNTERS: set[tuple[str, str]] = set()


def _provider_counter_key(*, protocol: str, base_url: str) -> tuple[str, str]:
    return protocol.strip().lower(), base_url.rstrip("/").lower()


def _valid_provider_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


async def resolve_request_token_estimate(
    transformer: Any,
    *,
    api_key: str,
    base_url: str,
    model_id: str,
    protocol: str,
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
    timeout: float,
    http_proxy: str | None,
    custom_headers: dict[str, str] | None,
    calibration_factor: float = 1.0,
) -> RequestTokenEstimate:
    input_payload = transformer.build_input_token_payload(
        model_id=model_id,
        messages=messages,
        tools=tools,
    )

    counter_key = _provider_counter_key(protocol=protocol, base_url=base_url)
    provider_count = None
    if counter_key not in _UNSUPPORTED_PROVIDER_COUNTERS:
        try:
            provider_count = await transformer.count_input_tokens(
                api_key=api_key,
                base_url=base_url,
                model_id=model_id,
                input_payload=input_payload,
                timeout=timeout,
                http_proxy=http_proxy,
                custom_headers=custom_headers,
            )
        except LLMException as exc:
            if exc.kwargs.get("status") in {404, 405, 501}:
                _UNSUPPORTED_PROVIDER_COUNTERS.add(counter_key)
        except Exception:
            provider_count = None

    normalized_provider_count = _valid_provider_count(provider_count)
    if normalized_provider_count is not None:
        return RequestTokenEstimate(
            input_tokens=normalized_provider_count,
            source="provider_count",
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
    return RequestTokenEstimate(
        input_tokens=apply_token_calibration(raw_local_tokens, calibration_factor),
        source="local_payload_calibrated",
        raw_local_tokens=raw_local_tokens,
    )

