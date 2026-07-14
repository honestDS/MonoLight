import json

from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_messages_for_budget
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


def find_protected_tail_start(non_system_msgs: list[InternalMessage]) -> int:
    if not non_system_msgs:
        return 0

    last_idx = len(non_system_msgs) - 1
    if non_system_msgs[last_idx].role == MessageRole.TOOL:
        tool_call_ids = set()
        scan_idx = last_idx
        while scan_idx >= 0 and non_system_msgs[scan_idx].role == MessageRole.TOOL:
            if non_system_msgs[scan_idx].tool_call_id:
                tool_call_ids.add(non_system_msgs[scan_idx].tool_call_id)
            scan_idx -= 1

        if scan_idx >= 0:
            candidate = non_system_msgs[scan_idx]
            if candidate.role == MessageRole.ASSISTANT and candidate.tool_calls:
                required_ids = {tool_call.id for tool_call in candidate.tool_calls}
                if tool_call_ids and tool_call_ids.issubset(required_ids):
                    user_idx = scan_idx - 1
                    while user_idx >= 0:
                        if non_system_msgs[user_idx].role == MessageRole.USER:
                            return user_idx
                        user_idx -= 1
                    return scan_idx

    return last_idx


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
