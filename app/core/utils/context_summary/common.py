import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.constants import ERR_CONTEXT_SUMMARY_WORK_INVALID
from app.core.i18n import t
from app.core.prompts import CONTEXT_SUMMARY_WRAPPER
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole

ContextSummaryWorkValidityChecker = Callable[[], Awaitable[bool]]


class ContextSummaryWorkInvalidError(RuntimeError):
    pass


async def ensure_context_summary_work_valid(
    checker: ContextSummaryWorkValidityChecker | None,
) -> None:
    if checker is not None and not await checker():
        raise ContextSummaryWorkInvalidError(t(ERR_CONTEXT_SUMMARY_WORK_INVALID))


def contains_context_summary_work_invalid(exc: BaseException) -> bool:
    if isinstance(exc, ContextSummaryWorkInvalidError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(contains_context_summary_work_invalid(nested) for nested in exc.exceptions)
    return False


@dataclass(frozen=True)
class ContextSummaryState:
    content: str | None
    message_id: int | None
    revision: int = field(default=0, compare=False, repr=False)

    def as_message(self) -> InternalMessage | None:
        if not self.content or self.message_id is None:
            return None
        return InternalMessage(
            role=MessageRole.USER,
            content=CONTEXT_SUMMARY_WRAPPER.format(
                through_message_id=self.message_id,
                content=self.content,
            ),
        )


def _sanitize_summary_tool_content(content: object) -> object:
    if not isinstance(content, str):
        return content
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    if not isinstance(payload, dict) or payload.get("error") != "confirmation_required":
        return content
    return json.dumps(
        {
            "error": "security_confirmation_not_retained",
            "reason": "The tool did not run because operation-specific confirmation was required.",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def serialize_message(message: InternalMessage) -> str:
    payload = message.model_dump(
        mode="json",
        exclude={"id", "attachments", "created_at"},
        exclude_none=True,
    )
    if message.role == MessageRole.TOOL and "content" in payload:
        payload["content"] = _sanitize_summary_tool_content(payload["content"])
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def join_messages(messages: list[InternalMessage]) -> str:
    if not messages:
        return "(none)"
    return "\n".join(serialize_message(message) for message in messages)


def estimate_summary_tokens(content: str | None) -> int:
    if not content:
        return 0
    return estimate_tokens(
        CONTEXT_SUMMARY_WRAPPER.format(
            through_message_id=0,
            content=content,
        )
    )


def select_summary_segment(
    messages: list[InternalMessage],
    target_tokens: int,
) -> list[InternalMessage]:
    if len(messages) < 2:
        return []

    running_tokens = 0
    preferred_end = 0
    safe_ends: list[int] = []
    for index, message in enumerate(messages):
        running_tokens += estimate_tokens(serialize_message(message))
        next_message = messages[index + 1] if index + 1 < len(messages) else None
        if message.id is not None and next_message is not None and next_message.role == MessageRole.USER:
            safe_ends.append(index + 1)
        if running_tokens <= target_tokens:
            preferred_end = index + 1

    eligible_ends = [end for end in safe_ends if end <= preferred_end]
    if eligible_ends:
        return messages[: eligible_ends[-1]]
    return []


def select_recent_rounds(
    messages: list[InternalMessage],
    round_count: int = 2,
) -> list[InternalMessage]:
    if not messages or round_count <= 0:
        return []
    user_indices = [index for index, message in enumerate(messages) if message.role == MessageRole.USER]
    if not user_indices:
        return []
    start_index = user_indices[-min(round_count, len(user_indices))]
    return messages[start_index:]


def calc_token_usage(
    *,
    messages: list[InternalMessage],
    summary_content: str | None,
    current_message: str,
    reserved_tokens: int,
    tools: list[dict] | None,
    context_window_k: int,
    max_tokens: int,
    safety_margin_tokens: int,
    threshold_percent: int,
    history_tokens_override: int | None = None,
    history_message_count_override: int | None = None,
) -> dict[str, int]:
    summary_tokens = estimate_summary_tokens(summary_content)
    history_tokens = history_tokens_override if history_tokens_override is not None else sum(estimate_tokens(serialize_message(message)) for message in messages)
    tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    context_window_tokens = context_window_k * 1024
    output_tokens = max(max_tokens, 0)
    safety_tokens = max(safety_margin_tokens, 0)
    input_budget = max(1, context_window_tokens - output_tokens - safety_tokens)
    current_message_tokens = estimate_tokens(current_message)
    required_tokens = reserved_tokens + summary_tokens + history_tokens + current_message_tokens + tools_tokens
    summary_trigger_tokens = max(1, input_budget * threshold_percent // 100)
    return {
        "summary_tokens": summary_tokens,
        "history_tokens": history_tokens,
        "tools_tokens": tools_tokens,
        "context_window_tokens": context_window_tokens,
        "output_tokens": output_tokens,
        "safety_tokens": safety_tokens,
        "input_budget": input_budget,
        "current_message_tokens": current_message_tokens,
        "required_tokens": required_tokens,
        "summary_trigger_tokens": summary_trigger_tokens,
        "compression_goal_tokens": summary_trigger_tokens,
        "reserved_tokens": reserved_tokens,
        "threshold_percent": threshold_percent,
        "history_message_count": history_message_count_override if history_message_count_override is not None else len(messages),
    }
