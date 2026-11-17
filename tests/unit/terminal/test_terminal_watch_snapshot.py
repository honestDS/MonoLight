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
from app.models.message import InternalConversationSummary, InternalMessage, MessageRole


def _old_snapshot(name: str = "old") -> dict:
    return {
        "type": "terminal_watch_state",
        "terminals": [{"terminal_session_id": name}],
    }


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
    payload = snapshot
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


def test_strip_removes_snapshots_from_independent_fields_without_changing_messages():
    tool_base = '{"event":"output"}'
    summary_message = InternalMessage(
        role=MessageRole.USER,
        content="preserved original request",
        conversation_summary=InternalConversationSummary(
            through_message_id=10,
            content="previous summary",
        ),
        current_terminal_watch=_old_snapshot("summary"),
    )
    messages = [
        InternalMessage(role=MessageRole.USER, content="user text", current_terminal_watch=_old_snapshot("user")),
        InternalMessage(role=MessageRole.TOOL, content=tool_base, current_terminal_watch=_old_snapshot("tool")),
        InternalMessage(role=MessageRole.TOOL, content="other tool output", current_terminal_watch={"type": "other"}),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant", current_terminal_watch=_old_snapshot("assistant")),
        summary_message,
    ]
    original = [message.model_dump(mode="json") for message in messages]

    stripped = strip_terminal_watch_snapshots(messages)

    assert [message.content for message in stripped] == [
        "user text",
        tool_base,
        "other tool output",
        "assistant",
        "preserved original request",
    ]
    assert stripped[1].content == tool_base
    assert stripped[4].role == MessageRole.USER
    assert stripped[4].conversation_summary is not None
    assert stripped[4].conversation_summary.content == "previous summary"
    assert all(message.current_terminal_watch is None for message in stripped)
    assert [message.model_dump(mode="json") for message in messages] == original


def test_append_strips_old_targets_without_mutating_inputs_or_user_messages():
    old = _old_snapshot()
    new = _old_snapshot("new")
    tool_base = '{"event":"output"}'
    summary_message = InternalMessage(
        role=MessageRole.USER,
        content="preserved original request",
        conversation_summary=InternalConversationSummary(
            through_message_id=10,
            content="previous summary",
        ),
        current_terminal_watch=_old_snapshot("summary"),
    )
    messages = [
        InternalMessage(role=MessageRole.USER, content="user request", current_terminal_watch=old),
        summary_message,
        InternalMessage(role=MessageRole.TOOL, content=tool_base, current_terminal_watch=_old_snapshot("tool")),
    ]
    original = [message.model_dump(mode="json") for message in messages]
    summary_only = InternalMessage(
        role=MessageRole.USER,
        content="preserved original request",
        conversation_summary=InternalConversationSummary(
            through_message_id=10,
            content="previous summary",
        ),
        current_terminal_watch=old,
    )
    summary_only_original = summary_only.model_dump(mode="json")

    updated = append_terminal_watch_snapshot(messages, new)
    summary_updated = append_terminal_watch_snapshot([summary_only], new)

    assert updated[0].content == "user request"
    assert updated[0].current_terminal_watch is None
    assert updated[1].role == MessageRole.USER
    assert updated[1].content == "preserved original request"
    assert updated[1].conversation_summary is not None
    assert updated[1].conversation_summary.content == "previous summary"
    assert updated[1].current_terminal_watch is None
    assert updated[2].content == tool_base
    assert updated[2].current_terminal_watch == new
    assert [message.model_dump(mode="json") for message in messages] == original
    assert summary_updated[0].role == MessageRole.USER
    assert summary_updated[0].content == "preserved original request"
    assert summary_updated[0].conversation_summary is not None
    assert summary_updated[0].conversation_summary.content == "previous summary"
    assert summary_updated[0].current_terminal_watch == new
    assert len(summary_updated) == 1
    assert summary_only.model_dump(mode="json") == summary_only_original

    updated[2].current_terminal_watch["terminals"][0]["terminal_session_id"] = "changed"
    assert new["terminals"][0]["terminal_session_id"] == "new"
    assert messages[2].current_terminal_watch == _old_snapshot("tool")


def test_append_handles_no_target_and_no_snapshot_without_adding_to_user():
    snapshot = _old_snapshot()
    user = InternalMessage(role=MessageRole.USER, content="user request")
    assistant = InternalMessage(role=MessageRole.ASSISTANT, content="assistant response")

    without_target = append_terminal_watch_snapshot([user, assistant], snapshot)
    without_snapshot = append_terminal_watch_snapshot(
        [InternalMessage(role=MessageRole.TOOL, content="tool result", current_terminal_watch=snapshot)],
        None,
    )
    unchanged = strip_terminal_watch_snapshots(
        [
            InternalMessage(role=MessageRole.TOOL, content="plain tool result"),
            InternalMessage(role=MessageRole.USER, content="plain user message"),
        ]
    )

    assert [message.role for message in without_target] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [message.content for message in without_target] == ["user request", "assistant response"]
    assert all(message.current_terminal_watch is None for message in without_target)
    assert without_snapshot[0].content == "tool result"
    assert without_snapshot[0].current_terminal_watch is None
    assert [message.content for message in unchanged] == ["plain tool result", "plain user message"]


def test_measure_snapshot_tokens_is_positive_for_a_snapshot():
    assert measure_terminal_watch_snapshot_tokens(_old_snapshot()) > 0
    assert measure_terminal_watch_snapshot_tokens(None) == 0
