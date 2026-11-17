import json

import pytest

from app.core.constants import ERR_LLM_API_RESPONSE_ERROR
from app.core.exceptions import LLMContextLengthException, LLMException
from app.models.message import InternalMessage, MessageRole
from app.transformers import http_transport
from app.transformers.deepseek.responses import DeepSeekResponsesTransformer


@pytest.fixture
def response_stream(monkeypatch):
    def install(events):
        consumed = []

        async def stream_sse_json(**kwargs):
            for event in events:
                consumed.append(event)
                normalized, _ = kwargs["normalize_event"](event)
                terminal = kwargs["is_terminal"](event)
                if normalized is not None:
                    yield normalized
                if terminal:
                    break

        monkeypatch.setattr(http_transport, "stream_sse_json", stream_sse_json)
        return consumed

    return install


async def _collect_stream():
    return [
        chunk
        async for chunk in DeepSeekResponsesTransformer().generate_stream(
            api_key="test-key",
            base_url="https://api.example/v1",
            model_id="deepseek-chat",
            messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        )
    ]


@pytest.mark.asyncio
async def test_generate_stream_deduplicates_text_and_reasoning_snapshots(response_stream) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_final",
        "status": "completed",
        "content": [{"type": "reasoning_text", "text": "private reasoning"}],
        "summary": [{"type": "summary_text", "text": "summary"}],
        "encrypted_content": "sealed",
        "provider_extension": {"preserved": True},
    }
    message_item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Public answer"}],
    }
    events = [
        {"type": "response.reasoning_text.delta", "output_index": 0, "content_index": 0, "delta": "private reasoning"},
        {"type": "response.reasoning_text.done", "output_index": 0, "content_index": 0, "text": "private reasoning"},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "Public answer"},
        {"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": "Public answer"},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
        {"type": "response.output_item.done", "output_index": 1, "item": message_item},
        {"type": "response.completed", "response": {"status": "completed", "output": [reasoning_item, message_item]}},
    ]
    response_stream(events)

    chunks = await _collect_stream()

    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert "".join(delta.get("content", "") for delta in deltas) == "Public answer"
    assert "".join(delta.get("reasoning_content", "") for delta in deltas) == "private reasoning"
    assert chunks[-1]["message_provider_metadata"] == {
        "protocol": "deepseek_responses",
        "output": [reasoning_item],
    }


@pytest.mark.asyncio
async def test_generate_stream_keeps_reasoning_item_snapshot_without_terminal_output(response_stream) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_only_done",
        "status": "completed",
        "content": [{"type": "reasoning_text", "text": "done-only reasoning"}],
        "provider_extension": {"retained": "as-is"},
    }
    events = [
        {"type": "response.created", "response": {"id": "resp_created"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": ""},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
        {"type": "response.completed", "response": {"status": "completed"}},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "after terminal"},
    ]
    consumed = response_stream(events)

    chunks = await _collect_stream()

    assert consumed == events[:-1]
    assert len(chunks) == 2
    assert chunks[0]["choices"][0]["delta"] == {"reasoning_content": "done-only reasoning"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["message_provider_metadata"] == {
        "protocol": "deepseek_responses",
        "output": [reasoning_item],
    }


@pytest.mark.asyncio
async def test_generate_stream_associates_parallel_function_calls_and_appends_argument_snapshots(response_stream) -> None:
    first_item = {
        "type": "function_call",
        "id": "fc_item_weather",
        "call_id": "call_weather",
        "name": "weather",
        "arguments": '{"city":"Paris"}',
        "status": "completed",
    }
    second_item = {
        "type": "function_call",
        "id": "fc_item_units",
        "call_id": "call_units",
        "name": "units",
        "arguments": '{"units":"celsius"}',
        "status": "completed",
    }
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**first_item, "arguments": ""}},
        {"type": "response.output_item.added", "output_index": 1, "item": {**second_item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_item_weather", "delta": '{"city":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_item_units", "delta": '{"units":'},
        {"type": "response.function_call_arguments.done", "item_id": "fc_item_units", "arguments": second_item["arguments"]},
        {"type": "response.function_call_arguments.done", "item_id": "fc_item_weather", "arguments": first_item["arguments"]},
        {"type": "response.output_item.done", "item": first_item},
        {"type": "response.output_item.done", "item": second_item},
        {"type": "response.completed", "response": {"status": "completed", "output": [first_item, second_item]}},
    ]
    response_stream(events)

    chunks = await _collect_stream()

    argument_parts = {0: "", 1: ""}
    call_ids = {}
    for chunk in chunks:
        for tool_delta in chunk["choices"][0]["delta"].get("tool_calls", []):
            index = tool_delta["index"]
            if tool_delta.get("id"):
                call_ids[index] = tool_delta["id"]
            function = tool_delta.get("function", {})
            argument_parts[index] += function.get("arguments", "")

    assert call_ids == {0: "call_weather", 1: "call_units"}
    assert json.loads(argument_parts[0]) == {"city": "Paris"}
    assert json.loads(argument_parts[1]) == {"units": "celsius"}


