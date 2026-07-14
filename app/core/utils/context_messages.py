import json

from app.core.prompts import RECENT_TOOL_SUMMARY_WRAPPER
from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_messages_for_budget, truncate_tool_result_with_stats
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole


def to_jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value


def is_context_summary_message(message: InternalMessage) -> bool:
    return isinstance(message.content, str) and message.content.startswith("<conversation_summary ")


def is_recent_tool_summary_message(message: InternalMessage) -> bool:
    return isinstance(message.content, str) and message.content.startswith("<recent_tool_summary ")


def is_synthetic_summary_message(message: InternalMessage) -> bool:
    return is_context_summary_message(message) or is_recent_tool_summary_message(message)


def message_token_text(msg: InternalMessage) -> str:
    if msg.tool_calls:
        return msg.model_dump_json(exclude_none=True)
    if isinstance(msg.content, str):
        return msg.content
    if msg.content is None:
        return ""
    if isinstance(msg.content, list):
        text_parts: list[str] = []
        for part in msg.content:
            part_type = getattr(part, "type", "")
            if part_type == "text":
                text_parts.append(str(getattr(part, "text", "") or ""))
            elif part_type == "image_url":
                text_parts.append("[图片]")
            elif part_type == "file":
                text_parts.append(f"[文件:{getattr(part, 'path', '') or ''}]")
            else:
                text_parts.append(json.dumps(to_jsonable(part), ensure_ascii=False))
        return "\n".join(item for item in text_parts if item)
    return json.dumps(to_jsonable(msg.content), ensure_ascii=False)


def find_protected_tail_start(
    non_system_msgs: list[InternalMessage],
    *,
    historical_round_count: int = 2,
) -> int:
    if not non_system_msgs:
        return 0

    user_indices = [index for index, message in enumerate(non_system_msgs) if message.role == MessageRole.USER and not is_synthetic_summary_message(message)]
    if not user_indices:
        return len(non_system_msgs)

    current_round_count = 1
    protected_round_count = historical_round_count + current_round_count
    return user_indices[-min(protected_round_count, len(user_indices))]


def _build_recent_tool_summary(
    call_message: InternalMessage,
    tool_messages: list[InternalMessage],
    *,
    content_budget_tokens: int,
) -> InternalMessage | None:
    message_ids = [message.id for message in (call_message, *tool_messages) if message.id is not None]
    if not message_ids:
        return None

    tool_names = ", ".join(tool_call.name for tool_call in call_message.tool_calls or [])
    result_lines = [f"- {tool_message.tool_call_id}: {message_token_text(tool_message)}" for tool_message in tool_messages]
    conclusion = "\n".join(
        [
            f"Tools: {tool_names or '(unknown)'}",
            "Results:",
            *(result_lines or ["- No persisted tool result was available."]),
        ]
    )
    truncated = truncate_tool_result_with_stats(
        conclusion,
        context_window_k=1,
        limit_tokens=max(1, content_budget_tokens),
    )
    return InternalMessage(
        role=MessageRole.USER,
        content=RECENT_TOOL_SUMMARY_WRAPPER.format(
            from_message_id=min(message_ids),
            through_message_id=max(message_ids),
            content=truncated.content,
        ),
    )


def replace_protected_tool_chains_for_budget(
    protected_tail: list[InternalMessage],
    *,
    non_system_budget: int,
) -> list[InternalMessage]:
    messages = [message.model_copy(deep=True) for message in protected_tail]

    while sum(estimate_tokens(message_token_text(message)) for message in messages) > non_system_budget:
        best_replacement: tuple[int, set[int], list[InternalMessage], int] | None = None

        for call_index, call_message in enumerate(messages):
            if call_message.role != MessageRole.ASSISTANT or not call_message.tool_calls:
                continue

            required_ids = {tool_call.id for tool_call in call_message.tool_calls}
            tool_indices = {index for index in range(call_index + 1, len(messages)) if messages[index].role == MessageRole.TOOL and messages[index].tool_call_id in required_ids}
            tool_messages = [messages[index] for index in sorted(tool_indices)]
            summary_message = _build_recent_tool_summary(
                call_message,
                tool_messages,
                content_budget_tokens=128,
            )
            if summary_message is None:
                continue

            replacement: list[InternalMessage] = []
            if call_message.content:
                replacement.append(
                    call_message.model_copy(
                        update={"tool_calls": None},
                        deep=True,
                    )
                )
            replacement.append(summary_message)

            original_tokens = estimate_tokens(message_token_text(call_message))
            original_tokens += sum(estimate_tokens(message_token_text(messages[index])) for index in tool_indices)
            replacement_tokens = sum(estimate_tokens(message_token_text(message)) for message in replacement)
            saved_tokens = original_tokens - replacement_tokens
            if saved_tokens <= 0:
                continue
            if best_replacement is None or saved_tokens > best_replacement[3]:
                best_replacement = (
                    call_index,
                    tool_indices,
                    replacement,
                    saved_tokens,
                )

        if best_replacement is None:
            break

        call_index, tool_indices, replacement, _saved_tokens = best_replacement
        messages = [message for index, message in enumerate(messages) if index != call_index and index not in tool_indices]
        insertion_index = min(call_index, len(messages))
        messages[insertion_index:insertion_index] = replacement

    return messages


def trim_protected_tail_tools(
    protected_tail: list[InternalMessage],
    uid: str,
    session_id: str,
    context_window_k: int,
    non_system_budget: int,
) -> list[InternalMessage]:
    tool_msgs = [msg for msg in protected_tail if msg.role == MessageRole.TOOL]
    if not tool_msgs:
        return protected_tail

    non_tool_tokens = sum(estimate_tokens(message_token_text(msg)) for msg in protected_tail if msg.role != MessageRole.TOOL)
    tool_budget = max(1, non_system_budget - non_tool_tokens)
    truncate_tool_messages_for_budget(
        tool_msgs=tool_msgs,
        context_window_k=context_window_k,
        budget_tokens=tool_budget,
        uid=uid,
        session_id=session_id,
    )

    return protected_tail
