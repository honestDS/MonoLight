import json

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
)
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.utils.context_budget import ensure_configured_request_capacity_available
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import (
    InternalMessage,
    Message,
    MessageRole,
)
from app.models.profile import (
    Profile,
)

load_dotenv()
logger = get_logger(__name__)

CONTEXT_HISTORY_PAGE_SIZE = 200


def _is_background_tool_result_message(msg: InternalMessage) -> bool:
    if not isinstance(msg.content, str):
        return False
    try:
        payload = json.loads(msg.content)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "background_tool_result"


class ContextManager:
    @classmethod
    async def _load_history_backward_by_id(
        cls,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        before_id: int | None,
        after_id: int | None,
        page_size: int = CONTEXT_HISTORY_PAGE_SIZE,
    ) -> list[Message]:
        raw_history: list[Message] = []
        page_before_id = before_id

        while True:
            page = await message_crud.get_history_backward_by_id(
                db,
                session_id=session_id,
                uid=uid,
                after_id=after_id,
                before_id=before_id,
                page_before_id=page_before_id,
                limit=page_size,
            )
            if not page:
                break

            raw_history.extend(page)
            if len(page) < page_size:
                break

            last_id = page[-1].id
            if last_id is None:
                break
            page_before_id = last_id

        return raw_history

    @classmethod
    async def get_messages(
        cls,
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        current_message: str,
        before_id: int | None = None,
        after_id: int | None = None,
        context_window_k: int = 4,
        reserved_tokens: int = 0,
    ) -> list[InternalMessage]:
        """
        获取总结边界之后的完整持久化历史，并仅做工具链协议对齐。

        历史消息在这里不再按请求预算裁剪或改写。上下文缩减统一由总结机制负责，
        最终模型请求阶段只做硬窗口预算校验。
        """
        del profile, current_message, context_window_k, reserved_tokens
        raw_history = await cls._load_history_backward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            before_id=before_id,
            after_id=after_id,
        )
        parsed_history_desc = parse_db_messages_to_internal(raw_history)
        parsed_history = list(reversed(parsed_history_desc))
        return cls.audit_tool_chain(
            parsed_history,
            uid=uid,
            session_id=session_id,
        )

    @classmethod
    def validate_request_capacity(
        cls,
        context_window_k: int,
        max_tokens: int,
        safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    ) -> None:
        ensure_configured_request_capacity_available(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            safety_margin_tokens=safety_margin_tokens,
        )

    @classmethod
    def prepare_messages_for_model_request(
        cls,
        messages: list[InternalMessage],
        context_window_k: int,
        max_tokens: int,
        safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    ) -> list[InternalMessage]:
        """
        在每次模型请求前只校验静态配置容量，不根据本地 Token 估算拒绝请求。

        历史压缩由上下文总结机制负责；工具结果在当前工具轮结束时一次性定稿并持久化。
        实际上下文是否超限由 Provider 响应裁决；这里不得重新截断工具结果、替换工具链、
        滑动删除历史消息或根据估算值触发压缩，以保持请求前缀稳定。
        """
        request_messages = [msg.model_copy(deep=True) for msg in messages]
        ensure_configured_request_capacity_available(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            safety_margin_tokens=safety_margin_tokens,
        )

        return request_messages

    @classmethod
    def audit_tool_chain(
        cls,
        messages: list[InternalMessage],
        uid: str,
        session_id: str,
        emit_logs: bool = True,
        known_tool_call_ids: set[str] | None = None,
    ) -> list[InternalMessage]:
        audited_msgs = []
        consumed_msg_ids = set()
        effective_known_tool_call_ids = set(known_tool_call_ids or ())
        for message in messages:
            for tool_call in message.tool_calls or []:
                effective_known_tool_call_ids.add(tool_call.id)

        i = 0
        while i < len(messages):
            msg = messages[i]

            if id(msg) in consumed_msg_ids:
                i += 1
                continue

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                required_ids = list(dict.fromkeys(tc.id for tc in msg.tool_calls))
                matched_tools = []
                found_tool_call_ids = set()

                for j in range(i + 1, len(messages)):
                    target = messages[j]
                    if target.role == MessageRole.TOOL and target.tool_call_id in required_ids and target.tool_call_id not in found_tool_call_ids:
                        matched_tools.append(target)
                        found_tool_call_ids.add(target.tool_call_id)

                previous_non_system = next((item for item in reversed(audited_msgs) if item.role != MessageRole.SYSTEM), None)
                if previous_non_system is None or previous_non_system.role not in {MessageRole.USER, MessageRole.TOOL}:
                    for matched_tool in matched_tools:
                        consumed_msg_ids.add(id(matched_tool))
                    i += 1
                    continue

                audited_msgs.append(msg)
                for matched_tool in matched_tools:
                    audited_msgs.append(matched_tool)
                    consumed_msg_ids.add(id(matched_tool))

                if len(found_tool_call_ids) < len(required_ids):
                    if emit_logs:
                        logger.bind(uid=uid, session_id=session_id).warning(
                            t(
                                "LOG_CONTEXT_TOOL_CHAIN_INCOMPLETE",
                                required_ids=required_ids,
                                found_ids=list(found_tool_call_ids),
                            )
                        )
                    for tool_call_id in required_ids:
                        if tool_call_id not in found_tool_call_ids:
                            virtual_tool_msg = InternalMessage(
                                role=MessageRole.TOOL,
                                tool_call_id=tool_call_id,
                                content=json.dumps({"error": PROMPT_TOOL_INTERRUPTED}),
                            )
                            audited_msgs.append(virtual_tool_msg)
                i += 1
            elif msg.role == MessageRole.TOOL:
                if _is_background_tool_result_message(msg):
                    i += 1
                    continue
                if emit_logs and msg.tool_call_id not in effective_known_tool_call_ids:
                    logger.bind(uid=uid, session_id=session_id).warning(t("LOG_CONTEXT_ORPHAN_TOOL_RESULT", tool_call_id=msg.tool_call_id))
                i += 1
            else:
                audited_msgs.append(msg)
                i += 1

        return audited_msgs
