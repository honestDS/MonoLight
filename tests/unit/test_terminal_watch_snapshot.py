import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.utils.dispatcher.terminal_watch_snapshot as snapshot_module
from app.core.prompts import TERMINAL_WATCH_STATE_INSTRUCTION_PROMPT
from app.core.utils.dispatcher.terminal_watch_snapshot import (
    append_terminal_watch_snapshot,
    load_current_terminal_watch_snapshot,
    measure_terminal_watch_snapshot_tokens,
    strip_terminal_watch_snapshots,
)
from app.models.background_task import BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, MessageRole

_SNAPSHOT_OPEN = "<current_terminal_watch_snapshot>"
_SNAPSHOT_CLOSE = "</current_terminal_watch_snapshot>"


def _decode_snapshot(snapshot: str) -> dict:
    assert snapshot.startswith(_SNAPSHOT_OPEN)
    assert snapshot.endswith(_SNAPSHOT_CLOSE)
    return json.loads(snapshot[len(_SNAPSHOT_OPEN) : -len(_SNAPSHOT_CLOSE)])


def _old_snapshot(name: str = "old") -> str:
    payload = {
        "type": "terminal_watch_state",
        "terminals": [{"terminal_session_id": name}],
    }
    return f"{_SNAPSHOT_OPEN}{json.dumps(payload, separators=(',', ':'))}{_SNAPSHOT_CLOSE}"


def _watch_row(
    task_id: int,
    *,
    tool_call_id: str,
    terminal_session_id: str,
    condition: str,
    status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
    event: str,
    created_at: datetime,
    automatic: bool,
    auto_reply: bool = True,
    text: str | None = None,
) -> dict:
    arguments = {
        "terminal_session_id": terminal_session_id,
        "after_offset": 0,
        "condition": condition,
    }
    if text is not None:
        arguments["text"] = text
    return {
        "id": task_id,
        "tool_call_id": tool_call_id,
        "arguments": arguments,
        "status": status,
        "reply_status": reply_status,
        "auto_reply": auto_reply,
        "created_at": created_at,
        "auto_terminal_watch": automatic,
        "event": event,
    }


@pytest.mark.asyncio
async def test_load_snapshot_separates_manual_and_automatic_watch_state(monkeypatch):
    first_created_at = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    latest_manual_created_at = datetime(2026, 10, 25, 12, 1, tzinfo=UTC)
    latest_automatic_created_at = datetime(2026, 10, 25, 12, 2, tzinfo=UTC)
    rows = [
        _watch_row(
            101,
            tool_call_id="manual-call-1",
            terminal_session_id="terminal-1",
            condition="output",
            status=BackgroundTaskStatus.PENDING,
            reply_status=BackgroundTaskReplyStatus.NONE,
            event="timeout",
            created_at=first_created_at,
            automatic=False,
        ),
        _watch_row(
            102,
            tool_call_id="manual-call-2",
            terminal_session_id="terminal-1",
            condition="output_contains",
            status=BackgroundTaskStatus.SUCCEEDED,
            reply_status=BackgroundTaskReplyStatus.PENDING,
            event="output_contains",
            created_at=latest_manual_created_at,
            automatic=False,
            text="needle",
        ),
        _watch_row(
            201,
            tool_call_id="automatic-call-1",
            terminal_session_id="terminal-1",
            condition="output",
            status=BackgroundTaskStatus.RUNNING,
            reply_status=BackgroundTaskReplyStatus.NONE,
            event="output_contains",
            created_at=first_created_at,
            automatic=True,
        ),
        _watch_row(
            202,
            tool_call_id="automatic-call-2",
            terminal_session_id="terminal-1",
            condition="output",
            status=BackgroundTaskStatus.FAILED,
            reply_status=BackgroundTaskReplyStatus.RUNNING,
            event="output",
            created_at=latest_automatic_created_at,
            automatic=True,
        ),
    ]

    async def list_watch_states(*_args, **_kwargs):
        return rows

    monkeypatch.setattr(snapshot_module.terminal_watch_reply_crud, "list_watch_states", list_watch_states)

    snapshot = await load_current_terminal_watch_snapshot(
        AsyncSession(),
        uid="user-1",
        session_id="session-1",
        profile_id=7,
    )

    assert snapshot is not None
    payload = _decode_snapshot(snapshot)
    assert payload["type"] == "terminal_watch_state"
    assert payload["instruction"] == TERMINAL_WATCH_STATE_INSTRUCTION_PROMPT
    assert payload["manual_call_count"] == 2
    assert payload["automatic_observation_count"] == 2

    terminal = payload["terminals"][0]
    assert terminal["terminal_session_id"] == "terminal-1"
    assert terminal["manual_call_count"] == 2
    assert terminal["automatic_observation_count"] == 2
    assert terminal["manual_condition_counts"] == {"output": 1, "output_contains": 1}
    assert terminal["manual_event_counts"] == {"timeout": 1, "output_contains": 1}
    assert terminal["automatic_event_counts"] == {"output_contains": 1, "output": 1}
    assert terminal["active_task_ids"] == [101, 201]
    assert terminal["pending_result_task_ids"] == [102, 202]
    assert terminal["latest_manual"] == {
        "task_id": 102,
        "tool_call_id": "manual-call-2",
        "arguments": {
            "terminal_session_id": "terminal-1",
            "after_offset": 0,
            "condition": "output_contains",
            "text": "needle",
        },
        "status": "succeeded",
        "event": "output_contains",
        "reply_status": "pending",
        "created_at": latest_manual_created_at.isoformat(),
        "matched": True,
    }
    assert terminal["latest_automatic"] == {
        "task_id": 202,
        "tool_call_id": "automatic-call-2",
        "arguments": {
            "terminal_session_id": "terminal-1",
            "after_offset": 0,
            "condition": "output",
        },
        "status": "failed",
        "event": "output",
        "reply_status": "running",
        "created_at": latest_automatic_created_at.isoformat(),
        "matched": False,
    }


