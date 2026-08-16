import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.core.constants import ERR_WEIXIN_OPENCLAW_OUTBOUND_TEXT_FALLBACK_TOO_LONG
from app.core.crud.message import message_crud
from app.core.crud.profile import profile_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.assistant_files import parse_assistant_files_content
from app.models.message import InternalMessage, MessageRole, MessageType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OutboundTextPolicy:
    utf8_byte_limit: int
    max_refinement_attempts: int
    additional_system_prompt: str
    refinement_prompt: str
    refinement_failed_message_key: str
    max_text_parts: int = 1


def build_outbound_text_policy_registry(*entries: tuple[str, OutboundTextPolicy]) -> Mapping[str, OutboundTextPolicy]:
    return MappingProxyType(dict(entries))


def split_outbound_text_by_newline(text: str, *, utf8_byte_limit: int) -> tuple[str, str] | None:
    total_utf8_bytes = len(text.encode("utf-8"))
    if total_utf8_bytes <= utf8_byte_limit:
        return None

    prefix_utf8_bytes = 0
    best_boundary: tuple[int, int] | None = None
    best_byte_difference: int | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if character not in {"\n", "\r"}:
            prefix_utf8_bytes += len(character.encode("utf-8"))
            index += 1
            continue

        delimiter_end = index + 2 if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n" else index + 1
        delimiter_utf8_bytes = delimiter_end - index
        suffix_utf8_bytes = total_utf8_bytes - prefix_utf8_bytes - delimiter_utf8_bytes
        if index > 0 and delimiter_end < len(text) and prefix_utf8_bytes <= utf8_byte_limit and suffix_utf8_bytes <= utf8_byte_limit:
            byte_difference = abs(prefix_utf8_bytes - suffix_utf8_bytes)
            if best_byte_difference is None or byte_difference < best_byte_difference:
                best_boundary = (index, delimiter_end)
                best_byte_difference = byte_difference
        prefix_utf8_bytes += delimiter_utf8_bytes
        index = delimiter_end

    if best_boundary is None:
        return None
    split_index, suffix_start = best_boundary
    return text[:split_index], text[suffix_start:]


