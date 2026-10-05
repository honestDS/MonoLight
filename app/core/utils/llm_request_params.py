from typing import Any

from app.core.constants import (
    CONTEXT_SUMMARY_TOP_P,
    MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    MEMORY_RECALL_PRECHECK_TEMPERATURE,
    MEMORY_RECALL_PRECHECK_TOP_P,
    SESSION_TITLE_MAX_OUTPUT_TOKENS,
    SESSION_TITLE_TOP_P,
)


def clean_generation_params(
    params: dict[str, Any],
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
) -> dict[str, Any]:
    """清理内部任务生成参数，内部任务不主动设置思考等级。"""
    cleaned = {key: value for key, value in params.items() if value is not None and key != "reasoning_effort"}
    normalized_protocol = protocol.strip().lower()
    configured_reasoning = (model_entry or {}).get("reasoning_effort")

    if configured_reasoning is not None and normalized_protocol in {"openai", "openai_responses"}:
        cleaned.pop("temperature", None)
        cleaned.pop("top_p", None)

    return cleaned


def build_internal_task_generation_params(
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
    temperature: float | None,
    top_p: float | None,
    max_tokens: int,
) -> dict[str, Any]:
    """构造内部 LLM 任务参数，不继承或指定思考等级。"""
    params = clean_generation_params(
        {
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        },
        model_entry=model_entry,
        protocol=protocol,
    )
    configured_max_tokens = (model_entry or {}).get("max_tokens")
    if isinstance(configured_max_tokens, int) and not isinstance(configured_max_tokens, bool) and configured_max_tokens > 0:
        params["max_tokens"] = min(params["max_tokens"], configured_max_tokens)
    return params


def build_memory_recall_precheck_generation_params(
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
) -> dict[str, Any]:
    return build_internal_task_generation_params(
        model_entry=model_entry,
        protocol=protocol,
        temperature=MEMORY_RECALL_PRECHECK_TEMPERATURE,
        top_p=MEMORY_RECALL_PRECHECK_TOP_P,
        max_tokens=MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    )


def build_session_title_generation_params(
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
) -> dict[str, Any]:
    return build_internal_task_generation_params(
        model_entry=model_entry,
        protocol=protocol,
        temperature=(model_entry or {}).get("temperature"),
        top_p=SESSION_TITLE_TOP_P,
        max_tokens=SESSION_TITLE_MAX_OUTPUT_TOKENS,
    )


def build_context_summary_generation_params(
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
    max_output_tokens: int,
) -> dict[str, Any]:
    return build_internal_task_generation_params(
        model_entry=model_entry,
        protocol=protocol,
        temperature=(model_entry or {}).get("temperature"),
        top_p=CONTEXT_SUMMARY_TOP_P,
        max_tokens=max_output_tokens,
    )


__all__ = [
    "build_context_summary_generation_params",
    "build_internal_task_generation_params",
    "build_memory_recall_precheck_generation_params",
    "build_session_title_generation_params",
    "clean_generation_params",
]
