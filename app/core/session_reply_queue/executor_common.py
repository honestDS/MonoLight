import time
from datetime import datetime
from typing import Any

from app.core.crud.session.message import message_crud
from app.core.session_reply_queue.manager import (
    build_session_reply_work_event_id,
    build_session_reply_work_identity,
    get_work_request_ids,
)
from app.core.utils.assistant_files import parse_assistant_files_content
from app.models.message import Message, MessageRole
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkType

__all__ = [
    "SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX",
]

SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX = "session-reply-work"


def _work_identity(work: SessionReplyWorkItem) -> str:
    return build_session_reply_work_identity(work)


def _result_message_dedupe_key(work: SessionReplyWorkItem) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{_work_identity(work)}:result"


def _error_message_dedupe_key(work: SessionReplyWorkItem) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{_work_identity(work)}:error"


def _message_belongs_to_work(message: Message, work: SessionReplyWorkItem) -> bool:
    if message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id:
        return False
    if not isinstance(message.created_at, datetime) or not isinstance(work.created_at, datetime):
        return False
    return message.created_at.replace(tzinfo=None) >= work.created_at.replace(tzinfo=None)


async def _get_persisted_result(db, work: SessionReplyWorkItem) -> Message | None:
    persisted_result = await db.get(Message, work.result_message_id) if work.result_message_id else None
    if persisted_result is not None and _message_belongs_to_work(persisted_result, work):
        return persisted_result

    persisted_result = await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
    if persisted_result is not None and _message_belongs_to_work(persisted_result, work):
        return persisted_result

    return None


def _response_from_persisted_message(work: SessionReplyWorkItem, message: Message) -> dict[str, Any]:
    content = parse_assistant_files_content(message.content)
    if work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
        return {
            "choices": [
                {
                    "message": {
                        "role": MessageRole.ASSISTANT,
                        "content": content,
                    },
                    "finish_reason": True,
                    "created_at": time.time(),
                }
            ],
            "history": [],
            "files": None,
        }
    return {
        "content": content,
        "history": [],
        "files": [],
    }


def _response_content(response: dict[str, Any]) -> str:
    if isinstance(response.get("content"), str):
        return response["content"]
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _event_for_work(work: SessionReplyWorkItem, response: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    source = {
        SessionReplyWorkType.FOREGROUND_REPLY: "foreground",
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION: "confirmed_tool_execution",
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY: "background_task",
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY: "scheduled_task",
    }[work.work_type]
    content = parse_assistant_files_content(_response_content(response))
    event = {
        "event_id": build_session_reply_work_event_id(work, error=error),
        "type": "proactive_reply_error" if error else "proactive_reply",
        "source": source,
        "session_id": work.session_id,
        "work_id": work.id,
        "content": content,
        "history": response.get("history", []),
        "files": response.get("files", []),
        "request_ids": get_work_request_ids(work),
    }
    if response.get("llm_request_metadata") is not None:
        event["llm_request_metadata"] = response["llm_request_metadata"]
    if response.get("message_id") is not None:
        event["message_id"] = response["message_id"]
    if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
        event["task_id"] = int(work.source_id)
        event["background_task_id"] = int(work.source_id)
    elif work.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY:
        event["trigger_message_id"] = int(work.source_id)
    if isinstance(work.execution_state, dict) and work.execution_state.get("stream_requested"):
        event["_stream_requested"] = True
    return event


def _metadata_with_work_order(
    work: SessionReplyWorkItem,
    metadata: Any,
    event_sequence_no: int | None = None,
) -> Any:
    if not isinstance(metadata, dict):
        return metadata
    ordered_metadata = {
        **metadata,
        "work_id": work.id,
        "work_sequence_no": work.sequence_no,
    }
    if event_sequence_no is not None:
        ordered_metadata["event_sequence_no"] = event_sequence_no
    return ordered_metadata
