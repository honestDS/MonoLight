import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.terminal_watch import terminal_watch_reply_crud
from app.core.prompts import TERMINAL_WATCH_STATE_INSTRUCTION_PROMPT
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole

__all__ = [
    "append_terminal_watch_snapshot",
    "load_current_terminal_watch_snapshot",
    "measure_terminal_watch_snapshot_tokens",
    "strip_terminal_watch_snapshots",
]


_SNAPSHOT_OPEN = "<current_terminal_watch_snapshot>"
_SNAPSHOT_CLOSE = "</current_terminal_watch_snapshot>"
_ACTIVE_STATUSES = {"pending", "running"}
_PENDING_RESULT_STATUSES = {"succeeded", "failed"}
_PENDING_REPLY_STATUSES = {"pending", "running"}


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _created_at_value(value: Any) -> str | None:
    value = _enum_value(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _snapshot_json(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    serialized = serialized.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{_SNAPSHOT_OPEN}{serialized}{_SNAPSHOT_CLOSE}"


def _latest_task(row: Mapping[str, Any]) -> dict[str, Any]:
    arguments = row.get("arguments")
    return {
        "task_id": row.get("id"),
        "tool_call_id": row.get("tool_call_id"),
        "arguments": dict(arguments),
        "status": _enum_value(row.get("status")),
        "event": _enum_value(row.get("event")),
        "reply_status": _enum_value(row.get("reply_status")),
        "created_at": _created_at_value(row.get("created_at")),
        "matched": _enum_value(row.get("event")) == "output_contains",
    }


def _new_terminal_state(terminal_session_id: str) -> dict[str, Any]:
    return {
        "terminal_session_id": terminal_session_id,
        "manual_call_count": 0,
        "automatic_observation_count": 0,
        "manual_condition_counts": {},
        "manual_event_counts": {},
        "automatic_event_counts": {},
        "active_task_ids": [],
        "pending_result_task_ids": [],
        "latest_manual": None,
        "latest_automatic": None,
    }


def _increment(mapping: dict[str, int], value: Any) -> None:
    value = _enum_value(value)
    if isinstance(value, str) and value:
        mapping[value] = mapping.get(value, 0) + 1


def _append_task_id(task_ids: list[int], task_id: Any) -> None:
    if _is_positive_int(task_id) and task_id not in task_ids:
        task_ids.append(task_id)


async def load_current_terminal_watch_snapshot(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
) -> str | None:
    if not isinstance(db, AsyncSession):
        return None
    if not _is_positive_int(profile_id):
        return None

    try:
        rows = await terminal_watch_reply_crud.list_watch_states(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
        )
    except OperationalError as exc:
        if "no such table: background_task" in str(exc).lower():
            return None
        raise
    if not rows:
        return None

    terminals: dict[str, dict[str, Any]] = {}
    manual_call_count = 0
    automatic_observation_count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        arguments = row.get("arguments")
        if not isinstance(arguments, Mapping):
            continue
        terminal_session_id = arguments.get("terminal_session_id")
        if not isinstance(terminal_session_id, str) or not terminal_session_id.strip():
            continue

        state = terminals.setdefault(terminal_session_id, _new_terminal_state(terminal_session_id))
        automatic = row.get("auto_terminal_watch") is True
        task_id = row.get("id")
        event = row.get("event")
        status = _enum_value(row.get("status"))
        reply_status = _enum_value(row.get("reply_status"))

        if automatic:
            automatic_observation_count += 1
            state["automatic_observation_count"] += 1
            _increment(state["automatic_event_counts"], event)
            state["latest_automatic"] = _latest_task(row)
        else:
            manual_call_count += 1
            state["manual_call_count"] += 1
            _increment(state["manual_condition_counts"], arguments.get("condition"))
            _increment(state["manual_event_counts"], event)
            state["latest_manual"] = _latest_task(row)

        if status in _ACTIVE_STATUSES:
            _append_task_id(state["active_task_ids"], task_id)
        if status in _PENDING_RESULT_STATUSES and row.get("auto_reply") is True and reply_status in _PENDING_REPLY_STATUSES:
            _append_task_id(state["pending_result_task_ids"], task_id)

    if not terminals:
        return None

    return _snapshot_json(
        {
            "type": "terminal_watch_state",
            "instruction": TERMINAL_WATCH_STATE_INSTRUCTION_PROMPT,
            "manual_call_count": manual_call_count,
            "automatic_observation_count": automatic_observation_count,
            "terminals": list(terminals.values()),
        }
    )


def _valid_snapshot_payload(snapshot: str) -> bool:
    if not isinstance(snapshot, str) or not snapshot.startswith(_SNAPSHOT_OPEN) or not snapshot.endswith(_SNAPSHOT_CLOSE):
        return False
    payload_text = snapshot[len(_SNAPSHOT_OPEN) : -len(_SNAPSHOT_CLOSE)]
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("type") == "terminal_watch_state" and isinstance(payload.get("terminals"), list)


def _strip_terminal_watch_snapshot(content: str | None) -> str | None:
    if not isinstance(content, str) or not content.endswith(_SNAPSHOT_CLOSE):
        return content

    if content.startswith(_SNAPSHOT_OPEN):
        if _valid_snapshot_payload(content):
            return ""
        return content

    marker_index = content.rfind(f"\n\n{_SNAPSHOT_OPEN}")
    if marker_index < 0:
        return content
    snapshot = content[marker_index + 2 :]
    if not _valid_snapshot_payload(snapshot):
        return content
    return content[:marker_index]


def strip_terminal_watch_snapshots(messages: list[InternalMessage]) -> list[InternalMessage]:
    stripped: list[InternalMessage] = []
    for message in messages:
        if (message.role == MessageRole.TOOL or is_context_summary_message(message)) and isinstance(message.content, str):
            stripped.append(
                message.model_copy(
                    deep=True,
                    update={"content": _strip_terminal_watch_snapshot(message.content)},
                )
            )
        else:
            stripped.append(message.model_copy(deep=True))
    return stripped


def append_terminal_watch_snapshot(
    messages: list[InternalMessage],
    snapshot: str | None,
) -> list[InternalMessage]:
    updated = strip_terminal_watch_snapshots(messages)
    if not snapshot:
        return updated

    target = next(
        (message for message in reversed(updated) if message.role == MessageRole.TOOL and isinstance(message.content, str)),
        None,
    )
    if target is None:
        target = next((message for message in updated if is_context_summary_message(message) and isinstance(message.content, str)), None)
    if target is None:
        return updated

    base_content = target.content or ""
    target.content = f"{base_content}\n\n{snapshot}" if base_content else snapshot
    return updated


def measure_terminal_watch_snapshot_tokens(snapshot: str | None) -> int:
    if not snapshot:
        return 0
    return estimate_tokens(f"\n\n{snapshot}")
