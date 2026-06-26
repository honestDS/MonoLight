import json

from app.models.message import (
    InternalMessage,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
)


def _parse_background_task_result_message(msg: Message, content: str) -> list[InternalMessage]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return [InternalMessage(id=msg.id, role=MessageRole.SYSTEM, content=content, attachments=msg.attachments)]

    if not isinstance(payload, dict):
        return [InternalMessage(id=msg.id, role=MessageRole.SYSTEM, content=content, attachments=msg.attachments)]

    tool_call_payload = payload.get("tool_call")
    tool_result_payload = payload.get("tool_result")
    if not isinstance(tool_call_payload, dict) or not isinstance(tool_result_payload, dict):
        task_payload = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        tool_call_id = f"background_task_result_{msg.id or 'unknown'}"
        tool_call_payload = {
            "id": tool_call_id,
            "name": task_payload.get("tool_name") or "background_task",
            "arguments": {},
        }
        tool_result_payload = {
            "tool_call_id": tool_call_id,
            "content": json.dumps(task_payload or payload, ensure_ascii=False),
        }

    try:
        tool_call = InternalToolCall(**tool_call_payload)
    except Exception:
        return [InternalMessage(id=msg.id, role=MessageRole.SYSTEM, content=content, attachments=msg.attachments)]

    tool_call_id = tool_result_payload.get("tool_call_id") or tool_call.id
    tool_result_content = tool_result_payload.get("content")
    if not isinstance(tool_result_content, str):
        tool_result_content = json.dumps(tool_result_content, ensure_ascii=False)

    return [
        InternalMessage(
            id=msg.id,
            role=MessageRole.TOOL,
            content=tool_result_content,
            attachments=msg.attachments,
            tool_call_id=tool_call_id,
        ),
        InternalMessage(
            id=msg.id,
            role=MessageRole.ASSISTANT,
            content=None,
            attachments=msg.attachments,
            tool_calls=[tool_call],
        ),
    ]


def parse_db_messages_to_internal(raw_messages: list[Message]) -> list[InternalMessage]:
    """
    将数据库存储的原始 Message 对象列表解析并转换为业务协议所需的 InternalMessage 列表。
    通过 type 字段执行直接检测，无需依赖启发式逻辑。
    """
    parsed_history: list[InternalMessage] = []
    for msg in raw_messages:
        try:
            role = MessageRole(msg.role)
            m_type = MessageType(msg.type)
            content = (msg.content or "").strip()
            tool_calls = None
            tool_call_id = None

            # 直接检测：仅在类型明确为 TOOL_CALL 或 TOOL_RESULT 时尝试解析 JSON
            if m_type == MessageType.BACKGROUND_TASK_RESULT:
                parsed_history.extend(_parse_background_task_result_message(msg, content))
                continue
            elif m_type == MessageType.TOOL_CALL or m_type == MessageType.TOOL_RESULT:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        if "tool_calls" in parsed and parsed["tool_calls"] is not None:
                            tool_calls = [InternalToolCall(**tc) for tc in parsed["tool_calls"] if tc is not None]
                            content = parsed.get("content")
                        if "tool_call_id" in parsed:
                            tool_call_id = parsed["tool_call_id"]
                            content = parsed.get("content")
                except json.JSONDecodeError:
                    # 鲁棒性退避：解析失败按原样呈现
                    pass
            elif m_type == MessageType.TEXT and content.startswith("[") and content.endswith("]"):
                try:
                    parsed_content = json.loads(content)
                    if isinstance(parsed_content, list):
                        content = parsed_content
                except json.JSONDecodeError:
                    pass

            parsed_history.append(
                InternalMessage(
                    id=msg.id,
                    role=role,
                    content=content,
                    attachments=msg.attachments,
                    tool_calls=tool_calls,
                    tool_call_id=tool_call_id,
                )
            )
        except Exception:
            continue
    return parsed_history