@pytest.mark.asyncio
async def test_load_snapshot_returns_none_without_watch_rows(monkeypatch):
    async def list_watch_states(*_args, **_kwargs):
        return []

    monkeypatch.setattr(snapshot_module.terminal_watch_reply_crud, "list_watch_states", list_watch_states)

    snapshot = await load_current_terminal_watch_snapshot(
        AsyncSession(),
        uid="user-1",
        session_id="session-1",
        profile_id=7,
    )

    assert snapshot is None


def test_strip_removes_only_valid_snapshots_from_tools_and_summaries():
    valid = _old_snapshot()
    invalid = f"{_SNAPSHOT_OPEN}{json.dumps({'type': 'other', 'terminals': []})}{_SNAPSHOT_CLOSE}"
    tool_base = '{"event":"output"}'
    summary_base = '<conversation_summary id="summary-1">previous summary</conversation_summary>'
    user_content = f"user text\n\n{valid}"
    messages = [
        InternalMessage(role=MessageRole.USER, content=user_content),
        InternalMessage(role=MessageRole.TOOL, content=f"{tool_base}\n\n{valid}"),
        InternalMessage(role=MessageRole.TOOL, content=f"{tool_base}\n\n{invalid}"),
        InternalMessage(role=MessageRole.ASSISTANT, content=f"assistant\n\n{valid}"),
        InternalMessage(role=MessageRole.ASSISTANT, content=f"{summary_base}\n\n{valid}"),
    ]
    original = [message.model_dump(mode="json") for message in messages]

    stripped = strip_terminal_watch_snapshots(messages)

    assert stripped[0].content == user_content
    assert stripped[1].content == tool_base
    assert stripped[2].content == f"{tool_base}\n\n{invalid}"
    assert stripped[3].content == f"assistant\n\n{valid}"
    assert stripped[4].content == summary_base
    assert [message.model_dump(mode="json") for message in messages] == original


def test_append_strips_old_targets_without_mutating_inputs_or_user_messages():
    old = _old_snapshot()
    new = _old_snapshot("new")
    summary_base = '<conversation_summary id="summary-1">previous summary</conversation_summary>'
    tool_base = '{"event":"output"}'
    user_content = f"user request\n\n{old}"
    messages = [
        InternalMessage(role=MessageRole.USER, content=user_content),
        InternalMessage(role=MessageRole.ASSISTANT, content=f"{summary_base}\n\n{old}"),
        InternalMessage(role=MessageRole.TOOL, content=f"{tool_base}\n\n{old}"),
    ]
    original = [message.model_dump(mode="json") for message in messages]
    summary_only = InternalMessage(role=MessageRole.ASSISTANT, content=f"{summary_base}\n\n{old}")
    summary_only_original = summary_only.model_dump(mode="json")

    updated = append_terminal_watch_snapshot(messages, new)
    summary_updated = append_terminal_watch_snapshot([summary_only], new)

    assert updated[0].content == user_content
    assert updated[1].content == summary_base
    assert updated[2].content == f"{tool_base}\n\n{new}"
    assert [message.model_dump(mode="json") for message in messages] == original
    assert summary_updated[0].content == f"{summary_base}\n\n{new}"
    assert summary_only.model_dump(mode="json") == summary_only_original


def test_append_handles_no_target_and_no_snapshot_without_adding_to_user():
    snapshot = _old_snapshot()
    user = InternalMessage(role=MessageRole.USER, content="user request")
    assistant = InternalMessage(role=MessageRole.ASSISTANT, content="assistant response")

    without_target = append_terminal_watch_snapshot([user, assistant], snapshot)
    without_snapshot = append_terminal_watch_snapshot(
        [InternalMessage(role=MessageRole.TOOL, content=f"tool result\n\n{snapshot}")],
        None,
    )
    unchanged = strip_terminal_watch_snapshots(
        [
            InternalMessage(role=MessageRole.TOOL, content="plain tool result"),
            InternalMessage(role=MessageRole.USER, content="plain user message"),
        ]
    )

    assert [message.content for message in without_target] == ["user request", "assistant response"]
    assert all(snapshot not in (message.content or "") for message in without_target)
    assert without_snapshot[0].content == "tool result"
    assert [message.content for message in unchanged] == ["plain tool result", "plain user message"]


def test_measure_snapshot_tokens_is_positive_for_a_snapshot():
    assert measure_terminal_watch_snapshot_tokens(_old_snapshot()) > 0
