import json

from app.core.prompts import SCHEDULED_TASK_TRIGGER_PROMPT
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
)


def _parse_background_task_result_message(msg: Message, content: str) -> list[InternalMessage]:
    return [
        InternalMessage(
            id=msg.id,
            role=MessageRole.USER,
            content=content,
            system_prompt=msg.system_prompt,
            attachments=msg.attachments,
        )
    ]


def _remove_completed_file_send_chains(messages: list[InternalMessage]) -> list[InternalMessage]:
    completed_tool_call_ids = {message.tool_call_id for message in messages if message.role == MessageRole.TOOL and message.tool_call_id}
    file_send_call_ids = {tool_call.id for message in messages if message.role == MessageRole.ASSISTANT and message.tool_calls for tool_call in message.tool_calls if tool_call.name == "send_file_to_user" and tool_call.id in completed_tool_call_ids}
    if not file_send_call_ids:
        return messages

    filtered_messages: list[InternalMessage] = []
    for message in messages:
        if message.role == MessageRole.TOOL and message.tool_call_id in file_send_call_ids:
            continue
        if message.role != MessageRole.ASSISTANT or not message.tool_calls:
            filtered_messages.append(message)
            continue

        remaining_tool_calls = [tool_call for tool_call in message.tool_calls if tool_call.id not in file_send_call_ids]
        if remaining_tool_calls:
            filtered_messages.append(message.model_copy(update={"tool_calls": remaining_tool_calls}))
        elif message.content:
            filtered_messages.append(message.model_copy(update={"tool_calls": None}))

    return filtered_messages


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
            elif m_type == MessageType.SCHEDULED_TASK_TRIGGER:
                role = MessageRole.USER
                content = SCHEDULED_TASK_TRIGGER_PROMPT.format(message=content)
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

            if role == MessageRole.ERR:
                role = MessageRole.ASSISTANT

            parsed_history.append(
                InternalMessage(
                    id=msg.id,
                    role=role,
                    content=content,
                    system_prompt=msg.system_prompt,
                    attachments=msg.attachments,
                    tool_calls=tool_calls,
                    tool_call_id=tool_call_id,
                )
            )
        except Exception:
            continue
    return _remove_completed_file_send_chains(parsed_history)