def _outbound_text_refinement_dedupe_key(
    *,
    uid: str,
    session_id: str,
    source: str,
    event: dict[str, Any],
    refinement_attempt: int,
    purpose: str,
) -> str:
    event_identity: Any = event["event_id"] if event.get("event_id") is not None else event
    payload = json.dumps(
        {
            "uid": uid,
            "session_id": session_id,
            "source": source,
            "event_identity": event_identity,
            "refinement_attempt": refinement_attempt,
            "purpose": purpose,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _save_outbound_text_refinement_prompt(
    *,
    db: Any,
    session_id: str,
    uid: str,
    profile_id: int,
    refinement_prompt: str,
    dedupe_key: str,
) -> InternalMessage:
    from app.core.utils.dispatcher.save_message import save_message

    return await save_message(
        db,
        session_id,
        uid,
        MessageRole.USER,
        MessageType.TEXT,
        InternalMessage(role=MessageRole.USER, content=refinement_prompt),
        profile_id,
        is_processed=True,
        dedupe_key=dedupe_key,
    )


async def _save_outbound_text_refinement_assistant_message(
    *,
    db: Any,
    session_id: str,
    uid: str,
    profile_id: int,
    ai_msg: InternalMessage,
    dedupe_key: str,
) -> InternalMessage:
    from app.core.utils.dispatcher.save_assistant_message import save_assistant_message

    return await save_assistant_message(
        db,
        session_id=session_id,
        uid=uid,
        profile_id=profile_id,
        ai_msg=ai_msg,
        dedupe_key=dedupe_key,
    )


async def process_outbound_text_event(
    uid: str,
    session_id: str,
    source: str,
    event: dict[str, Any],
    policy: OutboundTextPolicy,
) -> dict[str, Any]:
    processed_event = deepcopy(event)
    content = processed_event.get("content")
    if not isinstance(content, str) or not content or len(content.encode("utf-8")) <= policy.utf8_byte_limit:
        return processed_event
    if policy.max_text_parts >= 2 and split_outbound_text_by_newline(content, utf8_byte_limit=policy.utf8_byte_limit) is not None:
        return processed_event

    logger.bind(
        uid=uid,
        session_id=session_id,
        session_source=source,
        utf8_bytes=len(content.encode("utf-8")),
        utf8_byte_limit=policy.utf8_byte_limit,
    ).warning(t("LOG_MESSAGE_PLATFORM_OUTBOUND_TEXT_REFINEMENT_STARTED"))

    candidate = content
    for refinement_attempt in range(1, policy.max_refinement_attempts + 1):
        refined_text, context_available = await _generate_refined_outbound_text(
            uid=uid,
            session_id=session_id,
            source=source,
            event=event,
            candidate=candidate,
            policy=policy,
            refinement_attempt=refinement_attempt,
        )
        if not context_available:
            break
        if refined_text and len(refined_text.encode("utf-8")) < len(candidate.encode("utf-8")):
            candidate = refined_text
            if len(candidate.encode("utf-8")) <= policy.utf8_byte_limit:
                processed_event["content"] = candidate
                return processed_event
            if policy.max_text_parts >= 2 and split_outbound_text_by_newline(candidate, utf8_byte_limit=policy.utf8_byte_limit) is not None:
                processed_event["content"] = candidate
                return processed_event

        logger.bind(
            uid=uid,
            session_id=session_id,
            session_source=source,
            refinement_attempt=refinement_attempt,
        ).debug("Message platform outbound text refinement attempt did not reach the byte limit")

    fallback_content = t(policy.refinement_failed_message_key)
    if len(fallback_content.encode("utf-8")) > policy.utf8_byte_limit:
        raise ValueError(t(ERR_WEIXIN_OPENCLAW_OUTBOUND_TEXT_FALLBACK_TOO_LONG))
    processed_event["content"] = await _save_outbound_text_refinement_fallback(
        uid=uid,
        session_id=session_id,
        source=source,
        event=event,
        fallback_content=fallback_content,
        refinement_attempt=policy.max_refinement_attempts + 1,
    )
    logger.bind(
        uid=uid,
        session_id=session_id,
        session_source=source,
        utf8_byte_limit=policy.utf8_byte_limit,
    ).warning(t("LOG_MESSAGE_PLATFORM_OUTBOUND_TEXT_REFINEMENT_FAILED"))
    return processed_event


async def _generate_refined_outbound_text(
    *,
    uid: str,
    session_id: str,
    source: str,
    event: dict[str, Any],
    candidate: str,
    policy: OutboundTextPolicy,
    refinement_attempt: int,
) -> tuple[str | None, bool]:
    try:
        async with AsyncSessionLocal() as db:
            session = await session_crud.get_by_session_id(db, session_id)
            if session is None or session.uid != uid:
                _log_refinement_context_missing(uid=uid, session_id=session_id, source=source)
                return None, False
            if session.profile_id is None:
                _log_refinement_context_missing(uid=uid, session_id=session_id, source=source)
                return None, False
            profile = await profile_crud.get_with_relations(db, session.profile_id)
            if profile is None or profile.uid != uid:
                _log_refinement_context_missing(uid=uid, session_id=session_id, source=source)
                return None, False

            await _save_outbound_text_refinement_prompt(
                db=db,
                session_id=session_id,
                uid=uid,
                profile_id=profile.id,
                refinement_prompt=policy.refinement_prompt,
                dedupe_key=_outbound_text_refinement_dedupe_key(
                    uid=uid,
                    session_id=session_id,
                    source=source,
                    event=event,
                    refinement_attempt=refinement_attempt,
                    purpose="refinement_prompt",
                ),
            )

            assistant_dedupe_key = _outbound_text_refinement_dedupe_key(
                uid=uid,
                session_id=session_id,
                source=source,
                event=event,
                refinement_attempt=refinement_attempt,
                purpose="refinement_result",
            )
            persisted_assistant_message = await message_crud.get_by_dedupe_key(db, assistant_dedupe_key)
            if persisted_assistant_message is not None:
                refined_text = parse_assistant_files_content(persisted_assistant_message.content)
                return refined_text, True

            from app.core.dispatcher import ChatDispatcher

            ai_msg, _turn_messages, _files = await ChatDispatcher._generate_reply_from_history(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                call_context="message_platform_outbound_text_refinement",
                allow_tools=False,
                submission_context=[InternalMessage(role=MessageRole.ASSISTANT, content=candidate)],
                extra_messages=[InternalMessage(role=MessageRole.USER, content=policy.refinement_prompt)],
                additional_system_prompt=policy.additional_system_prompt,
                persist_response=False,
                reply_source=source,
            )
            saved_msg = await _save_outbound_text_refinement_assistant_message(
                db=db,
                session_id=session_id,
                uid=uid,
                profile_id=profile.id,
                ai_msg=ai_msg,
                dedupe_key=assistant_dedupe_key,
            )
    except Exception:
        logger.bind(
            uid=uid,
            session_id=session_id,
            session_source=source,
        ).warning(t("LOG_MESSAGE_PLATFORM_OUTBOUND_TEXT_REFINEMENT_ATTEMPT_FAILED"), exc_info=True)
        return None, True

    refined_text = parse_assistant_files_content(saved_msg.content)
    return refined_text, True


async def _save_outbound_text_refinement_fallback(
    *,
    uid: str,
    session_id: str,
    source: str,
    event: dict[str, Any],
    fallback_content: str,
    refinement_attempt: int,
) -> str:
    try:
        async with AsyncSessionLocal() as db:
            session = await session_crud.get_by_session_id(db, session_id)
            if session is None or session.uid != uid or session.profile_id is None:
                _log_refinement_context_missing(uid=uid, session_id=session_id, source=source)
                return fallback_content
            profile = await profile_crud.get_with_relations(db, session.profile_id)
            if profile is None or profile.uid != uid:
                _log_refinement_context_missing(uid=uid, session_id=session_id, source=source)
                return fallback_content
            saved_msg = await _save_outbound_text_refinement_assistant_message(
                db=db,
                session_id=session_id,
                uid=uid,
                profile_id=profile.id,
                ai_msg=InternalMessage(role=MessageRole.ASSISTANT, content=fallback_content),
                dedupe_key=_outbound_text_refinement_dedupe_key(
                    uid=uid,
                    session_id=session_id,
                    source=source,
                    event=event,
                    refinement_attempt=refinement_attempt,
                    purpose="refinement_fallback",
                ),
            )
            return saved_msg.content if isinstance(saved_msg.content, str) else fallback_content
    except Exception:
        logger.bind(
            uid=uid,
            session_id=session_id,
            session_source=source,
        ).warning(t("LOG_MESSAGE_PLATFORM_OUTBOUND_TEXT_REFINEMENT_ATTEMPT_FAILED"), exc_info=True)
        return fallback_content


def _log_refinement_context_missing(*, uid: str, session_id: str, source: str) -> None:
    logger.bind(
        uid=uid,
        session_id=session_id,
        session_source=source,
    ).warning(t("LOG_MESSAGE_PLATFORM_OUTBOUND_TEXT_REFINEMENT_CONTEXT_MISSING"))
