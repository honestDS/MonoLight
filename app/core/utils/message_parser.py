import json
from typing import List
from app.models.message import (
    Message,
    MessageRole,
    MessageType,
    InternalMessage,
    InternalToolCall,
)


def parse_db_messages_to_internal(raw_messages: List[Message]) -> List[InternalMessage]:
    """
    将数据库存储的原始 Message 对象列表解析并转换为业务协议所需的 InternalMessage 列表。
    通过 type 字段执行直接检测，无需依赖启发式逻辑。
    """
    parsed_history: List[InternalMessage] = []
    for msg in raw_messages:
        try:
            role = MessageRole(msg.role)
            m_type = MessageType(msg.type)
            content = (msg.content or "").strip()
            tool_calls = None
            tool_call_id = None

            # 直接检测：仅在类型明确为 TOOL_CALL 或 TOOL_RESULT 时尝试解析 JSON
            if m_type == MessageType.TOOL_CALL or m_type == MessageType.TOOL_RESULT:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        if "tool_calls" in parsed:
                            tool_calls = [
                                InternalToolCall(**tc) for tc in parsed["tool_calls"]
                            ]
                            content = parsed.get("content")
                        if "tool_call_id" in parsed:
                            tool_call_id = parsed["tool_call_id"]
                            content = parsed.get("content")
                except json.JSONDecodeError:
                    # 鲁棒性退避：解析失败按原样呈现
                    pass

            parsed_history.append(
                InternalMessage(
                    role=role,
                    content=content,
                    tool_calls=tool_calls,
                    tool_call_id=tool_call_id,
                )
            )
        except Exception:
            continue
    return parsed_history
