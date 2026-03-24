import json
from typing import List
from app.models.message import Message, MessageRole, InternalMessage, InternalToolCall


def parse_db_messages_to_internal(raw_messages: List[Message]) -> List[InternalMessage]:
    """
    将数据库存储的原始 Message 对象列表解析并转换为业务协议所需的 InternalMessage 列表。
    处理内容包括：JSON 反序列化、工具调用元数据提取、角色对齐。
    """
    parsed_history: List[InternalMessage] = []
    for msg in raw_messages:
        try:
            role = MessageRole(msg.role)
            content = msg.content or ""
            tool_calls = None
            tool_call_id = None

            # 提取工具调用元数据
            # 如果是 TOOL 角色，或者 content 中包含 tool_calls 关键字，尝试进行 JSON 解析
            if role == MessageRole.TOOL or (
                role == MessageRole.ASSISTANT and "tool_calls" in content
            ):
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
                    # 若解析失败，则按普通文本 content 处理
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
            # 异常消息跳过，确保上下文链条稳定性
            continue
    return parsed_history
