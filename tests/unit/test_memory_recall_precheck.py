import json
from types import SimpleNamespace

import pytest

from app.core.constants import LOG_MEMORY_RECALL_CHANNEL_FAILED
from app.core.dispatchers.memory import persistence as persistence_module
from app.core.dispatchers.memory import recall as precheck_module
from app.core.dispatchers.memory import request as request_module
from app.core.dispatchers.memory.types import MemoryRecallContext
from app.core.exceptions import LLMException
from app.core.prompts import LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT
from app.core.tools.longterm_memory import MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME
from app.core.utils.dispatcher import provider_state
from app.models.message import InternalMessage, InternalToolCall, MessageRole


class _FakeDb:
    def __init__(self):
        self.commit_count = 0
        self.order = []

    async def commit(self):
        self.commit_count += 1
        self.order.append("commit")

    async def refresh(self, _session):
        return None


def _call(call_id="call-1", arguments=None, name=MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME):
    return InternalToolCall(
        id=call_id,
        name=name,
        arguments=arguments
        or {
            "operation": "recall",
            "query": "user context",
            "knowledge_query": "user context details",
            "top_k": 3,
        },
    )


def _assistant(
    call_id="call-1",
    arguments=None,
    content=None,
    refusal=None,
    message_id=None,
    name=MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
):
    return InternalMessage(
        id=message_id,
        role=MessageRole.ASSISTANT,
        content=content,
        refusal=refusal,
        tool_calls=[_call(call_id, arguments, name)],
    )


def _response(message):
    return SimpleNamespace(message=message, usage={})


