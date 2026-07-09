from typing import Any

from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.manager import message_platform_polling_manager
from app.core.session_notifier import session_notifier
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


async def send_session_event(uid: str, session_id: str, event: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        session = await session_crud.get_by_session_id(db, session_id)
    source = session.reply_target_source if session and session.reply_target_source else session.source if session else "http"

    if source in {"http", "ws"}:
        await session_notifier.notify(uid, session_id, event)
        logger.bind(uid=uid, session_id=session_id, event_type=event.get("type"), session_source=source).debug("WebSocket session event notified")
        return

    platform_sent = await message_platform_polling_manager.send_session_event(uid, session_id, source, event)
    if platform_sent:
        logger.bind(uid=uid, session_id=session_id, event_type=event.get("type"), session_source=source).info(t("LOG_MESSAGE_PLATFORM_SESSION_EVENT_SENT"))
        return
    logger.bind(uid=uid, session_id=session_id, event_type=event.get("type"), session_source=source).warning(t("LOG_MESSAGE_PLATFORM_SESSION_EVENT_DROPPED"))
