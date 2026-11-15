import json
from dataclasses import dataclass, field

from app.core.audit.visibility import obscure_audit_tool_result
from app.core.constants import TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY
from app.core.prompts import EXTERNAL_RESULT_REPLY_GUIDANCE_PROMPT, SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.utils.assistant_files import model_visible_message_content
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
    decode_message_content,
)


def _is_valid_message_id(message_id: object) -> bool:
    return isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0


def _parse_json_object(content: str | None) -> dict[str, object] | None:
    try:
        payload = json.loads(content or "")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_tool_call_ids(message: Message) -> tuple[str, ...]:
    if message.role != MessageRole.ASSISTANT or message.type != MessageType.TOOL_CALL:
        return ()
    payload = _parse_json_object(message.content)
    raw_tool_calls = payload.get("tool_calls") if payload is not None else None
    if not isinstance(raw_tool_calls, list):
        return ()
    return tuple(tool_call["id"] for tool_call in raw_tool_calls if isinstance(tool_call, dict) and isinstance(tool_call.get("id"), str) and tool_call["id"].strip())


def _parse_tool_result_shell(message: Message) -> tuple[dict[str, object], str, int | None, bool] | None:
    if message.role != MessageRole.TOOL or message.type != MessageType.TOOL_RESULT:
        return None
    payload = _parse_json_object(message.content)
    tool_call_id = payload.get("tool_call_id") if payload is not None else None
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        return None

    provider_metadata = message.provider_metadata
    has_explicit_source = isinstance(provider_metadata, dict) and TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY in provider_metadata
    source_message_id = provider_metadata.get(TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY) if has_explicit_source else None
    if has_explicit_source and not _is_valid_message_id(source_message_id):
        return None
    return payload, tool_call_id, source_message_id, has_explicit_source


def is_tool_result_covered_by_summary(message: Message, summary_message_id: int | None) -> bool:
    if not _is_valid_message_id(summary_message_id):
        return False
    parsed_shell = _parse_tool_result_shell(message)
    if parsed_shell is None:
        return False
    _payload, _tool_call_id, source_message_id, has_explicit_source = parsed_shell
    if not has_explicit_source:
        return False
    if not _is_valid_message_id(source_message_id):
        return False
    if source_message_id > summary_message_id:
        return False
    return True


@dataclass(slots=True)
class StoredToolCallState:
    session_id: str
    uid: str
    work_id: int | None = None
    pending: dict[tuple[int, str], int] = field(default_factory=dict)
    latest: dict[tuple[int, str], int] = field(default_factory=dict)

    def _in_scope(self, message: Message) -> bool:
        return message.session_id == self.session_id and message.uid == self.uid and message.work_id == self.work_id

    def observe(self, message: Message) -> None:
        if not self._in_scope(message):
            return

        if message.role == MessageRole.ASSISTANT and message.type == MessageType.TOOL_CALL:
            if not _is_valid_message_id(message.id):
                return
            for tool_call_id in _parse_tool_call_ids(message):
                key = (message.id, tool_call_id)
                self.pending[key] = message.profile_id
                self.latest[(message.profile_id, tool_call_id)] = message.id
            return

        parsed_result = _parse_tool_result_shell(message)
        if parsed_result is None:
            return
        _payload, tool_call_id, source_message_id, has_explicit_source = parsed_result
        if not has_explicit_source:
            source_message_id = self.latest.get((message.profile_id, tool_call_id))
        if source_message_id is None:
            return

        key = (source_message_id, tool_call_id)
        if self.pending.get(key) == message.profile_id:
            self.pending.pop(key, None)

    def accept_late_tool_result(self, message: Message) -> tuple[int, InternalMessage] | None:
        if not self._in_scope(message) or not message.is_processed:
            return None

        parsed_shell = _parse_tool_result_shell(message)
        if parsed_shell is None:
            return None
        payload, tool_call_id, source_message_id, has_explicit_source = parsed_shell
        if not has_explicit_source:
            return None
        if source_message_id is None:
            return None
        execution_status = payload.get("tool_execution_status")
        if execution_status == "failed":
            if payload.get("tool_cancelled") is not True:
                return None
        elif execution_status != "execution_unknown":
            return None

        key = (source_message_id, tool_call_id)
        if self.pending.get(key) != message.profile_id:
            return None

        parsed_results = parse_db_messages_to_internal([message])
        if not parsed_results:
            return None

        self.pending.pop(key, None)
        return source_message_id, parsed_results[0]