def _context(*, messages=None, turn_messages=None, boundary=42):
    channel = SimpleNamespace(
        id=11,
        name="primary",
        base_url="https://example.invalid",
        chat_timeout=60,
        get_decrypted_api_key=lambda: "api-key",
    )
    return MemoryRecallContext(
        db=_FakeDb(),
        uid="uid-1",
        session_id="session-1",
        profile=SimpleNamespace(id=7),
        cfg=SimpleNamespace(memory=SimpleNamespace()),
        username="tester",
        messages=messages or [InternalMessage(id=1, role=MessageRole.USER, content="request")],
        turn_messages=turn_messages or [],
        current_user_boundary_message_id=boundary,
        upper_message_id=33,
        chat_channel="chat",
        chat_cursor_key="cursor",
        chat_channel_obj=channel,
        model_entry={"model_id": "model-1", "protocol": "OPENAI", "max_tokens": 128},
        channel_rule=SimpleNamespace(priority=1),
        chat_params={
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 128,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )


@pytest.mark.parametrize(
    ("content", "refusal"),
    [(None, None), ("", None), (" ", None), (None, ""), (None, " ")],
)
def test_recall_validators_accept_only_empty_text_and_one_valid_recall_call(content, refusal):
    message = _assistant(content=content, refusal=refusal)

    assert precheck_module.response_is_valid(_response(message))
    assert persistence_module.is_valid_recall_call(message)


def test_live_precheck_requires_knowledge_query_but_persisted_legacy_recall_remains_valid():
    message = _assistant(arguments={"operation": "recall", "query": "legacy context", "top_k": 3})

    assert not precheck_module.response_is_valid(_response(message))
    assert persistence_module.is_valid_recall_call(message)


@pytest.mark.parametrize(
    "message",
    [
        _assistant(content="assistant body"),
        _assistant(refusal="refused"),
        _assistant(),
        _assistant(
            arguments={
                "operation": "create",
                "content": "mutation",
                "memory_key": "k",
                "memory_type": "fact",
            }
        ),
        _assistant(arguments={"operation": "recall"}),
        _assistant(arguments={"operation": "recall", "query": "user context", "unexpected": True}),
        _assistant(name="unrelated_tool"),
        InternalMessage(role=MessageRole.USER, tool_calls=[_call()]),
    ],
)
def test_recall_validators_reject_body_refusal_multiple_tools_mutation_and_bad_args(message):
    if message.content is None and message.refusal is None and message.tool_calls:
        message.tool_calls.append(_call("call-2"))

    assert not precheck_module.response_is_valid(_response(message))
    assert not persistence_module.is_valid_recall_call(message)


@pytest.mark.parametrize("top_k", [0, 51, True, 3.5, "3"])
def test_recall_validators_reject_invalid_explicit_top_k(top_k):
    message = _assistant(arguments={"operation": "recall", "query": "user context", "top_k": top_k})

    assert not precheck_module.response_is_valid(_response(message))
    assert not persistence_module.is_valid_recall_call(message)


@pytest.mark.asyncio
async def test_precheck_assistant_dedupe_reuses_saved_call_without_model_request(monkeypatch):
    context = _context()
    assistant = _assistant(message_id=101)
    executed = []

    async def load(_context):
        return assistant, None, "assistant-key", "tool-key"

    async def save(context_arg, message, **kwargs):
        executed.append((context_arg, message, kwargs))

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("saved assistant recovery must not request a model")

    monkeypatch.setattr(precheck_module, "load_dedupe_messages", load)
    monkeypatch.setattr(precheck_module, "save_and_execute_recall", save)
    monkeypatch.setattr(precheck_module, "select_initial_channel", unexpected)
    monkeypatch.setattr(precheck_module, "generate", unexpected)

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert len(executed) == 1
    assert executed[0][1] is assistant
    assert executed[0][2]["assistant_already_saved"] is True


async def _patch_precheck_request_flow(monkeypatch, responses, saved):
    requests = []

    async def load(_context):
        return None, None, "assistant-key", "tool-key"

    async def select(_context):
        return True

    async def prepare(_context, messages, *, is_main_context):
        requests.append((list(messages), is_main_context))
        return list(messages), {"input_tokens": 1}, f"response-{len(requests)}"

    async def generate(_context, _messages, _metadata):
        return responses[len(requests) - 1]

    async def update(_context, _response):
        return None

    async def save(_context, message, **_kwargs):
        saved.append(message)

    monkeypatch.setattr(precheck_module, "load_dedupe_messages", load)
    monkeypatch.setattr(precheck_module, "select_initial_channel", select)
    monkeypatch.setattr(precheck_module, "prepare_request_messages", prepare)
    monkeypatch.setattr(precheck_module, "generate", generate)
    monkeypatch.setattr(precheck_module, "update_output_metadata", update)
    monkeypatch.setattr(precheck_module, "save_and_execute_recall", save)
    return requests


@pytest.mark.asyncio
async def test_precheck_invalid_then_valid_corrects_once_without_polluting_main_messages(monkeypatch):
    context = _context()
    saved = []
    invalid = _assistant(call_id="invalid-call", content="unexpected body", message_id=20)
    valid = _assistant(call_id="valid-call", message_id=21)
    requests = await _patch_precheck_request_flow(
        monkeypatch,
        [_response(invalid), _response(valid)],
        saved,
    )

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert len(requests) == 2
    assert [message.role for message in requests[0][0]] == [MessageRole.USER]
    assert [message.role for message in requests[1][0]] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
        MessageRole.USER,
    ]
    assert requests[1][0][1].content == "unexpected body"
    assert requests[1][0][2].tool_call_id == "invalid-call"
    assert json.loads(requests[1][0][2].content) == {"status": "ignored"}
    assert requests[1][0][3].role == MessageRole.USER
    correction = requests[1][0][3].content
    assert LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT in correction
    assert LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT.isascii()
    correction_text = correction.lower()
    assert "exactly one structured tool call" in correction_text
    assert "the operation must be recall" in correction_text
    assert "concise, normalized long-term-memory retrieval expression" in correction_text
    assert "do not copy the full user message" in correction_text
    assert "remove request actions" in correction_text
    assert "no assistant prose or refusal" in correction_text
    assert "do not call create, update, or delete" in correction_text
    assert "any other operation or tool" in correction_text
    assert [message.id for message in context.messages] == [1]
    assert [message.id for message in context.turn_messages] == []
    assert [message.tool_calls[0].id for message in saved] == ["valid-call"]


