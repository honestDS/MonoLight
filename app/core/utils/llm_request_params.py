from typing import Any

from app.core.constants import (
    CONTEXT_SUMMARY_REASONING_EFFORT,
    CONTEXT_SUMMARY_TOP_P,
    MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    MEMORY_RECALL_PRECHECK_REASONING_EFFORT,
    MEMORY_RECALL_PRECHECK_TEMPERATURE,
    MEMORY_RECALL_PRECHECK_TOP_P,
    SESSION_TITLE_MAX_OUTPUT_TOKENS,
    SESSION_TITLE_REASONING_EFFORT,
    SESSION_TITLE_TOP_P,
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
        if isinstance(configured_reasoning, str) and configured_reasoning.strip().lower() == "none" and "reasoning_effort" in cleaned:
            cleaned["reasoning_effort"] = "none"
    else:
        cleaned.pop("reasoning_effort", None)

    return cleaned


def build_internal_task_generation_params(
    *,
    model_entry: dict[str, Any] | None,
    protocol: str,
    temperature: float | None,
    top_p: float | None,
    reasoning_effort: str | None,
    max_tokens: int,
) -> dict[str, Any]:
    """构造内部 LLM 任务参数，并统一处理思考模型与采样参数互斥关系。"""
    params = clean_generation_params(
        {
            "temperature": temperature,
            "top_p": top_p,
            "reasoning_effort": reasoning_effort,
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
        reasoning_effort=MEMORY_RECALL_PRECHECK_REASONING_EFFORT,
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
        reasoning_effort=SESSION_TITLE_REASONING_EFFORT,
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
        reasoning_effort=CONTEXT_SUMMARY_REASONING_EFFORT,
        max_tokens=max_output_tokens,
    )


__all__ = [
    "build_context_summary_generation_params",
    "build_internal_task_generation_params",
    "build_memory_recall_precheck_generation_params",
    "build_session_title_generation_params",
    "clean_generation_params",
]