def _parse_background_task_result_message(msg: Message, content: str) -> list[InternalMessage]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        payload = None

    if isinstance(payload, dict) and payload.get("type") == "background_tool_result" and payload.get("tool_cancelled") is True:
        task = payload.get("task")
        tool_call = payload.get("tool_call")
        execution_status = payload.get("execution_status")
        if not isinstance(task, dict) or task.get("status") != "cancelled" or not isinstance(task.get("error"), str):
            return []
        if not isinstance(tool_call, dict) or "id" not in tool_call or not isinstance(tool_call.get("name"), str):
            return []
        if execution_status not in {"execution_unknown", "failed"}:
            return []
        content = json.dumps(
            {
                "tool_call_id": tool_call["id"],
                "tool_name": tool_call["name"],
                "status": task["status"],
                "execution_status": execution_status,
                "error": task["error"],
            },
            ensure_ascii=False,
        )
    elif isinstance(payload, dict) and payload.get("ui_only") is True:
        task_id = payload.get("task_id")
        instruction = payload.get("instruction")
        if payload.get("type") != "background_tool_result" or not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0 or not isinstance(instruction, str) or not instruction or "task" not in payload:
            return []
        content = json.dumps(
            {
                "type": "external_work_resume",
                "instruction": instruction,
                "tasks": [payload],
                "reply_guidance": EXTERNAL_RESULT_REPLY_GUIDANCE_PROMPT,
            },
            ensure_ascii=False,
        )
    elif isinstance(payload, dict) and payload.get("type") == "background_tool_result":
        content = json.dumps(
            {
                **payload,
                "reply_guidance": EXTERNAL_RESULT_REPLY_GUIDANCE_PROMPT,
            },
            ensure_ascii=False,
        )

    return [
        InternalMessage(
            id=msg.id,
            role=MessageRole.USER,
            content=content,
            type=msg.type,
            environment_prompt=msg.environment_prompt,
            guidance_prompt=msg.guidance_prompt,
            attachments=msg.attachments,
        )
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
            tool_execution_status = None
            refusal = None
            suppress_tool_result_metadata = False
            provider_metadata = msg.provider_metadata if isinstance(msg.provider_metadata, dict) else None
            execution_guidance = None
            current_session_todo = None
            current_terminal_watch = None

            # 直接检测：仅在需要的消息类型中尝试解析 JSON
            if m_type in {MessageType.AUDIT_CONFIRMATION, MessageType.AUDIT_DECISION}:
                continue
            if m_type == MessageType.BACKGROUND_TASK_RESULT:
                parsed_history.extend(_parse_background_task_result_message(msg, content))
                continue
            elif m_type == MessageType.SCHEDULED_TASK_TRIGGER:
                role = MessageRole.USER
                execution_guidance = SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
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
                            if m_type == MessageType.TOOL_RESULT:
                                current_session_todo = parsed.get("current_session_todo")
                                current_terminal_watch = parsed.get("current_terminal_watch")
                        if m_type == MessageType.TOOL_RESULT:
                            tool_execution_status = parsed.get("tool_execution_status")
                        if isinstance(parsed.get("refusal"), str):
                            refusal = parsed["refusal"]
                            content = parsed.get("content")
                        if isinstance(parsed.get("provider_metadata"), dict):
                            if not isinstance(provider_metadata, dict):
                                provider_metadata = parsed["provider_metadata"]
                            content = parsed.get("content")
                except json.JSONDecodeError:
                    # 鲁棒性退避：解析失败按原样呈现
                    pass
                if m_type == MessageType.TOOL_RESULT and isinstance(content, str):
                    try:
                        audit_payload = json.loads(content)
                    except json.JSONDecodeError:
                        audit_payload = None
                    if audit_payload is not None:
                        obscured_payload = obscure_audit_tool_result(audit_payload)
                        if obscured_payload is not audit_payload:
                            content = json.dumps(obscured_payload, ensure_ascii=False)
                            refusal = None
                            provider_metadata = None
                            suppress_tool_result_metadata = True
                    if suppress_tool_result_metadata:
                        current_session_todo = None
                        current_terminal_watch = None
                    elif msg.model_context_suffix is not None:
                        context_suffix = json.loads(msg.model_context_suffix)
                        current_session_todo = context_suffix.get("current_session_todo")
                        current_terminal_watch = context_suffix.get("current_terminal_watch")
            elif m_type == MessageType.TEXT:
                content = decode_message_content(msg.content or "", msg.content_format)

            if m_type == MessageType.TOOL_RESULT and isinstance(provider_metadata, dict) and TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY in provider_metadata:
                provider_metadata = {key: value for key, value in provider_metadata.items() if key != TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY} or None

            if role == MessageRole.ERR:
                role = MessageRole.ASSISTANT
            if m_type == MessageType.TEXT and role == MessageRole.ASSISTANT:
                content = model_visible_message_content(InternalMessage(role=role, content=content))
            if not content and refusal:
                content = refusal

            message_kwargs = {
                "id": msg.id,
                "type": m_type,
                "content": content,
                "reasoning_content": None if suppress_tool_result_metadata else msg.reasoning_content,
                "refusal": refusal,
                "provider_metadata": provider_metadata,
                "environment_prompt": msg.environment_prompt,
                "guidance_prompt": msg.guidance_prompt,
                "execution_guidance": execution_guidance,
                "attachments": msg.attachments,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
                "tool_execution_status": tool_execution_status,
                "current_session_todo": current_session_todo,
                "current_terminal_watch": current_terminal_watch,
            }
            if role == MessageRole.USER and m_type == MessageType.TEXT:
                parsed_history.append(InternalMessage.from_user_input(**message_kwargs))
            else:
                parsed_history.append(InternalMessage(role=role, **message_kwargs))
        except Exception:
            continue
    return parsed_history
