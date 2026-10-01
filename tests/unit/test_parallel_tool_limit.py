import json
from collections import Counter
from types import SimpleNamespace

import pytest

import app.core.dispatchers.interactive_tools as interactive_tools
from app.core.prompts import ERR_PARALLEL_LIMIT_EXCEEDED
from app.core.tools.list_background_tasks import LIST_BACKGROUND_TASKS_TOOL_SCHEMA
from app.core.tools.todo import MANAGE_TODO_TOOL_NAME, MANAGE_TODO_TOOL_SCHEMA, ManageTodoExecutor
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.models.message import InternalMessage, InternalToolCall, MessageRole


def _tool_calls(count: int) -> list[InternalToolCall]:
    return [InternalToolCall(id=f"call-{index}", name=f"tool-{index}", arguments={}) for index in range(count)]


def _valid_todo_calls(count: int) -> list[InternalToolCall]:
    return [
        InternalToolCall(
            id=f"call-{index}",
            name=MANAGE_TODO_TOOL_NAME,
            arguments={"operation": "read"},
        )
        for index in range(count)
    ]


def _state(tool_limit: int = 5, tools: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        db=object(),
        uid="uid",
        session_id="session",
        active_tasks=set(),
        session_source="test",
        goal_mode=False,
        final_message_dedupe_key=None,
        stream_event_callback=None,
        show_tool_calls=False,
        username="tester",
        profile=SimpleNamespace(id=1),
        checkpoint_state=SimpleNamespace(
            callback=None,
            upper_message_id=None,
            memory_recall_boundary_message_id=None,
        ),
        turn_messages=[],
        files_to_user=[],
        latest_llm_request_metadata=None,
        messages=[],
        current_turn=0,
        cfg=SimpleNamespace(
            tool=SimpleNamespace(max_parallel_tools=tool_limit, executor_max_workers=tool_limit),
        ),
        memory_enabled=False,
        model_entry={"model_id": "gpt-5.6-luna", "protocol": "OPENAI"},
        chat_params={"context_window_k": 4, "max_tokens": 100},
        tools=[MANAGE_TODO_TOOL_SCHEMA] if tools is None else tools,
        allowed_knowledge_base_ids=[],
        additional_user_messages_context=SimpleNamespace(),
    )


def _install_storage_stubs(monkeypatch) -> None:
    async def save_tool_response(
        _db,
        _session_id,
        _uid,
        _profile_id,
        tool_result,
        messages,
        turn_messages,
    ):
        stored_result = tool_result.model_copy(update={"id": len(messages) + 1})
        messages.append(stored_result)
        turn_messages.append(stored_result)
        return stored_result

    async def persist_todo_snapshot(*_args, **_kwargs):
        return None

    async def save_checkpoint(*_args, **_kwargs):
        return None

    monkeypatch.setattr(interactive_tools, "save_tool_response", save_tool_response)
    monkeypatch.setattr(
        interactive_tools,
        "persist_session_todo_snapshot_on_tool_results",
        persist_todo_snapshot,
    )
    monkeypatch.setattr(interactive_tools, "_save_execution_checkpoint", save_checkpoint)


def test_handle_parallel_tool_limit_rejects_only_calls_after_limit() -> None:
    calls = _tool_calls(8)

    executable, rejected = handle_parallel_tool_limit(calls, max_parallel_tools=5)

    assert executable == calls[:5]
    assert [result.tool_call_id for result in rejected] == [call.id for call in calls[5:]]
    for call, result in zip(calls[5:], rejected, strict=True):
        assert result.role is MessageRole.TOOL
        assert result.tool_call_id == call.id
        assert json.loads(result.content) == {
            "status": "failed",
            "tool_name": call.name,
            "error": "parallel_limit_exceeded",
            "requested": 8,
            "limit": 5,
            "executed": False,
            "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(requested=8, limit=5),
        }


def test_under_limit_preserves_all_calls_without_rejections() -> None:
    calls = _tool_calls(4)

    executable, rejected = handle_parallel_tool_limit(calls, max_parallel_tools=5)

    assert executable == calls
    assert rejected == []


