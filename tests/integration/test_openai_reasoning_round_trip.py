import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import GOAL_EXECUTION_PHASE_RUNNING
from app.core.context import ContextManager
from app.core.crud.session.message import message_crud
from app.core.dispatchers import interactive_generation as interactive_generation_module
from app.core.exceptions import LLMException
from app.core.utils.dispatcher import channel_call
from app.core.utils.dispatcher.provider_state import discard_mismatched_provider_state
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, InternalToolCall, Message, MessageResponse, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.providers.llm.client import LLMClient
from app.transformers.openai.base import BaseOpenAITransformer
from app.transformers.openai.chat_completions import OpenAIChatCompletionsTransformer
from app.transformers.openai.responses import OpenAIResponsesTransformer

SESSION_ID = "openai-reasoning-round-trip"
UID = "round-trip-user"
PROFILE_ID = 1
MODEL_ID = "round-trip-model"
BASE_URL = "https://provider.invalid/v1"
API_KEY = "test-key"
TOOL_ARGUMENTS = {"city": "Paris"}
TOOL_NAME = "lookup"


def _chat_response(reasoning: str, with_tools: bool) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "[tool_call]" if with_tools else "Answer",
        "reasoning": reasoning,
    }
    if with_tools:
        message["tool_calls"] = [
            {
                "id": "provider-chat-call",
                "type": "function",
                "vendor_trace": "chat-trace",
                "function": {
                    "name": TOOL_NAME,
                    "arguments": json.dumps(TOOL_ARGUMENTS),
                    "vendor_function": "chat-function",
                },
            }
        ]
    return {
        "id": "chat-response-1",
        "object": "chat.completion",
        "model": MODEL_ID,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if with_tools else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _responses_response(reasoning: str, with_tools: bool) -> dict[str, Any]:
    reasoning_item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "sealed-state", "summary": [] if not reasoning else [{"type": "summary_text", "text": reasoning}]}
    output: list[dict[str, Any]] = [reasoning_item]
    if with_tools:
        output.append({"type": "function_call", "id": "fc_1", "call_id": "provider-responses-call", "name": TOOL_NAME, "arguments": json.dumps(TOOL_ARGUMENTS), "status": "completed", "vendor_trace": "responses-trace"})
    else:
        output.append({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Answer", "annotations": []}]})
    return {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": MODEL_ID,
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _chat_event(
    model: str,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    choice: dict[str, Any] = {"delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    event: dict[str, Any] = {"model": model, "choices": [choice]}
    if usage is not None:
        event["usage"] = usage
    return event


def _chat_stream_events(response: dict[str, Any]) -> list[dict[str, Any]]:
    choice = response["choices"][0]
    message = choice["message"]
    model = response["model"]
    events = [_chat_event(model, {"reasoning": message.get("reasoning", "")})]
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        tool_call = tool_calls[0]
        function = tool_call["function"]
        arguments = function["arguments"]
        split_at = len(arguments) // 2
        started_tool_call = {"index": 0, "id": tool_call["id"], "type": tool_call.get("type", "function"), "function": {"name": function["name"]}}
        events += [_chat_event(model, {"content": message["content"], "tool_calls": [started_tool_call]}), _chat_event(model, {"tool_calls": [{"index": 0, "function": {"arguments": arguments[:split_at]}}]}), _chat_event(model, {"tool_calls": [{"index": 0, "function": {"arguments": arguments[split_at:]}}]})]
    else:
        events.append(_chat_event(model, {"content": message["content"]}))
    events.append(_chat_event(model, {}, finish_reason=choice["finish_reason"], usage=response["usage"]))
    return events


def _responses_stream_events(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response["output"]
    reasoning_item = next(item for item in output if item.get("type") == "reasoning")
    summary = reasoning_item.get("summary") or []
    reasoning_text = summary[0].get("text", "") if summary else ""
    events: list[dict[str, Any]] = [{"type": "response.reasoning_summary_text.delta", "output_index": output.index(reasoning_item), "summary_index": 0, "delta": reasoning_text}]
    function_call = next((item for item in output if item.get("type") == "function_call"), None)
    if function_call is not None:
        arguments = function_call["arguments"]
        split_at = len(arguments) // 2
        output_index = output.index(function_call)
        item_id = function_call["id"]
        added_item = {**function_call, "arguments": "", "status": "in_progress"}
        events += [
            {"type": "response.output_item.added", "output_index": output_index, "item": added_item},
            {"type": "response.function_call_arguments.delta", "output_index": output_index, "item_id": item_id, "delta": arguments[:split_at]},
            {"type": "response.function_call_arguments.delta", "output_index": output_index, "item_id": item_id, "delta": arguments[split_at:]},
            {"type": "response.function_call_arguments.done", "output_index": output_index, "item_id": item_id, "arguments": arguments},
            {"type": "response.output_item.done", "output_index": output_index, "item": {**function_call, "arguments": arguments, "status": "completed"}},
        ]
    else:
        message = next(item for item in output if item.get("type") == "message")
        text = next(part["text"] for part in message["content"] if part.get("type") == "output_text")
        output_index = output.index(message)
        events += [
            {"type": "response.output_text.delta", "output_index": output_index, "content_index": 0, "delta": text},
            {"type": "response.output_text.done", "output_index": output_index, "content_index": 0, "text": text},
        ]
    events.append({"type": "response.completed", "response": deepcopy(response)})
    return events


async def _ignore_content(_content: str) -> None:
    return None


@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("reasoning", ["Think carefully.", ""])
@pytest.mark.asyncio
async def test_openai_reasoning_round_trip(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    stream: bool,
    with_tools: bool,
    reasoning: str,
) -> None:
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db_session.commit()
    user_message = await save_message(db_session, SESSION_ID, UID, MessageRole.USER, MessageType.TEXT, InternalMessage(role=MessageRole.USER, content="Question"), PROFILE_ID)

    raw_response = _chat_response(reasoning, with_tools) if protocol == "openai" else _responses_response(reasoning, with_tools)
    raw_response["model"] = "provider-real-model"
    stream_events = _chat_stream_events(raw_response) if protocol == "openai" else _responses_stream_events(raw_response)
    captured_payloads: list[dict[str, Any]] = []

    async def fake_post(_self: BaseOpenAITransformer, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured_payloads.append(deepcopy(payload))
        return deepcopy(raw_response)

    async def fake_stream(_self: BaseOpenAITransformer, *, payload: dict[str, Any], normalize_event, **_kwargs: Any):
        captured_payloads.append(deepcopy(payload))
        for raw_event in stream_events:
            normalized, _ = normalize_event(deepcopy(raw_event))
            if normalized is not None:
                yield normalized

    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", fake_post)
    monkeypatch.setattr(BaseOpenAITransformer, "_stream_sse_json", fake_stream)

    tools = [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": "Look up a city.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    generate = LLMClient.generate_with_stream_callback if stream else LLMClient.generate
    call_kwargs: dict[str, Any] = {
        "api_key": API_KEY,
        "base_url": BASE_URL,
        "model_id": MODEL_ID,
        "messages": [user_message],
        "tools": tools if with_tools else None,
        "protocol": protocol,
        "channel_id": 1,
    }
    if stream:
        call_kwargs["on_content"] = _ignore_content

    first_response = await generate(**call_kwargs)
    assert first_response.model == "provider-real-model"
    assert first_response.message.provider_metadata["source"] == {
        "channel_id": 1,
        "model_id": MODEL_ID,
        "protocol": protocol,
    }
    expected_reasoning = reasoning if protocol == "openai" and not stream else reasoning or None
    assert first_response.message.reasoning_content == expected_reasoning
    if with_tools:
        assert first_response.message.content is None
        assert first_response.message.tool_calls and len(first_response.message.tool_calls) == 1
        normalized_tool_id = first_response.message.tool_calls[0].id
        assert normalized_tool_id.startswith("call_")
        assert first_response.message.tool_calls[0].name == TOOL_NAME
        assert first_response.message.tool_calls[0].arguments == TOOL_ARGUMENTS
    else:
        normalized_tool_id = None
        assert first_response.message.content == "Answer"
        assert first_response.message.tool_calls is None

    saved_assistant = await save_assistant_message(db_session, SESSION_ID, UID, PROFILE_ID, first_response.message)
    assistant_id = saved_assistant.id
    db_session.expire_all()
    stored_assistant = await db_session.get(Message, assistant_id)
    assert stored_assistant is not None
    assert stored_assistant.provider_metadata == first_response.message.provider_metadata
    assert stored_assistant.reasoning_content == first_response.message.reasoning_content
    public_message = MessageResponse.model_validate(stored_assistant).model_dump(mode="json")
    assert "provider_metadata" not in public_message
    assert "sealed-state" not in json.dumps(public_message, ensure_ascii=False)
    assert public_message["content"] != reasoning
    if with_tools:
        stored_body = json.loads(stored_assistant.content or "{}")
        assert "provider_metadata" not in stored_body
        assert stored_body.get("content") is None
    else:
        assert stored_assistant.content == "Answer"
        assert public_message["content"] == "Answer"

    if with_tools:
        await save_message(db_session, SESSION_ID, UID, MessageRole.TOOL, MessageType.TOOL_RESULT, InternalMessage(role=MessageRole.TOOL, content="tool output", tool_call_id=normalized_tool_id), PROFILE_ID)
    await save_message(db_session, SESSION_ID, UID, MessageRole.USER, MessageType.TEXT, InternalMessage(role=MessageRole.USER, content="Follow-up"), PROFILE_ID)
    history = await ContextManager.get_messages(db_session, SESSION_ID, UID, profile=Profile(id=PROFILE_ID, uid=UID, name="round-trip", configs={}), current_message="Follow-up")
    history_assistant = next(message for message in history if message.role == MessageRole.ASSISTANT)
    assert history_assistant.reasoning_content == expected_reasoning
    if with_tools:
        assert history_assistant.tool_calls and history_assistant.tool_calls[0].id == normalized_tool_id
        history_tool = next(message for message in history if message.role == MessageRole.TOOL)
        assert history_tool.tool_call_id == normalized_tool_id

    call_kwargs["messages"] = history
    await generate(**call_kwargs)
    assert len(captured_payloads) == 2
    first_payload, second_payload = captured_payloads
    if protocol == "openai_responses":
        assert first_payload["store"] is False
        assert second_payload["store"] is False
        assert first_payload["include"] == ["reasoning.encrypted_content"]
        assert second_payload["include"] == ["reasoning.encrypted_content"]
        input_items = second_payload["input"]
        raw_reasoning_item = next(item for item in raw_response["output"] if item.get("type") == "reasoning")
        reasoning_items = [item for item in input_items if item.get("type") == "reasoning"]
        assert reasoning_items == [raw_reasoning_item]
        assert len(reasoning_items) == 1
        reasoning_index = input_items.index(reasoning_items[0])
        assert all("provider_metadata" not in item for item in input_items)
        if with_tools:
            function_item = next(item for item in input_items if item.get("type") == "function_call")
            assert input_items.index(function_item) > reasoning_index
            assert function_item["call_id"] == normalized_tool_id
            assert function_item["name"] == TOOL_NAME
            assert json.loads(function_item["arguments"]) == TOOL_ARGUMENTS
            output_item = next(item for item in input_items if item.get("type") == "function_call_output")
            assert output_item["call_id"] == normalized_tool_id
        else:
            assistant_item = next(item for item in input_items if item.get("role") == "assistant")
            assert input_items.index(assistant_item) > reasoning_index
            assert assistant_item["content"] == "Answer"
    else:
        messages = second_payload["messages"]
        assistant_item = next(item for item in messages if item.get("role") == "assistant")
        assert assistant_item["reasoning_content"] == reasoning
        assert "provider_metadata" not in assistant_item
        if with_tools:
            assert assistant_item["content"] == "[tool_call]"
            provider_tool_call = assistant_item["tool_calls"][0]
            assert provider_tool_call["id"] == normalized_tool_id
            assert provider_tool_call["function"]["name"] == TOOL_NAME
            assert json.loads(provider_tool_call["function"]["arguments"]) == TOOL_ARGUMENTS
            tool_item = next(item for item in messages if item.get("role") == "tool")
            assert tool_item["tool_call_id"] == normalized_tool_id
        else:
            assert assistant_item["content"] == "Answer"


LEGACY_MESSAGE_METADATA = {"legacy_message": "kept"}
LEGACY_TOOL_METADATA = {"legacy_tool": "kept"}


@pytest.mark.parametrize(
    ("column_metadata", "expected_metadata"),
    [(None, LEGACY_MESSAGE_METADATA), ({}, {}), ({"column_message": "preferred"}, {"column_message": "preferred"})],
    ids=["column-null-falls-back", "empty-column-wins", "new-column-wins"],
)
@pytest.mark.asyncio
async def test_legacy_tool_call_metadata_precedence_and_provider_round_trip(
    db_session: AsyncSession,
    column_metadata: dict[str, Any] | None,
    expected_metadata: dict[str, Any],
) -> None:
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    old_content = json.dumps(
        {
            "role": "assistant",
            "content": "legacy tool body",
            "tool_calls": [
                {
                    "id": "legacy-call",
                    "name": TOOL_NAME,
                    "arguments": TOOL_ARGUMENTS,
                    "provider_metadata": LEGACY_TOOL_METADATA,
                }
            ],
            "provider_metadata": LEGACY_MESSAGE_METADATA,
        }
    )
    legacy_message = Message(session_id=SESSION_ID, uid=UID, role=MessageRole.ASSISTANT, type=MessageType.TOOL_CALL, content=old_content, provider_metadata=column_metadata, profile_id=PROFILE_ID, is_processed=True)
    db_session.add(legacy_message)
    await db_session.commit()
    legacy_id = legacy_message.id
    db_session.expire_all()
    stored = await db_session.get(Message, legacy_id)
    assert stored is not None
    parsed_messages = parse_db_messages_to_internal([stored])
    assert len(parsed_messages) == 1
    parsed = parsed_messages[0]
    assert parsed.provider_metadata == expected_metadata
    assert parsed.content == "legacy tool body"
    assert parsed.tool_calls and parsed.tool_calls[0].provider_metadata == LEGACY_TOOL_METADATA

    chat_provider = OpenAIChatCompletionsTransformer.to_provider([parsed])
    assert chat_provider[0]["content"] == "legacy tool body"
    assert chat_provider[0]["tool_calls"][0]["id"] == "legacy-call"
    assert json.loads(chat_provider[0]["tool_calls"][0]["function"]["arguments"]) == TOOL_ARGUMENTS
    responses_provider = OpenAIResponsesTransformer.to_provider([parsed])
    assert responses_provider[0]["role"] == "assistant"
    assert responses_provider[0]["content"] == "legacy tool body"
    assert responses_provider[1]["type"] == "function_call"
    assert responses_provider[1]["call_id"] == "legacy-call"
    assert json.loads(responses_provider[1]["arguments"]) == TOOL_ARGUMENTS


@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("change", ["channel", "model", "protocol"])
@pytest.mark.asyncio
async def test_persisted_provider_state_is_discarded_on_source_change(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    with_tools: bool,
    change: str,
) -> None:
    reasoning = "Think carefully."
    initial_source = {"channel_id": 1, "model_id": MODEL_ID, "protocol": protocol}
    other_session_id = f"{SESSION_ID}-other"
    other_uid = "round-trip-other-user"
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    db_session.add(ChatSession(session_id=other_session_id, uid=other_uid, profile_id=PROFILE_ID))
    await db_session.commit()

    user_message = await save_message(
        db_session,
        SESSION_ID,
        UID,
        MessageRole.USER,
        MessageType.TEXT,
        InternalMessage(role=MessageRole.USER, content="Question"),
        PROFILE_ID,
    )
    raw_response = _chat_response(reasoning, with_tools) if protocol == "openai" else _responses_response(reasoning, with_tools)
    raw_response["model"] = "provider-real-model"

    async def fake_post(_self: BaseOpenAITransformer, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return deepcopy(raw_response)

    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", fake_post)
    tools = [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": "Look up a city.",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    first_response = await LLMClient.generate(
        api_key=API_KEY,
        base_url=BASE_URL,
        model_id=MODEL_ID,
        messages=[user_message],
        tools=tools if with_tools else None,
        protocol=protocol,
        channel_id=1,
    )
    assert first_response.model == "provider-real-model"
    assert first_response.message.reasoning_content == reasoning
    assert first_response.message.provider_metadata["source"] == initial_source

    saved_assistant = await save_assistant_message(db_session, SESSION_ID, UID, PROFILE_ID, first_response.message)
    assistant_id = saved_assistant.id
    assert assistant_id is not None
    old_assistant_snapshot = first_response.message.model_copy(update={"id": assistant_id}, deep=True)

    normalized_tool_id = None
    tool_message_id = None
    if with_tools:
        assert first_response.message.tool_calls and len(first_response.message.tool_calls) == 1
        normalized_tool_id = first_response.message.tool_calls[0].id
        tool_message = await save_message(
            db_session,
            SESSION_ID,
            UID,
            MessageRole.TOOL,
            MessageType.TOOL_RESULT,
            InternalMessage(role=MessageRole.TOOL, content="tool output", tool_call_id=normalized_tool_id),
            PROFILE_ID,
        )
        tool_message_id = tool_message.id
        assert tool_message_id is not None

    other_message = await save_message(
        db_session,
        other_session_id,
        other_uid,
        MessageRole.ASSISTANT,
        MessageType.TEXT,
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="Other",
            reasoning_content="Other reasoning",
            provider_metadata={"source": initial_source},
        ),
        PROFILE_ID,
    )
    assert other_message.id is not None

    db_session.expire_all()
    stored_assistant = await db_session.get(Message, assistant_id)
    assert stored_assistant is not None
    assistant_content = stored_assistant.content
    stored_tool = await db_session.get(Message, tool_message_id) if tool_message_id is not None else None
    if with_tools:
        assert stored_tool is not None
    tool_content = stored_tool.content if stored_tool is not None else None

    history = await ContextManager.get_messages(
        db_session,
        SESSION_ID,
        UID,
        profile=Profile(id=PROFILE_ID, uid=UID, name="round-trip", configs={}),
        current_message="Follow-up",
    )
    history_assistant = next(message for message in history if message.role == MessageRole.ASSISTANT)
    assert history_assistant.reasoning_content == reasoning
    assert history_assistant.provider_metadata["source"] == initial_source
    if with_tools:
        assert history_assistant.tool_calls and history_assistant.tool_calls[0].id == normalized_tool_id
        assert history_assistant.tool_calls[0].arguments == TOOL_ARGUMENTS
        history_tool = next(message for message in history if message.role == MessageRole.TOOL)
        assert history_tool.tool_call_id == normalized_tool_id
        assert history_tool.content == "tool output"

    await discard_mismatched_provider_state(
        db_session,
        session_id=SESSION_ID,
        uid=UID,
        messages=history,
        channel_id=1,
        model_id=MODEL_ID,
        protocol=protocol,
    )
    await db_session.commit()
    db_session.expire_all()
    same_source_assistant = await db_session.get(Message, assistant_id)
    assert same_source_assistant is not None
    assert same_source_assistant.reasoning_content == reasoning
    assert same_source_assistant.provider_metadata["source"] == initial_source

    if protocol == "openai":
        same_source_payload = OpenAIChatCompletionsTransformer.to_provider(history, model_id=MODEL_ID, channel_id=1)
        same_source_assistant_item = next(item for item in same_source_payload if item.get("role") == "assistant")
        assert same_source_assistant_item["reasoning_content"] == reasoning
        if with_tools:
            same_source_tool_call = same_source_assistant_item["tool_calls"][0]
            assert same_source_tool_call["vendor_trace"] == "chat-trace"
            assert same_source_tool_call["function"]["vendor_function"] == "chat-function"
    else:
        same_source_payload = OpenAIResponsesTransformer.to_provider(history, model_id=MODEL_ID, channel_id=1)
        same_source_reasoning = next(item for item in same_source_payload if item.get("type") == "reasoning")
        assert same_source_reasoning["encrypted_content"] == "sealed-state"
        assert same_source_reasoning["summary"][0]["text"] == reasoning

    new_channel_id = 2 if change == "channel" else 1
    new_model_id = "round-trip-model-new" if change == "model" else MODEL_ID
    new_protocol = ("openai_responses" if protocol == "openai" else "openai") if change == "protocol" else protocol
    await discard_mismatched_provider_state(
        db_session,
        session_id=SESSION_ID,
        uid=UID,
        messages=history,
        channel_id=new_channel_id,
        model_id=new_model_id,
        protocol=new_protocol,
    )
    await db_session.commit()

    db_session.expire_all()
    persisted_assistant = await db_session.get(Message, assistant_id)
    assert persisted_assistant is not None
    assert persisted_assistant.reasoning_content is None
    assert persisted_assistant.provider_metadata == {}
    assert persisted_assistant.content == assistant_content
    persisted_tool = await db_session.get(Message, tool_message_id) if tool_message_id is not None else None
    if with_tools:
        assert persisted_tool is not None
        assert persisted_tool.content == tool_content

    reloaded_history = await ContextManager.get_messages(
        db_session,
        SESSION_ID,
        UID,
        profile=Profile(id=PROFILE_ID, uid=UID, name="round-trip", configs={}),
        current_message="Follow-up",
    )
    reloaded_assistant = next(message for message in reloaded_history if message.role == MessageRole.ASSISTANT)
    assert reloaded_assistant.reasoning_content is None
    assert reloaded_assistant.provider_metadata == {}
    if with_tools:
        assert reloaded_assistant.content is None
        assert reloaded_assistant.tool_calls and reloaded_assistant.tool_calls[0].id == normalized_tool_id
        assert reloaded_assistant.tool_calls[0].arguments == TOOL_ARGUMENTS
        reloaded_tool = next(message for message in reloaded_history if message.role == MessageRole.TOOL)
        assert reloaded_tool.tool_call_id == normalized_tool_id
        assert reloaded_tool.content == "tool output"
    else:
        assert reloaded_assistant.content == "Answer"

    user_row = await db_session.get(Message, user_message.id)
    assert user_row is not None
    assert user_row.content == "Question"
    other_row = await db_session.get(Message, other_message.id)
    assert other_row is not None
    assert other_row.content == "Other"
    assert other_row.reasoning_content == "Other reasoning"
    assert other_row.provider_metadata == {"source": initial_source}

    if new_protocol == "openai":
        target_payload = OpenAIChatCompletionsTransformer.to_provider(reloaded_history, model_id=new_model_id, channel_id=new_channel_id)
    else:
        target_payload = OpenAIResponsesTransformer.to_provider(reloaded_history, model_id=new_model_id, channel_id=new_channel_id)
    serialized_target_payload = json.dumps(target_payload, ensure_ascii=False)
    for stale_value in (reasoning, "sealed-state", "chat-trace", "chat-function", "responses-trace"):
        assert stale_value not in serialized_target_payload

    target_user = next(item for item in target_payload if item.get("role") == "user")
    assert target_user["content"] == "Question"
    if with_tools:
        if new_protocol == "openai":
            target_assistant = next(item for item in target_payload if item.get("role") == "assistant")
            assert target_assistant["content"] == "[tool_call]"
            target_tool_call = target_assistant["tool_calls"][0]
            assert target_tool_call["id"] == normalized_tool_id
            assert target_tool_call["function"]["name"] == TOOL_NAME
            assert json.loads(target_tool_call["function"]["arguments"]) == TOOL_ARGUMENTS
            assert "vendor_trace" not in target_tool_call
            assert "vendor_function" not in target_tool_call["function"]
            target_tool = next(item for item in target_payload if item.get("role") == "tool")
            assert target_tool["tool_call_id"] == normalized_tool_id
            assert target_tool["content"] == "tool output"
        else:
            target_function_call = next(item for item in target_payload if item.get("type") == "function_call")
            assert target_function_call["call_id"] == normalized_tool_id
            assert target_function_call["name"] == TOOL_NAME
            assert json.loads(target_function_call["arguments"]) == TOOL_ARGUMENTS
            assert "vendor_trace" not in target_function_call
            target_output = next(item for item in target_payload if item.get("type") == "function_call_output")
            assert target_output["call_id"] == normalized_tool_id
            assert target_output["output"] == "tool output"
    else:
        target_assistant = next(item for item in target_payload if item.get("role") == "assistant")
        assert target_assistant["content"] == "Answer"
        if new_protocol == "openai":
            assert "reasoning_content" not in target_assistant
        else:
            assert not any(item.get("type") == "reasoning" for item in target_payload)

    db_session.expire_all()
    cutback_history = await ContextManager.get_messages(
        db_session,
        SESSION_ID,
        UID,
        profile=Profile(id=PROFILE_ID, uid=UID, name="round-trip", configs={}),
        current_message="Follow-up",
    )
    await discard_mismatched_provider_state(
        db_session,
        session_id=SESSION_ID,
        uid=UID,
        messages=cutback_history,
        channel_id=1,
        model_id=MODEL_ID,
        protocol=protocol,
    )
    await db_session.commit()
    cutback_assistant = next(message for message in cutback_history if message.role == MessageRole.ASSISTANT)
    assert cutback_assistant.reasoning_content is None
    assert cutback_assistant.provider_metadata == {}
    if with_tools:
        assert cutback_assistant.tool_calls and cutback_assistant.tool_calls[0].provider_metadata is None

    await discard_mismatched_provider_state(
        db_session,
        session_id=SESSION_ID,
        uid=UID,
        messages=[old_assistant_snapshot],
        channel_id=1,
        model_id=MODEL_ID,
        protocol=protocol,
    )
    await db_session.commit()
    assert old_assistant_snapshot.reasoning_content is None
    assert old_assistant_snapshot.provider_metadata == {}
    if with_tools:
        assert old_assistant_snapshot.tool_calls and old_assistant_snapshot.tool_calls[0].provider_metadata is None


@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("mode", ["background", "non_stream", "stream"])
@pytest.mark.asyncio
async def test_real_channel_fallback_discards_persisted_provider_state(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    mode: str,
) -> None:
    reasoning = "Think carefully."
    profile = Profile(id=PROFILE_ID, uid=UID, name="round-trip", configs={})
    model_entry = {
        "model_id": MODEL_ID,
        "usage": "CHAT",
        "protocol": protocol.upper(),
        "context_window_k": 64,
        "max_tokens": 512,
        "temperature": 0.0,
        "top_p": 1.0,
        "reasoning_effort": None,
    }
    channel_one = SimpleNamespace(
        id=1,
        name="channel-one",
        api_key=API_KEY,
        base_url=BASE_URL,
        http_proxy=None,
        get_decrypted_api_key=lambda: API_KEY,
    )
    channel_two = SimpleNamespace(
        id=2,
        name="channel-two",
        api_key=API_KEY,
        base_url=BASE_URL,
        http_proxy=None,
        get_decrypted_api_key=lambda: API_KEY,
    )
    rule_one = SimpleNamespace(priority=1)
    rule_two = SimpleNamespace(priority=2)
    chat_channel = SimpleNamespace(chat_timeout=60.0, rules=[rule_one, rule_two])
    selection_calls: list[set[int] | None] = []

    async def fake_select(_db, _chat_channel, _expected_usage, **kwargs):
        excluded_priorities = kwargs.get("excluded_priorities")
        selection_calls.append(excluded_priorities)
        if excluded_priorities:
            return channel_two, model_entry, rule_two
        return channel_one, model_entry, rule_one

    monkeypatch.setattr(channel_call, "select_channel", fake_select)
    monkeypatch.setattr(interactive_generation_module, "select_channel", fake_select)

    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    await db_session.commit()
    user_message = await save_message(
        db_session,
        SESSION_ID,
        UID,
        MessageRole.USER,
        MessageType.TEXT,
        InternalMessage(role=MessageRole.USER, content="Question"),
        PROFILE_ID,
    )
    raw_response = _chat_response(reasoning, False) if protocol == "openai" else _responses_response(reasoning, False)
    raw_response["model"] = "provider-real-model"

    async def setup_post(_self: BaseOpenAITransformer, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return deepcopy(raw_response)

    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", setup_post)
    initial_response = await LLMClient.generate(
        api_key=API_KEY,
        base_url=BASE_URL,
        model_id=MODEL_ID,
        channel_id=1,
        messages=[user_message],
        protocol=protocol,
    )
    assert initial_response.message.reasoning_content == reasoning
    assert initial_response.message.provider_metadata["source"] == {"channel_id": 1, "model_id": MODEL_ID, "protocol": protocol}
    saved_assistant = await save_assistant_message(db_session, SESSION_ID, UID, PROFILE_ID, initial_response.message)
    assistant_id = saved_assistant.id
    assert assistant_id is not None

    captured_payloads: list[dict[str, Any]] = []

    async def fallback_post(_self: BaseOpenAITransformer, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured_payloads.append(deepcopy(payload))
        if len(captured_payloads) == 1:
            raise LLMException(message="channel one failed")
        return deepcopy(raw_response)

    if mode == "stream":
        stream_events = _chat_stream_events(raw_response) if protocol == "openai" else _responses_stream_events(raw_response)

        async def unexpected_post(_self: BaseOpenAITransformer, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("stream fallback must use the SSE network path")

        async def fallback_stream(_self: BaseOpenAITransformer, *, payload: dict[str, Any], normalize_event, **_kwargs: Any):
            captured_payloads.append(deepcopy(payload))
            if len(captured_payloads) == 1:
                raise LLMException(message="channel one failed")
            for raw_event in stream_events:
                normalized, _ = normalize_event(deepcopy(raw_event))
                if normalized is not None:
                    yield normalized

        monkeypatch.setattr(BaseOpenAITransformer, "_post_json", unexpected_post)
        monkeypatch.setattr(BaseOpenAITransformer, "_stream_sse_json", fallback_stream)
    else:
        monkeypatch.setattr(BaseOpenAITransformer, "_post_json", fallback_post)

    async def request_builder(_chat_params, _channel_obj, _model_entry):
        return await ContextManager.get_messages(
            db_session,
            SESSION_ID,
            UID,
            profile=profile,
            current_message="Follow-up",
        )

    if mode == "background":
        response, selected_channel, selected_model, selected_rule, _chat_params = await channel_call.generate_chat_with_fallback(
            db_session,
            chat_channel=chat_channel,
            request_builder=request_builder,
            call_context="test_channel_fallback",
            cursor_key=None,
            uid=UID,
            session_id=SESSION_ID,
        )
        assert selected_channel.id == 2
        assert selected_model["model_id"] == MODEL_ID
        assert selected_rule.priority == 2
        assert selection_calls == [None, {1}]
    else:
        history = await request_builder({}, channel_one, model_entry)
        stream_events_received: list[dict[str, Any]] = []

        async def stream_event_callback(event: dict[str, Any]) -> None:
            stream_events_received.append(deepcopy(event))

        state = SimpleNamespace(
            db=db_session,
            uid=UID,
            session_id=SESSION_ID,
            profile=profile,
            cfg=SimpleNamespace(),
            messages=history,
            checkpoint_state=SimpleNamespace(
                upper_message_id=None,
                total_output_tokens=0,
                session_total_input_tokens=0,
                session_total_cached_tokens=0,
                session_total_output_tokens=0,
                execution_phase=GOAL_EXECUTION_PHASE_RUNNING,
            ),
            context_summary_work_validity_checker=None,
            context_summary_lifecycle_callback=None,
            chat_params={
                "temperature": 0.0,
                "top_p": 1.0,
                "reasoning_effort": None,
                "max_tokens": 512,
                "chat_timeout": 60.0,
                "context_window_k": 64,
            },
            model_entry=model_entry,
            chat_channel=chat_channel,
            chat_cursor_key="round-trip:CHAT",
            chat_channel_obj=channel_one,
            channel_rule=rule_one,
            latest_llm_request_metadata=None,
            current_turn=1,
            goal_mode=False,
            stream_event_callback=stream_event_callback if mode == "stream" else None,
            request_metadata_callback=None,
            expose_tool_call_content=True,
            show_tool_calls=True,
            dispatcher_mode=mode,
            dispatch_logger=SimpleNamespace(bind=lambda **_kwargs: SimpleNamespace(warning=lambda *_args, **_inner: None)),
            img_understanding=False,
            audio_understanding=False,
            video_understanding=False,
            initial_msg=user_message,
        )
        generation_result = await interactive_generation_module.generate_interactive_turn(
            state,
            current_tools=[],
            response_id="fallback-response",
        )
        response = generation_result
        assert state.chat_channel_obj.id == 2
        assert state.model_entry["model_id"] == MODEL_ID
        assert state.channel_rule.priority == 2
        assert selection_calls == [{1}]
        if mode == "stream":
            assert any(event.get("type") == "content" and event.get("content") == "Answer" for event in stream_events_received)

    final_message = response.message
    if mode == "background":
        assert response.model == "provider-real-model"
    assert final_message.content == "Answer"
    assert final_message.provider_metadata["source"] == {"channel_id": 2, "model_id": MODEL_ID, "protocol": protocol}

    assert len(captured_payloads) == 2
    first_payload, second_payload = captured_payloads
    assert first_payload["model"] == MODEL_ID
    assert second_payload["model"] == MODEL_ID
    if protocol == "openai":
        first_assistant_payload = next(item for item in first_payload["messages"] if item.get("role") == "assistant")
        second_assistant_payload = next(item for item in second_payload["messages"] if item.get("role") == "assistant")
        assert first_assistant_payload["reasoning_content"] == reasoning
        assert "reasoning_content" not in second_assistant_payload
    else:
        first_reasoning_item = next(item for item in first_payload["input"] if item.get("type") == "reasoning")
        assert first_reasoning_item["encrypted_content"] == "sealed-state"
        assert not any(item.get("type") == "reasoning" for item in second_payload["input"])

    db_session.expire_all()
    stored_assistant = await db_session.get(Message, assistant_id)
    assert stored_assistant is not None
    assert stored_assistant.content == "Answer"
    assert stored_assistant.reasoning_content is None
    assert stored_assistant.provider_metadata == {}


@pytest.mark.parametrize("old_assistant_count", [20, 600])
@pytest.mark.asyncio
async def test_summary_boundary_provider_state_cleanup_ignores_summarized_history(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    old_assistant_count: int,
) -> None:
    session_id = f"{SESSION_ID}-summary-boundary-{old_assistant_count}"
    uid = f"{UID}-summary-boundary-{old_assistant_count}"
    source = {"channel_id": 1, "model_id": MODEL_ID, "protocol": "openai_responses"}
    old_marker = f"summary-boundary-old-{old_assistant_count}"
    active_marker = f"summary-boundary-active-{old_assistant_count}"
    old_encrypted_content = old_marker + ("x" * (16 * 1024 - len(old_marker)))
    old_reasoning = "summarized historical reasoning"
    active_reasoning = "active reasoning"
    active_content = "active answer"

    dialect = db_session.bind.sync_engine.dialect
    original_json_deserializer = dialect._json_deserializer or json.loads
    decoded_old_markers = 0
    decoded_active_markers = 0

    def tracking_json_deserializer(value: Any) -> Any:
        nonlocal decoded_active_markers, decoded_old_markers
        decoded = original_json_deserializer(value)
        if isinstance(decoded, dict) and decoded.get("test_marker") == old_marker:
            decoded_old_markers += 1
        elif isinstance(decoded, dict) and decoded.get("test_marker") == active_marker:
            decoded_active_markers += 1
        return decoded

    monkeypatch.setattr(dialect, "_json_deserializer", tracking_json_deserializer)

    chat_session = ChatSession(session_id=session_id, uid=uid, profile_id=PROFILE_ID)
    db_session.add(chat_session)
    await db_session.flush()

    old_messages: list[Message] = []
    for index in range(old_assistant_count):
        old_messages.append(
            Message(
                session_id=session_id,
                uid=uid,
                role=MessageRole.ASSISTANT,
                type=MessageType.TEXT,
                content=f"historical answer {index}",
                reasoning_content=old_reasoning,
                provider_metadata={
                    "source": source,
                    "protocol": "openai_responses",
                    "output": [{"type": "reasoning", "encrypted_content": old_encrypted_content, "summary": []}],
                    "test_marker": old_marker,
                },
                profile_id=PROFILE_ID,
                is_processed=True,
            )
        )
    db_session.add_all(old_messages)
    await db_session.flush()
    summary_boundary_id = old_messages[-1].id
    old_snapshots = {message.id: (message.content, message.reasoning_content, deepcopy(message.provider_metadata)) for message in old_messages}
    chat_session.context_summary = "Summary of historical messages"
    chat_session.context_summary_message_id = summary_boundary_id
    chat_session.context_summary_revision = 2
    await db_session.commit()

    active_message = Message(
        session_id=session_id,
        uid=uid,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content=active_content,
        reasoning_content=active_reasoning,
        provider_metadata={
            "source": source,
            "protocol": "openai_responses",
            "output": [{"type": "reasoning", "encrypted_content": "active-encrypted-state", "summary": []}],
            "test_marker": active_marker,
        },
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    db_session.add(active_message)
    await db_session.commit()
    active_message_id = active_message.id

    profile = Profile(id=PROFILE_ID, uid=uid, name="summary-boundary", configs={})
    active_messages = await ContextManager.get_messages(
        db_session,
        session_id,
        uid,
        profile=profile,
        after_id=summary_boundary_id,
        current_message="Follow-up",
    )
    active_assistants = [message for message in active_messages if message.role == MessageRole.ASSISTANT]
    assert [message.id for message in active_assistants] == [active_message_id]
    decoded_old_markers = 0
    decoded_active_markers = 0

    async def discard_for_channel(channel_id: int) -> None:
        nonlocal decoded_active_markers, decoded_old_markers
        decoded_old_markers = 0
        decoded_active_markers = 0
        await discard_mismatched_provider_state(
            db_session,
            session_id=session_id,
            uid=uid,
            messages=active_messages,
            channel_id=channel_id,
            model_id=MODEL_ID,
            protocol="openai_responses",
        )
        await db_session.commit()
        assert decoded_old_markers == 0
        assert decoded_active_markers <= 1

    await discard_for_channel(1)
    db_session.expire_all()
    same_source = await db_session.get(Message, active_message_id)
    assert same_source is not None
    assert same_source.content == active_content
    assert same_source.reasoning_content == active_reasoning
    assert same_source.provider_metadata == {
        "source": source,
        "protocol": "openai_responses",
        "output": [{"type": "reasoning", "encrypted_content": "active-encrypted-state", "summary": []}],
        "test_marker": active_marker,
    }

    await discard_for_channel(2)
    db_session.expire_all()
    switched_source = await db_session.get(Message, active_message_id)
    assert switched_source is not None
    assert switched_source.content == active_content
    assert switched_source.reasoning_content is None
    assert switched_source.provider_metadata == {}

    await discard_for_channel(1)
    db_session.expire_all()
    switched_back = await db_session.get(Message, active_message_id)
    assert switched_back is not None
    assert switched_back.content == active_content
    assert switched_back.reasoning_content is None
    assert switched_back.provider_metadata == {}

    monkeypatch.setattr(dialect, "_json_deserializer", original_json_deserializer)
    db_session.expire_all()
    reloaded_old_messages = (await db_session.execute(select(Message).where(Message.id.in_(old_snapshots)).order_by(Message.id))).scalars().all()
    assert len(reloaded_old_messages) == old_assistant_count
    for reloaded_message in reloaded_old_messages:
        expected_content, expected_reasoning, expected_metadata = old_snapshots[reloaded_message.id]
        assert reloaded_message.content == expected_content
        assert reloaded_message.reasoning_content == expected_reasoning
        assert reloaded_message.provider_metadata == expected_metadata


@pytest.mark.asyncio
async def test_large_duplicate_provider_state_cleanup_batches_without_message_loss(
    db_session: AsyncSession,
) -> None:
    session_id = f"{SESSION_ID}-large-duplicate"
    uid = f"{UID}-large-duplicate"
    source = {"channel_id": 1, "model_id": MODEL_ID, "protocol": "openai"}
    active_count = 1001
    db_session.add(ChatSession(session_id=session_id, uid=uid, profile_id=PROFILE_ID))

    active_rows = [
        Message(
            session_id=session_id,
            uid=uid,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content=f"unique answer {index}",
            reasoning_content=f"reasoning {index}",
            provider_metadata={"source": source},
            profile_id=PROFILE_ID,
            is_processed=True,
        )
        for index in range(active_count)
    ]
    excluded_row = Message(
        session_id=session_id,
        uid=uid,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="excluded answer",
        reasoning_content="excluded reasoning",
        provider_metadata={"source": source},
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    db_session.add_all([*active_rows, excluded_row])
    await db_session.flush()
    active_ids = [message.id for message in active_rows]
    excluded_id = excluded_row.id
    await db_session.commit()

    active = parse_db_messages_to_internal(active_rows)
    active_id_set = set(active_ids)
    assert [message.id for message in active] == active_ids
    assert len(active_id_set) == active_count
    assert excluded_id not in active_id_set

    read_batches: list[list[int]] = []
    write_batches: list[list[int]] = []

    def collect_provider_state_ids(
        _connection: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        tokens = statement.lstrip().split(None, 1)
        if not tokens or tokens[0].upper() not in {"SELECT", "UPDATE"}:
            return
        values = parameters.values() if isinstance(parameters, dict) else parameters or ()
        ids = [value for value in values if type(value) is int]
        if tokens[0].upper() == "SELECT":
            read_batches.append(ids)
        else:
            write_batches.append(ids)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", collect_provider_state_ids)
    try:
        await discard_mismatched_provider_state(
            db_session,
            session_id=session_id,
            uid=uid,
            messages=[*active, *active],
            channel_id=2,
            model_id=MODEL_ID,
            protocol="openai",
        )
        await db_session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", collect_provider_state_ids)

    observed_reads = [message_id for batch in read_batches for message_id in batch]
    observed_writes = [message_id for batch in write_batches for message_id in batch]
    assert all(len(batch) <= 500 for batch in [*read_batches, *write_batches])
    assert set(observed_reads) == active_id_set
    assert set(observed_writes) == active_id_set
    assert len(observed_reads) == len(set(observed_reads))

    db_session.expire_all()
    reloaded_active = (await db_session.execute(select(Message).where(Message.id.in_(active_ids)).order_by(Message.id))).scalars().all()
    assert len(reloaded_active) == active_count
    assert [message.content for message in reloaded_active] == [f"unique answer {index}" for index in range(active_count)]
    assert all(message.reasoning_content is None for message in reloaded_active)
    assert all(message.provider_metadata == {} for message in reloaded_active)

    reloaded_excluded = await db_session.get(Message, excluded_id)
    assert reloaded_excluded is not None
    assert reloaded_excluded.content == "excluded answer"
    assert reloaded_excluded.reasoning_content == "excluded reasoning"
    assert reloaded_excluded.provider_metadata == {"source": source}
    assert all(message.content == f"unique answer {index}" for index, message in enumerate(active))
    assert all(message.reasoning_content is None for message in active)
    assert all(message.provider_metadata == {} for message in active)


@pytest.mark.parametrize(
    "reasoning_content",
    [None, "", "x" * 65536],
    ids=["none", "empty", "large"],
)
@pytest.mark.asyncio
async def test_get_provider_states_returns_owned_reasoning_presence(
    db_session: AsyncSession,
    reasoning_content: str | None,
) -> None:
    session_id = f"{SESSION_ID}-provider-states"
    other_session_id = f"{session_id}-other"
    uid = f"{UID}-provider-states"
    wrong_uid = f"{uid}-wrong"
    db_session.add_all(
        [
            ChatSession(session_id=session_id, uid=uid, profile_id=PROFILE_ID),
            ChatSession(session_id=other_session_id, uid=uid, profile_id=PROFILE_ID),
        ]
    )
    target_assistant = Message(
        session_id=session_id,
        uid=uid,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="target",
        reasoning_content=reasoning_content,
        provider_metadata=None,
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    unrequested_assistant = Message(
        session_id=session_id,
        uid=uid,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="unrequested",
        reasoning_content="excluded reasoning",
        provider_metadata={"excluded": True},
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    user_message = Message(
        session_id=session_id,
        uid=uid,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="user",
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    other_assistant = Message(
        session_id=other_session_id,
        uid=uid,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content="other",
        reasoning_content="other reasoning",
        provider_metadata={"other": True},
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    db_session.add_all([target_assistant, unrequested_assistant, user_message, other_assistant])
    await db_session.commit()

    message_ids = [target_assistant.id, user_message.id, other_assistant.id]
    provider_states = await message_crud.get_provider_states(
        db_session,
        session_id=session_id,
        uid=uid,
        message_ids=message_ids,
    )
    assert provider_states == [(target_assistant.id, None, reasoning_content is not None)]
    assert provider_states[0][2] is (reasoning_content is not None)

    wrong_uid_states = await message_crud.get_provider_states(
        db_session,
        session_id=session_id,
        uid=wrong_uid,
        message_ids=message_ids,
    )
    assert wrong_uid_states == []


@pytest.mark.asyncio
async def test_virtual_provider_state_cleanup_does_not_scan_history_without_persisted_assistant_id(
    db_session: AsyncSession,
) -> None:
    source = {"channel_id": 1, "model_id": MODEL_ID, "protocol": "openai"}
    historical_content = "historical answer"
    historical_reasoning = "historical reasoning"
    historical_metadata = {"source": source, "trace": "historical"}
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
    historical_assistant = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.ASSISTANT,
        type=MessageType.TEXT,
        content=historical_content,
        reasoning_content=historical_reasoning,
        provider_metadata=historical_metadata,
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    user_message = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="current question",
        profile_id=PROFILE_ID,
        is_processed=True,
    )
    db_session.add_all([historical_assistant, user_message])
    await db_session.commit()
    historical_assistant_id = historical_assistant.id
    stored_user = await db_session.get(Message, user_message.id)
    assert stored_user is not None
    parsed_user = parse_db_messages_to_internal([stored_user])[0]

    virtual_assistant = InternalMessage(
        id=None,
        role=MessageRole.ASSISTANT,
        content="virtual answer",
        reasoning_content="virtual reasoning",
        provider_metadata={"source": source},
        tool_calls=[
            InternalToolCall(
                id="virtual-call",
                name=TOOL_NAME,
                arguments=TOOL_ARGUMENTS,
                provider_metadata={"trace": "virtual"},
            )
        ],
    )
    observed_sql: list[str] = []

    def record_sql(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        observed_sql.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        provider_states = await message_crud.get_provider_states(
            db_session,
            session_id=SESSION_ID,
            uid=UID,
            message_ids=[],
        )
        assert provider_states == []
        await discard_mismatched_provider_state(
            db_session,
            session_id=SESSION_ID,
            uid=UID,
            messages=[parsed_user, virtual_assistant],
            channel_id=2,
            model_id=MODEL_ID,
            protocol="openai",
        )
        await db_session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert observed_sql == []
    assert virtual_assistant.content == "virtual answer"
    assert virtual_assistant.reasoning_content is None
    assert virtual_assistant.provider_metadata == {}
    assert virtual_assistant.tool_calls and virtual_assistant.tool_calls[0].arguments == TOOL_ARGUMENTS
    assert virtual_assistant.tool_calls[0].provider_metadata is None

    db_session.expire_all()
    reloaded_historical = await db_session.get(Message, historical_assistant_id)
    assert reloaded_historical is not None
    assert reloaded_historical.content == historical_content
    assert reloaded_historical.reasoning_content == historical_reasoning
    assert reloaded_historical.provider_metadata == historical_metadata


def _reasoning_effort_model_entry(
    protocol: str,
    reasoning_effort: str | None,
    *,
    reasoning_efforts: list[str],
) -> dict[str, Any]:
    return {
        "model_id": MODEL_ID,
        "usage": "CHAT",
        "protocol": protocol.upper(),
        "context_window_k": 64,
        "max_tokens": 128,
        "temperature": 0.25,
        "top_p": 0.75,
        "reasoning_effort": reasoning_effort,
        "reasoning_efforts": reasoning_efforts,
    }


def _build_reasoning_effort_state(
    *,
    db_session: AsyncSession,
    uid: str,
    session_id: str,
    profile: Profile,
    messages: list[InternalMessage],
    initial_message: InternalMessage,
    model_entry: dict[str, Any],
    channel: Any,
    channel_rule: Any,
    chat_channel: Any,
    mode: str,
) -> tuple[SimpleNamespace, list[dict[str, Any]]]:
    stream_events: list[dict[str, Any]] = []

    async def stream_event_callback(event: dict[str, Any]) -> None:
        stream_events.append(deepcopy(event))

    state = SimpleNamespace(
        db=db_session,
        uid=uid,
        session_id=session_id,
        profile=profile,
        cfg=SimpleNamespace(),
        messages=messages,
        checkpoint_state=SimpleNamespace(
            upper_message_id=None,
            total_output_tokens=0,
            session_total_input_tokens=0,
            session_total_cached_tokens=0,
            session_total_output_tokens=0,
            execution_phase=GOAL_EXECUTION_PHASE_RUNNING,
        ),
        context_summary_work_validity_checker=None,
        context_summary_lifecycle_callback=None,
        chat_params={
            "temperature": model_entry["temperature"],
            "top_p": model_entry["top_p"],
            "reasoning_effort": model_entry["reasoning_effort"],
            "max_tokens": model_entry["max_tokens"],
            "chat_timeout": 60.0,
            "context_window_k": model_entry["context_window_k"],
        },
        model_entry=model_entry,
        chat_channel=chat_channel,
        chat_cursor_key=f"{session_id}:CHAT",
        chat_channel_obj=channel,
        channel_rule=channel_rule,
        latest_llm_request_metadata=None,
        current_turn=1,
        goal_mode=False,
        stream_event_callback=stream_event_callback if mode == "stream" else None,
        request_metadata_callback=None,
        expose_tool_call_content=True,
        show_tool_calls=True,
        dispatcher_mode=mode,
        dispatch_logger=SimpleNamespace(
            bind=lambda **_bind_kwargs: SimpleNamespace(
                warning=lambda *_warning_args, **_warning_kwargs: None,
            ),
        ),
        img_understanding=False,
        audio_understanding=False,
        video_understanding=False,
        initial_msg=initial_message,
    )
    return state, stream_events


def _assert_reasoning_effort_payload(
    payload: dict[str, Any],
    *,
    protocol: str,
    mode: str,
    expected_effort: str | None,
) -> None:
    assert payload["model"] == MODEL_ID
    assert payload["stream"] is (mode == "stream")
    if expected_effort is None:
        assert payload["temperature"] == 0.25
        assert payload["top_p"] == 0.75
        if protocol == "openai":
            assert "reasoning_effort" not in payload
        else:
            assert "reasoning" not in payload
        return

    assert "temperature" not in payload
    assert "top_p" not in payload
    if protocol == "openai":
        assert payload["reasoning_effort"] == expected_effort
    else:
        expected_reasoning = {"effort": expected_effort}
        if expected_effort != "none":
            expected_reasoning["summary"] = "auto"
        assert payload["reasoning"] == expected_reasoning


@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("mode", ["non_stream", "stream", "background"])
@pytest.mark.asyncio
async def test_reasoning_effort_reaches_openai_protocol_payloads(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    mode: str,
) -> None:
    session_id = f"{SESSION_ID}-effort-{protocol}-{mode}"
    uid = f"{UID}-effort-{protocol}-{mode}"
    profile = Profile(id=PROFILE_ID, uid=uid, name="reasoning-effort", configs={})
    db_session.add(
        ChatSession(
            session_id=session_id,
            uid=uid,
            profile_id=PROFILE_ID,
            reasoning_effort="custom-tier",
        )
    )
    await db_session.commit()
    user_message = await save_message(
        db_session,
        session_id,
        uid,
        MessageRole.USER,
        MessageType.TEXT,
        InternalMessage(role=MessageRole.USER, content="Question"),
        PROFILE_ID,
    )

    channel = SimpleNamespace(
        id=1,
        name="reasoning-channel",
        api_key=API_KEY,
        base_url=BASE_URL,
        http_proxy=None,
        get_decrypted_api_key=lambda: API_KEY,
    )
    channel_rule = SimpleNamespace(priority=1)
    chat_channel = SimpleNamespace(chat_timeout=60.0, rules=[channel_rule])
    selected_model_entry: dict[str, Any] = {}

    if mode == "background":

        async def fake_select_channel(_db: AsyncSession, _chat_channel: Any, _expected_usage: str, **_kwargs: Any):
            return channel, deepcopy(selected_model_entry), channel_rule

        monkeypatch.setattr(channel_call, "select_channel", fake_select_channel)

    async def request_builder(_chat_params: dict[str, Any], _channel_obj: Any, _model_entry: dict[str, Any]) -> list[InternalMessage]:
        return await ContextManager.get_messages(
            db_session,
            session_id,
            uid,
            profile=profile,
            current_message="Follow-up",
        )

    raw_response = _chat_response("", False) if protocol == "openai" else _responses_response("", False)
    raw_response["model"] = "provider-real-model"
    stream_events = _chat_stream_events(raw_response) if protocol == "openai" else _responses_stream_events(raw_response)
    captured_payloads: list[dict[str, Any]] = []

    async def fake_post(_self: BaseOpenAITransformer, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured_payloads.append(deepcopy(payload))
        return deepcopy(raw_response)

    async def fake_stream(_self: BaseOpenAITransformer, *, payload: dict[str, Any], normalize_event, **_kwargs: Any):
        captured_payloads.append(deepcopy(payload))
        for raw_event in stream_events:
            normalized, _ = normalize_event(deepcopy(raw_event))
            if normalized is not None:
                yield normalized

    monkeypatch.setattr(BaseOpenAITransformer, "_post_json", fake_post)
    monkeypatch.setattr(BaseOpenAITransformer, "_stream_sse_json", fake_stream)

    model_entries = [
        _reasoning_effort_model_entry(protocol, "model-default", reasoning_efforts=["model-default", "high"]),
        _reasoning_effort_model_entry(protocol, "rule-default", reasoning_efforts=["rule-default", "high"]),
        _reasoning_effort_model_entry(protocol, None, reasoning_efforts=["low", "high"]),
        _reasoning_effort_model_entry(protocol, "none", reasoning_efforts=["none", "high"]),
    ]
    expected_efforts = ["custom-tier", "rule-default", None, "none"]

    for index, (model_entry, expected_effort) in enumerate(zip(model_entries, expected_efforts, strict=True)):
        if index == 1:
            session = await db_session.get(ChatSession, session_id)
            assert session is not None
            session.reasoning_effort = None
            await db_session.commit()
            db_session.expire_all()
            persisted_session = await db_session.get(ChatSession, session_id)
            assert persisted_session is not None
            assert persisted_session.reasoning_effort is None

        if mode == "background":
            selected_model_entry = deepcopy(model_entry)
            response, _, _, _, chat_params = await channel_call.generate_chat_with_fallback(
                db_session,
                chat_channel=chat_channel,
                request_builder=request_builder,
                call_context="test_reasoning_effort",
                cursor_key=None,
                uid=uid,
                session_id=session_id,
            )
            assert response.message.content == "Answer"
            assert chat_params["reasoning_effort"] == expected_effort
        else:
            history = await request_builder({}, channel, model_entry)
            state, _stream_events_received = _build_reasoning_effort_state(
                db_session=db_session,
                uid=uid,
                session_id=session_id,
                profile=profile,
                messages=history,
                initial_message=user_message,
                model_entry=deepcopy(model_entry),
                channel=channel,
                channel_rule=channel_rule,
                chat_channel=chat_channel,
                mode=mode,
            )
            result = await interactive_generation_module.generate_interactive_turn(
                state,
                current_tools=[],
                response_id=f"reasoning-effort-{index}",
            )
            assert result.message.content == "Answer"

        assert len(captured_payloads) == index + 1
        _assert_reasoning_effort_payload(
            captured_payloads[-1],
            protocol=protocol,
            mode=mode,
            expected_effort=expected_effort,
        )