@pytest.mark.asyncio
async def test_generate_stream_backfills_terminal_snapshot_and_delivers_incomplete_usage(response_stream) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_terminal",
        "content": [{"type": "reasoning_text", "text": "snapshot reasoning"}],
        "provider_extension": "preserved",
    }
    message_item = {"type": "message", "content": [{"type": "output_text", "text": "snapshot answer"}]}
    tool_item = {
        "type": "function_call",
        "id": "fc_terminal",
        "call_id": "call_terminal",
        "name": "lookup",
        "arguments": '{"key":"value"}',
    }
    response_stream(
        [
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [reasoning_item, message_item, tool_item]},
            }
        ]
    )

    completed_chunks = await _collect_stream()
    completed_delta = completed_chunks[-1]["choices"][0]["delta"]
    assert len(completed_chunks) == 1
    assert completed_delta["content"] == "snapshot answer"
    assert completed_delta["reasoning_content"] == "snapshot reasoning"
    assert completed_delta["tool_calls"][0]["id"] == "call_terminal"
    assert completed_delta["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"key":"value"}'}
    assert completed_chunks[-1]["message_provider_metadata"] == {
        "protocol": "deepseek_responses",
        "output": [reasoning_item],
    }

    response_stream(
        [
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
                    "usage": {"input_tokens": 4, "output_tokens": 9},
                },
            }
        ]
    )

    incomplete_chunks = await _collect_stream()

    assert len(incomplete_chunks) == 1
    assert incomplete_chunks[0]["choices"][0]["finish_reason"] == "length"
    assert incomplete_chunks[0]["choices"][0]["delta"]["content"] == "partial"
    assert incomplete_chunks[0]["usage"]["prompt_tokens"] == 4
    assert incomplete_chunks[0]["usage"]["completion_tokens"] == 9
    assert incomplete_chunks[0]["usage"]["total_tokens"] == 13


@pytest.mark.parametrize(
    ("event", "expected_type", "provider_message"),
    [
        pytest.param(
            {"type": "response.failed", "response": {"status": "failed", "error": {"code": "context_length_exceeded", "message": "nested context limit"}}},
            LLMContextLengthException,
            "nested context limit",
            id="failed-nested-context-error",
        ),
        pytest.param(
            {"type": "error", "error": {"code": "context_length_exceeded", "message": "stream context limit"}},
            LLMContextLengthException,
            "stream context limit",
            id="error-context-code",
        ),
        pytest.param(
            {"type": "error", "error": {"code": "server_error", "message": "context_length_exceeded"}},
            LLMException,
            None,
            id="ordinary-error-is-not-context-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_stream_classifies_provider_errors(response_stream, event, expected_type, provider_message) -> None:
    response_stream([event])

    with pytest.raises(expected_type) as exc_info:
        await _collect_stream()

    if expected_type is LLMContextLengthException:
        assert exc_info.value.provider_message == provider_message
    else:
        assert type(exc_info.value) is LLMException
        assert exc_info.value.message == ERR_LLM_API_RESPONSE_ERROR
