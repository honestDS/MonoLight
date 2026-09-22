from typing import Any

from app.core.constants import (
    MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    MEMORY_RECALL_PRECHECK_REASONING_EFFORT,
    MEMORY_RECALL_PRECHECK_TEMPERATURE,
    MEMORY_RECALL_PRECHECK_TOP_P,
)


def clean_generation_params(
    params: dict[str, Any],
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
) -> dict[str, Any]:
    """清理统一生成参数，只保留当前模型配置可安全覆盖的字段。"""
    cleaned = {key: value for key, value in params.items() if value is not None}
    normalized_protocol = protocol.strip().lower()
    configured_reasoning = (model_entry or {}).get("reasoning_effort")

    if configured_reasoning is not None and normalized_protocol in {"openai", "openai_responses"}:
        cleaned.pop("temperature", None)
        cleaned.pop("top_p", None)
    else:
        cleaned.pop("reasoning_effort", None)

    return cleaned


def build_memory_recall_precheck_generation_params(
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
) -> dict[str, Any]:
    params = clean_generation_params(
        {
            "temperature": MEMORY_RECALL_PRECHECK_TEMPERATURE,
            "top_p": MEMORY_RECALL_PRECHECK_TOP_P,
            "reasoning_effort": MEMORY_RECALL_PRECHECK_REASONING_EFFORT,
            "max_tokens": MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
        },
        model_entry=model_entry,
        protocol=protocol,
    )
    configured_max_tokens = (model_entry or {}).get("max_tokens")
    if isinstance(configured_max_tokens, int) and not isinstance(configured_max_tokens, bool) and configured_max_tokens > 0:
        params["max_tokens"] = min(params["max_tokens"], configured_max_tokens)
    return params


__all__ = [
    "build_memory_recall_precheck_generation_params",
    "clean_generation_params",
]
