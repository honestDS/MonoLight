import hashlib
import json
from typing import Any

from app.core.crud.message_platform_outbox import message_platform_outbox_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.session_notifier import session_notifier
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


def normalize_outbox_event(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(event, ensure_ascii=False, default=str))


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


async def send_session_event(uid: str, session_id: str, event: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        session = await session_crud.get_by_session_id(db, session_id)
        source = session.reply_target_source if session and session.reply_target_source else session.source if session else "http"

        if source in {"http", "ws"}:
            await session_notifier.notify(uid, session_id, event)
            logger.bind(uid=uid, session_id=session_id, event_type=event.get("type"), session_source=source).debug("WebSocket session event notified")
            return

        normalized_event = normalize_outbox_event(event)
        outbox_item, created = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=build_outbox_dedupe_key(uid, session_id, source, normalized_event),
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
