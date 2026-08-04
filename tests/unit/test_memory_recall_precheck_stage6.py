import json
from types import SimpleNamespace

import pytest

from app.core.dispatchers import memory_recall as precheck_module
from app.core.dispatchers import memory_recall_persistence as persistence_module
from app.core.dispatchers.memory_recall_types import MemoryRecallContext
from app.core.exceptions import LLMException
from app.core.tools.longterm_memory import MANAGE_LONGTERM_MEMORY_TOOL_NAME
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


def _call(call_id="call-1", arguments=None, name=MANAGE_LONGTERM_MEMORY_TOOL_NAME):
    return InternalToolCall(
        id=call_id,
        name=name,
        arguments=arguments or {"operation": "recall", "query": "user context", "top_k": 3},
    )


def _assistant(
    call_id="call-1",
    arguments=None,
    content=None,
    refusal=None,
    message_id=None,
    name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
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
    channel = SimpleNamespace(base_url="https://example.invalid", chat_timeout=60, get_decrypted_api_key=lambda: "api-key")
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


@pytest.mark.parametrize(
    "message",
    [
        _assistant(content="assistant body"),
        _assistant(refusal="refused"),
        _assistant(),
        _assistant(arguments={"operation": "create", "content": "mutation", "memory_key": "k", "memory_type": "fact", "importance": 1}),
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


@pytest.mark.asyncio
async def test_precheck_recovers_complete_dedupe_pair_without_side_effects(monkeypatch):
    context = _context()
    assistant = _assistant(message_id=101)
    tool = InternalMessage(id=102, role=MessageRole.TOOL, tool_call_id="call-1", content=json.dumps({}))
    events = []

    async def event_callback(event):
        events.append(event)

    context.stream_event_callback = event_callback

    async def load(_context):
        return assistant, tool, "assistant-key", "tool-key"

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("dedupe recovery must not select, generate, or execute")

    monkeypatch.setattr(precheck_module, "load_dedupe_messages", load)
    monkeypatch.setattr(precheck_module, "select_initial_channel", unexpected)
    monkeypatch.setattr(precheck_module, "generate", unexpected)
    monkeypatch.setattr(precheck_module, "save_and_execute_recall", unexpected)

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert [message.id for message in context.messages] == [1, 101, 102]
    assert [message.id for message in context.turn_messages] == [101, 102]
    assert events == []
    assert context.db.commit_count == 0


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
async def test_precheck_first_valid_response_requests_once_and_saves_once(monkeypatch):
    context = _context()
    saved = []
    requests = await _patch_precheck_request_flow(
        monkeypatch,
        [_response(_assistant(call_id="valid-call"))],
        saved,
    )

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert len(requests) == 1
    assert len(saved) == 1
    assert saved[0].tool_calls[0].id == "valid-call"
    assert [message.id for message in context.messages] == [1]


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
    assert [message.id for message in context.messages] == [1]
    assert [message.id for message in context.turn_messages] == []
    assert [message.tool_calls[0].id for message in saved] == ["valid-call"]


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
    generated = 0
    saved = []

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

    result = await precheck_module.run_memory_recall_precheck(context)

    assert result.status == "completed"
    assert generated == 2
    assert fallback_calls == [{1}]
    assert len(saved) == 1


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
