from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS
from app.core.context import ContextManager
from app.core.prompts import CONTEXT_SUMMARY_PROMPT, CONTEXT_SUMMARY_WRAPPER
from app.core.utils.context_messages import is_context_summary_message, message_token_text
from app.core.utils.context_summary.common import (
    ContextSummaryWorkValidityChecker,
    calc_token_usage,
    join_messages,
    serialize_message,
)
from app.core.utils.context_summary.service import generate_summary_text, get_context_summary_state
from app.core.utils.context_summary.user_message_block import append_covered_user_message, split_covered_user_message
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig


def serialize_frozen_context_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    serialized: list[dict[str, Any]] = []
    for item in messages:
        message = item.model_copy(deep=True) if isinstance(item, InternalMessage) else InternalMessage.model_validate(item)
        if message.role == MessageRole.SYSTEM:
            continue
        serialized.append(message.model_dump(mode="json", exclude_none=True))
    return serialized


def load_frozen_context_messages(messages: Any) -> list[InternalMessage] | None:
    if not isinstance(messages, list):
        return None
    return [InternalMessage.model_validate(item) for item in serialize_frozen_context_messages(messages)]


def _select_frozen_compaction_segment(
    messages: list[InternalMessage],
    *,
    target_tokens: int,
    model_id: str | None = None,
    protocol: str | None = None,
) -> list[InternalMessage]:
    if len(messages) < 2:
        return []

    safe_ends: list[tuple[int, int]] = []
    running_tokens = 0
    index = 0
    while index < len(messages):
        message = messages[index]
        running_tokens += estimate_tokens(
            serialize_message(message),
            model_id=model_id,
            protocol=protocol,
        )
        if message.role != MessageRole.ASSISTANT:
            index += 1
            continue

        tool_calls = message.tool_calls or []
        if not tool_calls:
            safe_ends.append((index + 1, running_tokens))
            index += 1
            continue

        required_ids = {tool_call.id for tool_call in tool_calls}
        found_ids: set[str] = set()
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == MessageRole.TOOL:
            tool_message = messages[cursor]
            running_tokens += estimate_tokens(
                serialize_message(tool_message),
                model_id=model_id,
                protocol=protocol,
            )
            if tool_message.tool_call_id in required_ids:
                found_ids.add(tool_message.tool_call_id)
            cursor += 1
        if found_ids == required_ids:
            safe_ends.append((cursor, running_tokens))
        index = cursor

    if not safe_ends:
        return []
    eligible = [item for item in safe_ends if item[1] <= max(target_tokens, 1)]
    end = eligible[-1][0] if eligible else safe_ends[0][0]
    return messages[:end]


def _covered_user_from_segment(messages: list[InternalMessage]) -> tuple[int | None, str | None]:
    covered_message_id = None
    covered_content = None
    for message in messages:
        if message.role != MessageRole.USER or not isinstance(message.content, str):
            continue
        if not is_context_summary_message(message):
            if isinstance(message.id, int) and not isinstance(message.id, bool) and message.id > 0:
                covered_message_id = message.id
                covered_content = message.content
            continue

        content = message.content
        body_start = content.find(">\n")
        body_end = content.rfind("\n</conversation_summary>")
        if body_start < 0 or body_end <= body_start:
            continue
        _model_summary, covered = split_covered_user_message(content[body_start + 2 : body_end])
        if covered is not None:
            covered_message_id = covered.message_id
            covered_content = covered.content
    return covered_message_id, covered_content


async def compact_frozen_context_messages(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    messages: list[InternalMessage],
    context_window_k: int,
    max_tokens: int,
    tools: list[dict] | None,
    model_id: str | None = None,
    protocol: str | None = None,
    safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    force: bool = False,
) -> list[InternalMessage]:
    system_messages = [message.model_copy(deep=True) for message in messages if message.role == MessageRole.SYSTEM]
    history_messages = [message.model_copy(deep=True) for message in messages if message.role != MessageRole.SYSTEM]
    if len(history_messages) < 2:
        return messages

    reserved_tokens = sum(
        estimate_tokens(
            message_token_text(message),
            model_id=model_id,
            protocol=protocol,
        )
        for message in system_messages
    )
    threshold_percent = cfg.other.context_summary_threshold_percent
    must_compact = force

    while True:
        usage = calc_token_usage(
            messages=history_messages,
            summary_content=None,
            current_message="",
            reserved_tokens=reserved_tokens,
            tools=tools,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            safety_margin_tokens=safety_margin_tokens,
            threshold_percent=threshold_percent,
            model_id=model_id,
            protocol=protocol,
        )
        if not must_compact and usage["required_tokens"] <= usage["compression_goal_tokens"]:
            break

        available_history_tokens = max(
            1,
            usage["input_budget"] - reserved_tokens - usage["tools_tokens"],
        )
        segment = _select_frozen_compaction_segment(
            history_messages,
            target_tokens=max(1, available_history_tokens // 2),
            model_id=model_id,
            protocol=protocol,
        )
        if not segment:
            break

        prompt = CONTEXT_SUMMARY_PROMPT.format(
            existing_summary="(none)",
            recent_dialogue="(none)",
            conversation=join_messages(segment),
        )
        summary = await generate_summary_text(
            db,
            profile=profile,
            cfg=cfg,
            prompt=prompt,
            safety_margin_tokens=safety_margin_tokens,
            uid=uid,
            session_id=session_id,
            work_validity_checker=work_validity_checker,
        )
        if not summary:
            break

        covered_message_id, covered_content = _covered_user_from_segment(segment)
        summary = append_covered_user_message(
            summary,
            message_id=covered_message_id,
            content=covered_content,
        )
        if not summary:
            break
        summary_through_id = max(
            (
                message.id
                for message in segment
                if isinstance(message.id, int) and not isinstance(message.id, bool) and message.id > 0
            ),
            default=covered_message_id or 0,
        )
        replacement = InternalMessage(
            role=MessageRole.USER,
            content=CONTEXT_SUMMARY_WRAPPER.format(
                through_message_id=summary_through_id,
                content=summary,
            ),
        )
        candidate_history = [replacement, *history_messages[len(segment) :]]
        candidate_tokens = sum(
            estimate_tokens(
                serialize_message(message),
                model_id=model_id,
                protocol=protocol,
            )
            for message in candidate_history
        )
        if candidate_tokens >= usage["history_tokens"]:
            break

        history_messages = candidate_history
        must_compact = False

    return [*system_messages, *history_messages]


async def capture_frozen_context_messages(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    before_id: int,
) -> list[dict[str, Any]]:
    if not isinstance(before_id, int) or isinstance(before_id, bool) or before_id <= 0:
        raise ValueError("before_id must be a positive integer")

    summary_state = await get_context_summary_state(db, session_id=session_id, uid=uid)
    use_summary = summary_state.message_id is None or summary_state.message_id < before_id
    after_id = summary_state.message_id if use_summary else None
    raw_history = await ContextManager._load_history_backward_by_id(
        db,
        session_id=session_id,
        uid=uid,
        before_id=before_id,
        after_id=after_id,
    )
    raw_history = [message for message in raw_history if message.is_processed]
    history = ContextManager.audit_tool_chain(
        list(reversed(parse_db_messages_to_internal(raw_history))),
        uid=uid,
        session_id=session_id,
        emit_logs=False,
    )
    messages: list[InternalMessage] = []
    if use_summary:
        summary_message = summary_state.as_message()
        if summary_message is not None:
            messages.append(summary_message)
    messages.extend(history)
    return serialize_frozen_context_messages(messages)