@pytest.mark.asyncio
async def test_interactive_round_persists_rejected_calls_and_executes_prefix(monkeypatch) -> None:
    calls = _valid_todo_calls(8)
    calls[6] = calls[6].model_copy(update={"arguments": {}})
    state = _state()
    ai_msg = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=calls)
    executed_ids: list[str] = []
    _install_storage_stubs(monkeypatch)

    async def audit_tool_round(*_args, **_kwargs):
        return None

    async def execute_isolated_tool_call(_context, tool_call):
        executed_ids.append(tool_call.id)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=json.dumps({"status": "ok", "tool_name": tool_call.name}),
        )

    monkeypatch.setattr(interactive_tools, "audit_tool_round", audit_tool_round)
    monkeypatch.setattr(interactive_tools, "_execute_isolated_tool_call", execute_isolated_tool_call)
    monkeypatch.setattr(
        interactive_tools,
        "calculate_tool_result_round_budget_tokens",
        lambda **_kwargs: 1,
    )
    monkeypatch.setattr(interactive_tools, "extract_files_to_user", lambda _results: [])

    await interactive_tools.handle_interactive_tool_round(
        state,
        ai_msg=ai_msg,
        saved_msg=SimpleNamespace(id=10),
        response_id="response",
    )

    assert set(executed_ids) == {call.id for call in calls[:5]}
    assert set(executed_ids).isdisjoint(call.id for call in calls[5:])
    for messages in (state.messages, state.turn_messages):
        tool_results = [message for message in messages if message.role is MessageRole.TOOL]
        assert Counter(message.tool_call_id for message in tool_results) == Counter(call.id for call in calls)
        assert len(tool_results) == len(calls)
        result_by_id = {message.tool_call_id: message for message in tool_results}
        for call in calls[:5]:
            assert json.loads(result_by_id[call.id].content) == {
                "status": "ok",
                "tool_name": call.name,
            }
        for call in calls[5:]:
            assert json.loads(result_by_id[call.id].content) == {
                "status": "failed",
                "tool_name": call.name,
                "error": "parallel_limit_exceeded",
                "requested": len(calls),
                "limit": state.cfg.tool.max_parallel_tools,
                "executed": False,
                "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(
                    requested=len(calls),
                    limit=state.cfg.tool.max_parallel_tools,
                ),
            }


@pytest.mark.asyncio
async def test_exclusive_round_policy_rejects_mixed_round_atomically(monkeypatch) -> None:
    ordinary_call = InternalToolCall(
        id="ordinary-call",
        name=LIST_BACKGROUND_TASKS_TOOL_SCHEMA["function"]["name"],
        arguments={},
    )
    exclusive_call = InternalToolCall(
        id="exclusive-call",
        name=MANAGE_TODO_TOOL_NAME,
        arguments={"operation": "read"},
    )
    calls = [ordinary_call, exclusive_call]
    state = _state(
        tool_limit=1,
        tools=[LIST_BACKGROUND_TASKS_TOOL_SCHEMA, MANAGE_TODO_TOOL_SCHEMA],
    )
    state.cfg.tool.enabled_tools = [ordinary_call.name]
    ai_msg = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=calls)
    executed_ids: list[str] = []
    _install_storage_stubs(monkeypatch)
    monkeypatch.setattr(ManageTodoExecutor, "round_execution_policy", "exclusive")

    async def audit_tool_round(*_args, **_kwargs):
        return None

    async def execute_isolated_tool_call(_context, tool_call):
        executed_ids.append(tool_call.id)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=json.dumps({"status": "ok", "tool_name": tool_call.name}),
        )

    monkeypatch.setattr(interactive_tools, "audit_tool_round", audit_tool_round)
    monkeypatch.setattr(interactive_tools, "_execute_isolated_tool_call", execute_isolated_tool_call)

    await interactive_tools.handle_interactive_tool_round(
        state,
        ai_msg=ai_msg,
        saved_msg=SimpleNamespace(id=10),
        response_id="response",
    )

    assert executed_ids == []
    for messages in (state.messages, state.turn_messages):
        assert Counter(message.tool_call_id for message in messages) == Counter(call.id for call in calls)
        assert len(messages) == len(calls)
        for call, message in zip(calls, messages, strict=True):
            assert message.role is MessageRole.TOOL
            assert message.tool_call_id == call.id
            payload = json.loads(message.content)
            assert payload["status"] == "failed"
            assert payload["tool_name"] == call.name
            assert payload["error"] != "parallel_limit_exceeded"
            assert "parallel_limit_exceeded" not in message.content
            if call.id == exclusive_call.id:
                assert payload["round_execution_policy"] == "exclusive"


@pytest.mark.asyncio
async def test_atomic_round_policy_rejects_over_limit_round(monkeypatch) -> None:
    calls = _valid_todo_calls(8)
    state = _state()
    ai_msg = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=calls)
    executed_ids: list[str] = []
    _install_storage_stubs(monkeypatch)
    monkeypatch.setattr(ManageTodoExecutor, "round_execution_policy", "atomic")

    async def audit_tool_round(*_args, **_kwargs):
        return None

    async def execute_isolated_tool_call(_context, tool_call):
        executed_ids.append(tool_call.id)
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=json.dumps({"status": "ok", "tool_name": tool_call.name}),
        )

    monkeypatch.setattr(interactive_tools, "audit_tool_round", audit_tool_round)
    monkeypatch.setattr(interactive_tools, "_execute_isolated_tool_call", execute_isolated_tool_call)

    await interactive_tools.handle_interactive_tool_round(
        state,
        ai_msg=ai_msg,
        saved_msg=SimpleNamespace(id=10),
        response_id="response",
    )

    assert executed_ids == []
    for messages in (state.messages, state.turn_messages):
        assert Counter(message.tool_call_id for message in messages) == Counter(call.id for call in calls)
        assert len(messages) == len(calls)
        for call, message in zip(calls, messages, strict=True):
            assert message.role is MessageRole.TOOL
            assert message.tool_call_id == call.id
            payload = json.loads(message.content)
            assert payload["status"] == "failed"
            assert payload["tool_name"] == call.name
            assert payload["error"] != "parallel_limit_exceeded"
            assert "parallel_limit_exceeded" not in message.content
            assert payload["round_execution_policy"] == "atomic"
