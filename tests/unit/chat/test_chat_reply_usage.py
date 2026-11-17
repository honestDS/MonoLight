import pytest

from app.models.message import InternalMessage, MessageRole
from tests.unit.chat.audited_reply_support import _run_audited_interactive_dispatch
from tests.unit.chat.reply_retry_context_support import _patch_session_crud as _patch_session_crud


@pytest.mark.asyncio
async def test_interactive_accumulates_output_tokens_and_session_cache_metrics(monkeypatch):
    checkpoints = []
    events = []

    async def save_checkpoint(checkpoint):
        checkpoints.append(checkpoint)

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    async def publish_event(event):
        events.append(event)

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        response_usages=[
            {
                "prompt_tokens": 1000,
                "completion_tokens": 12,
                "cached_tokens": 200,
            },
            {
                "prompt_tokens": 1100,
                "completion_tokens": 20,
                "cached_tokens": 440,
            },
        ],
    )

    metadata_events = [event for event in events if event["type"] == "llm_request_metadata"]
    assert [event["input_tokens_source"] for event in metadata_events] == [
        "estimated",
        "provider",
        "estimated",
        "provider",
    ]
    assert [event["summary_trigger_tokens"] for event in metadata_events] == [2908] * 4
    provider_metadata_events = [event for event in metadata_events if event["input_tokens_source"] == "provider"]
    assert [event["input_tokens"] for event in provider_metadata_events] == [1000, 1100]
    assert [event["total_input_tokens"] for event in metadata_events] == [0, 1000, 1000, 2100]
    assert [event["total_cached_tokens"] for event in metadata_events] == [0, 200, 200, 640]
    assert [event["cache_hit_rate"] for event in metadata_events] == [None, 200 / 1000, 200 / 1000, 640 / 2100]
    assert metadata_events[1]["output_tokens"] == 12
    assert metadata_events[1]["total_output_tokens"] == 12
    assert metadata_events[2]["output_tokens"] == 12
    assert metadata_events[2]["total_output_tokens"] == 12
    assert metadata_events[2]["cached_tokens"] == 200
    assert metadata_events[3]["output_tokens"] == 32
    assert metadata_events[3]["total_output_tokens"] == 32
    assert metadata_events[3]["cached_tokens"] == 440
    assert any(checkpoint["total_output_tokens"] == 12 for checkpoint in checkpoints)
    assert any(checkpoint.get("session_total_output_tokens") == 12 for checkpoint in checkpoints)
    assert any(checkpoint["session_total_input_tokens"] == 1000 and checkpoint["session_total_cached_tokens"] == 200 for checkpoint in checkpoints)
    assert response["llm_request_metadata"]["output_tokens"] == 32
    assert response["llm_request_metadata"]["total_output_tokens"] == 32
    assert response["llm_request_metadata"]["cache_hit_rate"] == 640 / 2100
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_final_request_does_not_use_local_token_baseline_for_hard_limit(monkeypatch):
    trim_calls = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        response_usages=[
            {"prompt_tokens": 1000, "completion_tokens": 10},
            {"prompt_tokens": 1100, "completion_tokens": 20},
        ],
        trim_calls_target=trim_calls,
    )

    assert response["choices"][0]["message"]["content"] == "finished"
    assert unknown_calls == []
    assert len(trim_calls) == 2
    assert all(set(call) == {"messages", "context_window_k", "max_tokens"} for call in trim_calls)


@pytest.mark.asyncio
async def test_interactive_preserves_output_tokens_when_first_usage_has_no_prompt_tokens(monkeypatch):
    events = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    async def publish_event(event):
        events.append(event)

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        response_usages=[
            {"completion_tokens": 12},
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 30,
            },
        ],
    )

    metadata_events = [event for event in events if event["type"] == "llm_request_metadata"]
    assert metadata_events[2]["input_tokens_source"] == "estimated"
    assert metadata_events[2]["output_tokens"] == 12
    assert metadata_events[3]["input_tokens_source"] == "provider"
    assert metadata_events[3]["output_tokens"] == 32
    assert response["llm_request_metadata"]["output_tokens"] == 32
    assert unknown_calls == []


@pytest.mark.asyncio
async def test_interactive_resume_continues_total_output_tokens(monkeypatch):
    events = []

    async def save_checkpoint(_checkpoint):
        return None

    async def process_tool(tool_call, *args, **kwargs):
        return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content='{"status":"success"}')

    async def publish_event(event):
        events.append(event)

    response, unknown_calls = await _run_audited_interactive_dispatch(
        monkeypatch,
        save_checkpoint,
        process_tool,
        audit_result=None,
        stream_event_callback=publish_event,
        response_usages=[
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "cached_tokens": 20,
            },
            {
                "prompt_tokens": 120,
                "completion_tokens": 3,
                "cached_tokens": 30,
            },
        ],
        execution_resume_state={
            "messages": [InternalMessage(role=MessageRole.USER, content="request").model_dump(mode="json")],
            "turn_messages": [],
            "files_to_user": [],
            "current_turn": 0,
            "total_output_tokens": 50,
            "session_total_output_tokens": 12,
            "session_total_input_tokens": 500,
            "session_total_cached_tokens": 100,
        },
    )

    provider_metadata_events = [event for event in events if event["type"] == "llm_request_metadata" and event["input_tokens_source"] == "provider"]
    assert [event["output_tokens"] for event in provider_metadata_events] == [57, 60]
    assert [event["total_output_tokens"] for event in provider_metadata_events] == [19, 22]
    assert [event["total_input_tokens"] for event in provider_metadata_events] == [600, 720]
    assert [event["total_cached_tokens"] for event in provider_metadata_events] == [120, 150]
    assert [event["cache_hit_rate"] for event in provider_metadata_events] == [120 / 600, 150 / 720]
    assert response["llm_request_metadata"]["output_tokens"] == 60
    assert response["llm_request_metadata"]["total_output_tokens"] == 22
    assert response["llm_request_metadata"]["total_input_tokens"] == 720
    assert response["llm_request_metadata"]["total_cached_tokens"] == 150
    assert response["llm_request_metadata"]["cache_hit_rate"] == 150 / 720
    assert unknown_calls == []
