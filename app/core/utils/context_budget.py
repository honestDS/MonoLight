import json
from dataclasses import dataclass

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED,
    ERR_VALUE_MUST_BE_BETWEEN,
)
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.utils.context_messages import message_token_text
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole


@dataclass(frozen=True)
class ContextRequestBudget:
    context_window_tokens: int
    output_tokens: int
    tools_tokens: int
    safety_margin_tokens: int
    system_tokens: int
    total_input_budget: int
    non_system_budget: int


@dataclass(frozen=True)
class ContextRequestUsage:
    budget: ContextRequestBudget
    system_tokens: int
    non_system_tokens: int
    message_tokens: int
    required_input_tokens: int
    summary_trigger_tokens: int

    @property
    def exceeds_hard_window(self) -> bool:
        return self.required_input_tokens > self.budget.context_window_tokens - self.budget.output_tokens - self.budget.safety_margin_tokens

    @property
    def reaches_summary_threshold(self) -> bool:
        return self.required_input_tokens >= self.summary_trigger_tokens


def count_message_tokens(messages: list[InternalMessage]) -> tuple[int, int]:
    system_tokens = 0
    non_system_tokens = 0
    for message in messages:
        token_count = estimate_tokens(message_token_text(message))
        if message.role == MessageRole.SYSTEM:
            system_tokens += token_count
        else:
            non_system_tokens += token_count
    return system_tokens, non_system_tokens


def measure_context_request_usage(
    *,
    messages: list[InternalMessage],
    context_window_k: int,
    max_tokens: int,
    tools: list[dict] | None = None,
    safety_margin_tokens: int = 256,
    threshold_percent: int = 100,
    additional_non_system_tokens: int = 0,
) -> ContextRequestUsage:
    if not 1 <= threshold_percent <= 100:
        raise ValueError(
            t(
                ERR_VALUE_MUST_BE_BETWEEN,
                field="threshold_percent",
                minimum=1,
                maximum=100,
            )
        )

    system_tokens, non_system_tokens = count_message_tokens(messages)
    non_system_tokens += max(additional_non_system_tokens, 0)
    budget = build_context_request_budget(
        context_window_k=context_window_k,
        max_tokens=max_tokens,
        system_tokens=system_tokens,
        tools=tools,
        safety_margin_tokens=safety_margin_tokens,
    )
    message_tokens = system_tokens + non_system_tokens
    required_input_tokens = message_tokens + budget.tools_tokens
    threshold_base = max(
        1,
        budget.context_window_tokens - budget.output_tokens - budget.safety_margin_tokens,
    )
    return ContextRequestUsage(
        budget=budget,
        system_tokens=system_tokens,
        non_system_tokens=non_system_tokens,
        message_tokens=message_tokens,
        required_input_tokens=required_input_tokens,
        summary_trigger_tokens=max(1, threshold_base * threshold_percent // 100),
    )


def build_context_request_budget(
    *,
    context_window_k: int,
    max_tokens: int,
    system_tokens: int = 0,
    tools: list[dict] | None = None,
    safety_margin_tokens: int = 256,
) -> ContextRequestBudget:
    context_window_tokens = max(1, context_window_k * CONTEXT_WINDOW_TOKENS_PER_K)
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
