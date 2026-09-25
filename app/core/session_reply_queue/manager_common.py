import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_LLM_UNEXPECTED_ERROR,
)
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.session_source import default_show_tool_calls_for_source
from app.models.message import Message
from app.models.session_reply_work_item import (
    SessionReplyWorkItem,
)

__all__ = [
    "WORK_RESULT_POLL_INTERVAL_SECONDS",
    "get_work_request_ids",
    "merge_work_request_ids",
    "is_submission_queued",
    "get_tool_call_visibility",
    "build_input_accepted_event",
    "build_input_queued_event",
    "build_foreground_message_dedupe_key",
    "build_session_reply_work_identity",
    "build_session_reply_work_event_id",
    "build_identified_work_response",
]

logger = get_logger(__name__)

WORK_RESULT_POLL_INTERVAL_SECONDS = 0.2


def get_work_request_ids(work: SessionReplyWorkItem) -> list[str]:
    state = getattr(work, "execution_state", None)
    request_ids = state.get("request_ids") if isinstance(state, dict) else None
    if not isinstance(request_ids, list):
        return []
    unique_request_ids: list[str] = []
    seen: set[str] = set()
    for request_id in request_ids:
        if isinstance(request_id, str) and request_id and request_id not in seen:
            seen.add(request_id)
            unique_request_ids.append(request_id)
    return unique_request_ids


def merge_work_request_ids(
    *works: SessionReplyWorkItem,
    request_id: str | None = None,
) -> list[str]:
    merged_request_ids: list[str] = []
    seen: set[str] = set()
    for work in works:
        for item_request_id in get_work_request_ids(work):
            if item_request_id not in seen:
                seen.add(item_request_id)
                merged_request_ids.append(item_request_id)
    if isinstance(request_id, str) and request_id and request_id not in seen:
        merged_request_ids.append(request_id)
    return merged_request_ids


def is_submission_queued(submission_status: str) -> bool:
    return submission_status == "queued" or submission_status.endswith("_and_queued")


def build_input_accepted_event(
    session_id: str,
    request_id: str,
    work_id: int | None,
    submission_status: str,
) -> dict[str, Any]:
    return {
        "type": "input_accepted",
        "session_id": session_id,
        "request_id": request_id,
        "work_id": work_id,
        "submission_status": submission_status,
    }


def get_tool_call_visibility(session: Any | None, source: str) -> tuple[bool, bool]:
    show_tool_calls = session.show_tool_calls if session is not None else default_show_tool_calls_for_source(source)
    return show_tool_calls, show_tool_calls


def build_input_queued_event(
    session_id: str,
    request_id: str,
    work_id: int | None,
    submission_status: str,
) -> dict[str, Any]:
    return {
        "type": "input_queued",
        "session_id": session_id,
        "request_id": request_id,
        "work_id": work_id,
        "submission_status": submission_status,
    }


def _serialize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if hasattr(content, "model_dump"):
        content = content.model_dump(mode="json")
    return json.dumps(content, ensure_ascii=False)


def build_foreground_message_dedupe_key(session_id: str, message_id: int) -> str:
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"foreground-message:{session_digest}:{message_id}"


def build_session_reply_work_identity(work: SessionReplyWorkItem) -> str:
    created_at = work.created_at.replace(tzinfo=None).isoformat(timespec="microseconds") if isinstance(work.created_at, datetime) else str(work.created_at)
    payload = json.dumps(
        {
            "dedupe_key": work.dedupe_key,
            "created_at": created_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_session_reply_work_event_id(work: SessionReplyWorkItem, *, error: bool = False) -> str:
    return f"session-reply-work:{build_session_reply_work_identity(work)}:{'error' if error else 'event'}"


def build_identified_work_response(
    work: SessionReplyWorkItem,
    response: dict[str, Any],
    *,
    message_id: int | None = None,
) -> dict[str, Any]:
    identified_response = {**response, "work_id": work.id}
    result_message_id = getattr(work, "result_message_id", None) if message_id is None else message_id
    if isinstance(result_message_id, int) and not isinstance(result_message_id, bool) and result_message_id > 0:
        identified_response["message_id"] = result_message_id
    return identified_response


async def _get_work_failure_content(db: AsyncSession, work: SessionReplyWorkItem) -> str:
    message = await db.get(Message, work.result_message_id) if work.result_message_id else None
    return message.content if message and message.content else t(ERR_LLM_UNEXPECTED_ERROR)


async def _raise_work_failure(db: AsyncSession, work: SessionReplyWorkItem) -> None:
    error_content = await _get_work_failure_content(db, work)
    raise BaseBusinessException(
        message=error_content,
        default_message=error_content,
        data={
            "work_id": work.id,
            "event_id": build_session_reply_work_event_id(work, error=True),
        },
    )