@pytest.mark.asyncio
async def test_precheck_retry_reuses_clean_request_prefix_before_appending_correction(monkeypatch):
    todo_snapshot = (
        "<current_session_todo_snapshot>"
        + json.dumps(
            {
                "revision": 7,
                "todos": [
                    {"content": "send yellow image", "status": "completed"},
                    {"content": "find other colors", "status": "pending"},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "</current_session_todo_snapshot>"
    )
    context = _context(
        messages=[
            InternalMessage(
                id=1,
                role=MessageRole.SYSTEM,
                content="ORIGINAL_PRIVATE_SYSTEM_MARKER",
            ),
            InternalMessage(
                id=2,
                role=MessageRole.USER,
                content='<conversation_summary through_message_id="1">existing summary</conversation_summary>',
            ),
            InternalMessage(
                id=3,
                role=MessageRole.USER,
                content=[
                    {"type": "text", "text": "Magic9询图"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,IMAGE_PRIVATE_MARKER"},
                    },
                ],
                environment_prompt="ENVIRONMENT_PRIVATE_MARKER",
            ),
            InternalMessage(
                id=4,
                role=MessageRole.ASSISTANT,
                content="已发黄色图片",
                reasoning_content="THOUGHT_PRIVATE_MARKER",
            ),
            InternalMessage(
                id=5,
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="history-tool",
                        name="private_history_tool",
                        arguments={"secret": "TOOL_ARGUMENT_PRIVATE_MARKER"},
                    )
                ],
            ),
            InternalMessage(
                id=6,
                role=MessageRole.TOOL,
                tool_call_id="history-tool",
                content=f"TOOL_RESULT_PRIVATE_MARKER\n\n{todo_snapshot}",
            ),
            InternalMessage(
                id=7,
                role=MessageRole.USER,
                content="多找其他颜色",
                environment_prompt="CURRENT_ENVIRONMENT_PRIVATE_MARKER",
                reasoning_content="CURRENT_THOUGHT_PRIVATE_MARKER",
            ),
        ]
    )
    original_messages = [message.model_dump(mode="json") for message in context.messages]
    original_messages[3]["reasoning_content"] = None
    original_messages[3]["provider_metadata"] = {}
    invalid = _assistant(call_id="invalid-call", content="unexpected body", message_id=20)
    valid = _assistant(call_id="valid-call", message_id=21)
    model_requests = []
    accepted = []

    async def load(_context):
        return None, None, "assistant-key", "tool-key"

    async def get_session(_db, _session_id):
        return SimpleNamespace(llm_request_metadata={})

    async def get_provider_states(*_args, **_kwargs):
        return []

    async def generate(**kwargs):
        model_requests.append([message.model_copy(deep=True) for message in kwargs["messages"]])
        return [_response(invalid), _response(valid)][len(model_requests) - 1]

    async def save(_context, message, **_kwargs):
        accepted.append(message)

    monkeypatch.setattr(precheck_module, "load_dedupe_messages", load)
    monkeypatch.setattr(provider_state.message_crud, "get_provider_states", get_provider_states)
    monkeypatch.setattr(request_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(request_module.LLMClient, "generate", generate)
    monkeypatch.setattr(precheck_module, "save_and_execute_recall", save)

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert len(model_requests) == 2
    assert len(accepted) == 1
    assert accepted[0].tool_calls[0].id == "valid-call"

    first_request = model_requests[0]
    first_dump = [message.model_dump(mode="json") for message in first_request]
    assert first_request[0].role == MessageRole.SYSTEM
    assert first_request[-1].role == MessageRole.USER
    assert first_request[-1].content == "多找其他颜色"
    assert any(message.content == "已发黄色图片" for message in first_request)
    assert any(message.role == MessageRole.USER and isinstance(message.content, str) and "Magic9询图" in message.content for message in first_request)
    assert any(message.role == MessageRole.USER and isinstance(message.content, str) and message.content.startswith("<conversation_summary ") for message in first_request)
    todo_messages = [message for message in first_request if message.role == MessageRole.USER and isinstance(message.content, str) and "current_session_todo" in message.content]
    assert len(todo_messages) == 1
    assert json.loads(todo_messages[0].content) == {
        "current_session_todo": {
            "revision": 7,
            "todos": [
                {"content": "send yellow image", "status": "completed"},
                {"content": "find other colors", "status": "pending"},
            ],
        }
    }
    assert all(message.role != MessageRole.TOOL for message in first_request)
    assert all(not message.tool_calls for message in first_request)

    first_text = json.dumps(first_dump, ensure_ascii=False)
    assert LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT not in first_text
    for private_marker in (
        "ORIGINAL_PRIVATE_SYSTEM_MARKER",
        "IMAGE_PRIVATE_MARKER",
        "TOOL_ARGUMENT_PRIVATE_MARKER",
        "TOOL_RESULT_PRIVATE_MARKER",
        "ENVIRONMENT_PRIVATE_MARKER",
        "CURRENT_ENVIRONMENT_PRIVATE_MARKER",
        "THOUGHT_PRIVATE_MARKER",
        "CURRENT_THOUGHT_PRIVATE_MARKER",
    ):
        assert private_marker not in first_text

    second_request = model_requests[1]
    assert [message.model_dump(mode="json") for message in second_request[: len(first_request)]] == first_dump
    assert len(second_request) == len(first_request) + 3
    assert second_request[-3].model_dump(mode="json") == invalid.model_dump(mode="json")
    assert second_request[-2].role == MessageRole.TOOL
    assert second_request[-2].tool_call_id == "invalid-call"
    assert json.loads(second_request[-2].content) == {"status": "ignored"}
    assert second_request[-1].role == MessageRole.USER
    assert second_request[-1].content == LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT
    assert [message.model_dump(mode="json") for message in context.messages] == original_messages


@pytest.mark.asyncio
async def test_precheck_two_invalid_responses_fails_without_execution(monkeypatch):
    context = _context()
    saved = []
    invalid_responses = [
        _response(_assistant(call_id="invalid-1", content="body-1")),
        _response(_assistant(call_id="invalid-2", refusal="refused-2")),
    ]
    requests = await _patch_precheck_request_flow(monkeypatch, invalid_responses, saved)

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "failed"
    assert result.error_type == "invalid_recall_response"
    assert len(requests) == 2
    assert saved == []
    assert [message.id for message in context.messages] == [1]
    assert context.turn_messages == []


@pytest.mark.asyncio
async def test_precheck_llm_exception_falls_back_by_priority(monkeypatch):
    context = _context()
    fallback_calls = []
    warnings = []
    translations = []
    generated = 0
    saved = []

    class FakeLogger:
        def bind(self, **fields):
            return SimpleNamespace(warning=lambda message: warnings.append((fields, message)))

    def translate(key, **params):
        translations.append((key, params))
        return "translated warning"

    async def load(_context):
        return None, None, "assistant-key", "tool-key"

    async def select(_context):
        return True

    async def prepare(_context, messages, *, is_main_context):
        return list(messages), {"input_tokens": 1}, "response-id"

    async def generate(_context, _messages, _metadata):
        nonlocal generated
        generated += 1
        if generated == 1:
            raise LLMException(message="network")
        return _response(_assistant())

    async def fallback(context_arg, excluded):
        assert len(warnings) == 1
        assert warnings[0][0]["priority"] == context_arg.channel_rule.priority == 1
        fallback_calls.append(set(excluded))
        context_arg.channel_rule = SimpleNamespace(priority=2)
        return True

    async def update(_context, _response):
        return None

    async def save(_context, message, **_kwargs):
        saved.append(message)

    monkeypatch.setattr(precheck_module, "load_dedupe_messages", load)
    monkeypatch.setattr(precheck_module, "select_initial_channel", select)
    monkeypatch.setattr(precheck_module, "prepare_request_messages", prepare)
    monkeypatch.setattr(precheck_module, "generate", generate)
    monkeypatch.setattr(precheck_module, "fallback_channel", fallback)
    monkeypatch.setattr(precheck_module, "update_output_metadata", update)
    monkeypatch.setattr(precheck_module, "save_and_execute_recall", save)
    monkeypatch.setattr(precheck_module, "logger", FakeLogger())
    monkeypatch.setattr(precheck_module, "t", translate)

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert generated == 2
    assert fallback_calls == [{1}]
    assert len(saved) == 1
    assert len(warnings) == 1
    assert translations == [(LOG_MEMORY_RECALL_CHANNEL_FAILED, {"error": "network"})]
    warning_fields, warning_message = warnings[0]
    assert warning_message == "translated warning"
    assert warning_fields["uid"] == "uid-1"
    assert warning_fields["session_id"] == "session-1"
    assert warning_fields["priority"] == 1
    assert warning_fields["call_context"] == "chat_dispatch_non_stream_memory_recall"
    assert warning_fields["channel_id"] == 11
    assert warning_fields["channel_name"] == "primary / model-1"
    assert warning_fields["model_id"] == "model-1"
    assert warning_fields["model_name"] == "model-1"


@pytest.mark.asyncio
async def test_precheck_all_llm_channels_failed_returns_failed(monkeypatch):
    context = _context()
    fallback_calls = []
    generated = 0

    async def load(_context):
        return None, None, "assistant-key", "tool-key"

    async def select(_context):
        return True

    async def prepare(_context, messages, *, is_main_context):
        return list(messages), {"input_tokens": 1}, "response-id"

    async def generate(_context, _messages, _metadata):
        nonlocal generated
        generated += 1
        raise LLMException(message="network")

    async def fallback(context_arg, excluded):
        fallback_calls.append(set(excluded))
        if len(fallback_calls) == 1:
            context_arg.channel_rule = SimpleNamespace(priority=2)
            return True
        return False

    monkeypatch.setattr(precheck_module, "load_dedupe_messages", load)
    monkeypatch.setattr(precheck_module, "select_initial_channel", select)
    monkeypatch.setattr(precheck_module, "prepare_request_messages", prepare)
    monkeypatch.setattr(precheck_module, "generate", generate)
    monkeypatch.setattr(precheck_module, "fallback_channel", fallback)

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "failed"
    assert result.error_type == "llm_exception"
    assert generated == 2
    assert fallback_calls == [{1}, {1, 2}]
