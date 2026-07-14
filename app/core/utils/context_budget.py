import json
from dataclasses import dataclass

from app.core.constants import ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED
from app.core.exceptions import ParameterException
from app.core.utils.tokenizer import estimate_tokens


@dataclass(frozen=True)
class ContextRequestBudget:
    context_window_tokens: int
    output_tokens: int
    tools_tokens: int
    safety_margin_tokens: int
    system_tokens: int
    total_input_budget: int
    non_system_budget: int


def build_context_request_budget(
    *,
    context_window_k: int,
    max_tokens: int,
    system_tokens: int = 0,
    tools: list[dict] | None = None,
    safety_margin_tokens: int = 256,
) -> ContextRequestBudget:
    context_window_tokens = max(1, context_window_k * 1024)
    output_tokens = max(max_tokens, 0)
    tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    safety_tokens = max(safety_margin_tokens, 0)
    normalized_system_tokens = max(system_tokens, 0)
    total_input_budget = context_window_tokens - output_tokens - tools_tokens - safety_tokens
    non_system_budget = total_input_budget - normalized_system_tokens
    return ContextRequestBudget(
        context_window_tokens=context_window_tokens,
        output_tokens=output_tokens,
        tools_tokens=tools_tokens,
        safety_margin_tokens=safety_tokens,
        system_tokens=normalized_system_tokens,
        total_input_budget=total_input_budget,
        non_system_budget=non_system_budget,
    )


def ensure_context_request_budget_available(
    budget: ContextRequestBudget,
) -> None:
    if budget.total_input_budget <= 0 or budget.non_system_budget <= 0:
        raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)
