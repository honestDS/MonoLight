import hashlib
import json
from functools import lru_cache
from typing import Any

from app.core.constants import MSG_MESSAGE_PLATFORM_TOOL_USED
from app.core.crud.message_platform import message_platform_crud
from app.core.crud.message_platform_outbox import message_platform_outbox_crud
from app.core.crud.session import session_crud
from app.core.i18n import DEFAULT_LOCALE, message_platform_t, t
from app.core.log import get_logger
from app.core.message_platforms.outbound_text import (
    build_outbound_text_policy_registry,
    process_outbound_text_event,
)
from app.core.message_platforms.tool_output import combine_proactive_reply_tool_output
from app.core.session_notifier import session_notifier
from app.core.session_source import is_web_session_source
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_outbound_text_policy_registry():
    from app.adapters.weixin_openclaw.outbound import WEIXIN_OPENCLAW_OUTBOUND_TEXT_POLICY

    return build_outbound_text_policy_registry(
        ("weixin-openclaw", WEIXIN_OPENCLAW_OUTBOUND_TEXT_POLICY),
    )


def normalize_outbox_event(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(event, ensure_ascii=False, default=str))


def _has_proactive_reply_tool_history(event: dict[str, Any]) -> bool:
    history = event.get("history")
    if not isinstance(history, list):
        return False
    return any(item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list) and any(isinstance(tool_call, dict) for tool_call in item["tool_calls"]) for item in history if isinstance(item, dict))


def _has_deliverable_reply_payload(event: dict[str, Any]) -> bool:
    content = event.get("content")
    if isinstance(content, str):
        if content.strip():
            return True
    elif content:
        return True
    return isinstance(event.get("files"), list) and bool(event["files"])


async def _get_message_platform_language(uid: str, session_id: str, source: str) -> str:
    async with AsyncSessionLocal() as db:
        platform = await message_platform_crud.get_platform_for_session(
            db,
            uid=uid,
            session_id=session_id,
            source=source,
        )
    return str(getattr(platform, "language", None) or DEFAULT_LOCALE)


def _resolve_event_identity(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    event_source = str(event.get("source") or "")
    if event.get("event_id") is not None:
        return {"event_id": event["event_id"], "type": event_type}
    if event_source == "background_task" and event.get("background_task_id") is not None:
        return {
            "background_task_id": event["background_task_id"],
            "type": event_type,
        }
    if event_source == "scheduled_task" and event.get("trigger_message_id") is not None:
        return {
            "trigger_message_id": event["trigger_message_id"],
            "type": event_type,
        }
    return {"event": event}


def build_outbox_dedupe_key(uid: str, session_id: str, source: str, event: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "uid": uid,
            "session_id": session_id,
            "source": source,
            "identity": _resolve_event_identity(event),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _send_session_event_for_session(uid: str, session_id: str, event: dict[str, Any], session: Any) -> None:
    source = session.source if session and session.source else "http"
    normalized_event = normalize_outbox_event(event)
    stream_requested = normalized_event.pop("_stream_requested", False) is True
    if normalized_event.get("type") == "proactive_reply":
        if not is_web_session_source(source):
            if stream_requested:
                normalized_event.pop("history", None)
            elif session is not None and session.show_tool_calls and _has_proactive_reply_tool_history(normalized_event):
                language = await _get_message_platform_language(uid, session_id, source)
                normalized_event = combine_proactive_reply_tool_output(normalized_event, language=language)
    policy = get_outbound_text_policy_registry().get(source)
    if policy is not None:
        normalized_event = await process_outbound_text_event(uid, session_id, source, normalized_event, policy)
    if not is_web_session_source(source) and normalized_event.get("type") == "proactive_reply" and not _has_deliverable_reply_payload(normalized_event):
        return
    dedupe_key = build_outbox_dedupe_key(uid, session_id, source, normalized_event)
    if is_web_session_source(source):
        created = await session_notifier.notify(
            uid,
            session_id,
            normalized_event,
            dedupe_key=dedupe_key,
        )
        logger.bind(
            uid=uid,
            session_id=session_id,
            event_type=event.get("type"),
            session_source=source,
            session_event_created=created,
        ).debug("WebSocket session event notified")
        return

    async with AsyncSessionLocal() as db:
        outbox_item, created = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=dedupe_key,
            uid=uid,
            session_id=session_id,
            source=source,
            event=normalized_event,
        )

    logger.bind(
        uid=uid,
        session_id=session_id,
        event_type=event.get("type"),
        session_source=source,
        outbox_id=outbox_item.id,
        outbox_created=created,
    ).info(t("LOG_MESSAGE_PLATFORM_SESSION_EVENT_QUEUED"))


async def send_session_event(uid: str, session_id: str, event: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        session = await session_crud.get_by_session_id(db, session_id)
    await _send_session_event_for_session(uid, session_id, event, session)


async def send_session_stream_event(uid: str, session_id: str, event: dict[str, Any]) -> None:
    if event.get("type") != "tool_start":
        return

    async with AsyncSessionLocal() as db:
        session = await session_crud.get_by_session_id(db, session_id)
    source = session.source if session and session.source else "http"
    if session is None or is_web_session_source(source) or not session.show_tool_calls:
        return

    work_id = event.get("work_id")
    event_sequence_no = event.get("event_sequence_no")
    if work_id is None or event_sequence_no is None:
        return

    tool_names = event.get("tool_names")
    if not isinstance(tool_names, list):
        tool_names = [event.get("name")]
    tool_names = [name.strip() for name in tool_names if isinstance(name, str) and name.strip()]
    if not tool_names:
        return
    language = await _get_message_platform_language(uid, session_id, source)
    tool_lines = [message_platform_t(MSG_MESSAGE_PLATFORM_TOOL_USED, language=language, name=name) for name in tool_names]

    content = event.get("content")
    parts = [content.strip()] if isinstance(content, str) and content.strip() else []
    parts.extend(tool_lines)
    # Weixin Markdown requires blank lines between paragraphs for visible line breaks.
    await _send_session_event_for_session(
        uid,
        session_id,
        {
            "event_id": f"stream_tool_call:{work_id}:{event_sequence_no}",
            "type": "proactive_reply",
            "source": "stream_tool_call",
            "session_id": session_id,
            "work_id": work_id,
            "content": "\n\n".join(parts),
        },
        session,
    )
