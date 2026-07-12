import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.prompts import CONTEXT_SUMMARY_PROMPT, CONTEXT_SUMMARY_WRAPPER
from app.core.utils.dispatcher.helpers import format_exception_message, resolve_chat_params
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


@dataclass(frozen=True)
class ContextSummaryState:
    content: str | None
    message_id: int | None

    def as_message(self) -> InternalMessage | None:
        if not self.content:
            return None
        return InternalMessage(
            role=MessageRole.SYSTEM,
            content=CONTEXT_SUMMARY_WRAPPER.format(content=self.content),
        )


def _serialize_message(message: InternalMessage) -> str:
    payload = message.model_dump(
        mode="json",
        exclude={"id", "attachments", "created_at"},
        exclude_none=True,
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _select_summary_segment(messages: list[InternalMessage], target_tokens: int) -> list[InternalMessage]:
    if len(messages) < 2:
        return []

    running_tokens = 0
    preferred_end = 0
    safe_ends: list[int] = []
    for index, message in enumerate(messages):
        running_tokens += estimate_tokens(_serialize_message(message))
        next_message = messages[index + 1] if index + 1 < len(messages) else None
        if message.id is not None and next_message is not None and next_message.role == MessageRole.USER:
            safe_ends.append(index + 1)
        if running_tokens <= target_tokens:
            preferred_end = index + 1

    eligible_ends = [end for end in safe_ends if end <= preferred_end]
    if eligible_ends:
        return messages[: eligible_ends[-1]]
    return []


async def get_context_summary_state(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
) -> ContextSummaryState:
    session = await session_crud.get_by_session_id(db, session_id)
    if session is None or session.uid != uid:
        return ContextSummaryState(content=None, message_id=None)
    return ContextSummaryState(
        content=session.context_summary,
        message_id=session.context_summary_message_id,
    )


async def ensure_context_summary(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    before_id: int | None,
    current_message: str,
    context_window_k: int,
    max_tokens: int,
    reserved_tokens: int,
    tools: list[dict] | None = None,
    safety_margin_tokens: int = 256,
) -> ContextSummaryState:
    state = await get_context_summary_state(db, session_id=session_id, uid=uid)
    raw_history = await message_crud.get_history(
        db,
        session_id=session_id,
        uid=uid,
        limit=5000,
        before_id=before_id,
        after_id=state.message_id,
    )
    messages = list(reversed(parse_db_messages_to_internal(raw_history)))
    summary_tokens = estimate_tokens(CONTEXT_SUMMARY_WRAPPER.format(content=state.content)) if state.content else 0
    history_tokens = sum(estimate_tokens(_serialize_message(message)) for message in messages)
    tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    input_budget = max(
        1,
        context_window_k * 1024 - max(max_tokens, 0) - tools_tokens - max(safety_margin_tokens, 0),
    )
    required_tokens = reserved_tokens + summary_tokens + history_tokens + estimate_tokens(current_message)
    if required_tokens < input_budget:
        return state

    available_history_tokens = max(1, input_budget - reserved_tokens - summary_tokens)
    segment = _select_summary_segment(messages, max(1, available_history_tokens // 2))
    if not segment or segment[-1].id is None:
        return state

    conversation = "\n".join(_serialize_message(message) for message in segment)
    prompt = CONTEXT_SUMMARY_PROMPT.format(
        existing_summary=state.content or "(none)",
        conversation=conversation,
    )
    chat_channel = cfg.channel.chat_channel
    cursor_key = f"{profile.id}:CHAT:CONTEXT_SUMMARY"
    excluded_priorities: set[int] = set()
    selection = await select_channel(
        db,
        chat_channel,
        "CHAT",
        call_context="context_summary",
        cursor_key=cursor_key,
    )

    while selection:
        channel, model_entry, rule = selection
        chat_params = resolve_chat_params(model_entry, chat_channel)
        summary_max_tokens = min(1024, max(256, chat_params["context_window_k"] * 64))
        summary_input_tokens = estimate_tokens(prompt)
        summary_input_budget = max(1, chat_params["context_window_k"] * 1024 - summary_max_tokens - safety_margin_tokens)
        if summary_input_tokens > summary_input_budget:
            excluded_priorities.add(rule.priority)
            selection = await select_channel(
                db,
                chat_channel,
                "CHAT",
                call_context="context_summary_retry",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )
            continue

        try:
            response = await LLMClient.generate(
                api_key=channel.get_decrypted_api_key(),
                base_url=channel.base_url,
                model_id=model_entry["model_id"],
                messages=[InternalMessage(role=MessageRole.USER, content=prompt)],
                temperature=0.2,
                max_tokens=summary_max_tokens,
                protocol=getattr(channel, "protocol", "openai"),
                timeout=chat_params["chat_timeout"],
            )
            summary = (response.message.content or "").strip()
            if not summary:
                return state

            updated = await session_crud.update_context_summary(
                db,
                session_id=session_id,
                uid=uid,
                expected_message_id=state.message_id,
                summary=summary,
                message_id=segment[-1].id,
            )
            if updated:
                return ContextSummaryState(content=summary, message_id=segment[-1].id)
            return await get_context_summary_state(db, session_id=session_id, uid=uid)
        except Exception as exc:
            excluded_priorities.add(rule.priority)
            logger.bind(
                uid=uid,
                session_id=session_id,
                **channel_log_extra(channel, model_entry),
            ).warning(
                t(
                    "LOG_CONTEXT_SUMMARY_CHANNEL_FAILED",
                    default="Context summary channel failed: {error}",
                    error=format_exception_message(exc),
                )
            )
            selection = await select_channel(
                db,
                chat_channel,
                "CHAT",
                call_context="context_summary_retry",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )

    return state
