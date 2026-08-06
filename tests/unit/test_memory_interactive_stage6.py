import copy
import json
from types import SimpleNamespace

import pytest

from app.core.dispatchers import interactive as interactive_module
from app.core.dispatchers import interactive_helpers as interactive_helpers_module
from app.core.dispatchers import non_stream as non_stream_module
from app.core.dispatchers import stream as stream_module
from app.core.dispatchers.memory_recall_types import build_result
from app.core.prompts import PROMPT_MAX_TURNS_REACHED
from app.core.tools import MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA
from app.core.tools.longterm_memory import MANAGE_LONGTERM_MEMORY_TOOL_NAME
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.message import InternalMessage, InternalToolCall, MessageRole


class _Session:
    async def commit(self):
        return None


class _Channel:
    id = 1
    base_url = "https://example.invalid"
    chat_timeout = 60

    def get_decrypted_api_key(self):
        return "test-key"


class _Dispatcher(non_stream_module.NonStreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(cls, db, message, uid, session_id, profile, attachments):
        return None


class _StreamDispatcher(stream_module.StreamDispatcherMixin):
    @classmethod
    async def validate_initial_message_before_save(cls, db, message, uid, session_id, profile, attachments):
        return None


_MISSING = object()


def _build_cfg(memory=_MISSING, *, max_turns=1):
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(
            max_turns=max_turns,
            max_parallel_tools=4,
            executor_max_workers=1,
        ),
    )
    if memory is not _MISSING:
        cfg.memory = memory
    return cfg


def _recall_messages(boundary: int) -> tuple[InternalMessage, InternalMessage]:
    tool_call = InternalToolCall(
        id=f"recall-{boundary}",
        name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        arguments={"operation": "recall", "query": "current request"},
    )
    return (
        InternalMessage(
            id=1000 + boundary,
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[tool_call],
        ),
        InternalMessage(
            id=2000 + boundary,
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"operation":"recall","status":"success","items":[]}',
        ),
    )


async def _audit_tool_round_none(*args, **kwargs):
    return None


def _install_dispatcher_stubs(
    monkeypatch,
    cfg,
    response_messages,
    request_log,
    event_log,
    precheck,
    *,
    process_tool=None,
    expose_memory_tool=False,
):
    responses = list(response_messages)
    profile = SimpleNamespace(id=1)
    channel = _Channel()

    async def get_user(db, uid):
        return SimpleNamespace(username="tester")

    async def get_profile(db, profile_id):
        return profile

    async def validate_profile(db, current_profile):
        return cfg

    async def select_channel(db, channel_config, expected_usage, **kwargs):
        return channel, {"model_id": "chat-model", "usage": "CHAT", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    async def get_tools(db, current_profile):
        if expose_memory_tool:
            return [copy.deepcopy(MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA)], []
        return [], []

    async def mark_processed(db, message_id):
        return None

    async def prepare_messages(*args, **kwargs):
        return [args[5].model_copy(deep=True)]

    async def apply_checkpoint(db, **kwargs):
        return kwargs["messages"]

    async def materialize_environment_prompt(db, session_id, messages, max_tokens):
        return messages

    async def save_assistant(db, session_id, uid, profile_id, message, **kwargs):
        return SimpleNamespace(id=3000, content=message.content)

    async def save_tool_response(db, session_id, uid, profile_id, tool_result, messages, turn_messages, **kwargs):
        messages.append(tool_result)
        turn_messages.append(tool_result)
        return SimpleNamespace(id=3001, content=tool_result.content)

    async def generate(**kwargs):
        request_log.append(
            {
                "messages": [message.model_copy(deep=True) for message in kwargs["messages"]],
                "tools": kwargs["tools"],
            }
        )
        event_log.append("generate")
        return SimpleNamespace(message=responses.pop(0))

    async def generate_with_stream_callback(**kwargs):
        request_log.append(
            {
                "messages": [message.model_copy(deep=True) for message in kwargs["messages"]],
                "tools": kwargs["tools"],
            }
        )
        event_log.append("generate")
        response_message = responses.pop(0)
        if isinstance(response_message.content, str) and response_message.content:
            await kwargs["on_content"](response_message.content)
        return SimpleNamespace(message=response_message)

    async def isolated_tool(*args, **kwargs):
        if process_tool is not None:
            return await process_tool(*args, **kwargs)
        tool_call = args[0]
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"status":"success"}',
        )

    monkeypatch.setattr(interactive_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(interactive_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(interactive_module, "validate_profile_and_cfg", validate_profile)
    monkeypatch.setattr(interactive_module, "select_channel", select_channel)
    monkeypatch.setattr(interactive_module, "get_tools_for_profile", get_tools)
    monkeypatch.setattr(interactive_module, "mark_initial_message_processed", mark_processed)
    monkeypatch.setattr(interactive_module, "get_multimodal_from_entry", lambda model_entry: (False, False, False))
    monkeypatch.setattr(
        interactive_module,
        "resolve_chat_params",
        lambda model_entry, current_channel: {
            "temperature": None,
            "top_p": None,
            "max_tokens": 256,
            "chat_timeout": 60,
            "context_window_k": 4,
        },
    )
    monkeypatch.setattr(interactive_module, "prepare_messages", prepare_messages)
    monkeypatch.setattr(interactive_module, "apply_context_summary_checkpoint", apply_checkpoint)
    monkeypatch.setattr(interactive_module, "materialize_latest_user_environment_prompt", materialize_environment_prompt)
    monkeypatch.setattr(interactive_module.ContextManager, "trim_messages_for_model_request", lambda **kwargs: kwargs["messages"])
    monkeypatch.setattr(interactive_module, "run_memory_recall_precheck", precheck)
    monkeypatch.setattr(interactive_module.LLMClient, "generate", generate)
    monkeypatch.setattr(interactive_module.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    monkeypatch.setattr(interactive_module, "save_assistant_message", save_assistant)
    monkeypatch.setattr(interactive_module, "save_tool_response", save_tool_response)
    monkeypatch.setattr(interactive_module, "audit_tool_round", _audit_tool_round_none)
    monkeypatch.setattr(interactive_module, "prevalidate_tool_round", lambda *args, **kwargs: {})
    monkeypatch.setattr(interactive_helpers_module, "process_single_tool_with_isolated_db", isolated_tool)


async def _dispatch_non_stream(*, checkpoint_callback=None, additional_fetcher=None, resume_state=None):
    return await _Dispatcher.dispatch(
        db=_Session(),
        message="original",
        uid="user-1",
        session_id="session-1",
        persisted_initial_message=InternalMessage(id=10, role=MessageRole.USER, content="original"),
        frozen_user_message_ids=[10],
        persisted_profile_id=1,
        additional_user_messages_fetcher=additional_fetcher,
        execution_checkpoint_callback=checkpoint_callback,
        execution_resume_state=resume_state,
    )


@pytest.mark.asyncio
async def test_memory_recall_precheck_is_checkpointed_before_formal_non_stream_generation(monkeypatch):
    cfg = _build_cfg(SimpleNamespace(enabled=True), max_turns=1)
    checkpoints = []
    request_log = []
    event_log = []
    precheck_calls = []

    async def precheck(context):
        event_log.append("precheck")
        precheck_calls.append(context.current_user_boundary_message_id)
        assistant_message, tool_message = _recall_messages(context.current_user_boundary_message_id)
        context.messages.extend([assistant_message, tool_message])
        context.turn_messages.extend([assistant_message, tool_message])
        return build_result(context, "completed")

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [InternalMessage(role=MessageRole.ASSISTANT, content="formal answer")],
        request_log,
        event_log,
        precheck,
    )

    response = await _dispatch_non_stream(checkpoint_callback=save_checkpoint, additional_fetcher=lambda: _empty_batch())

    assert precheck_calls == [10]
    assert event_log == ["precheck", "generate"]
    assert [(item["memory_recall_boundary_message_id"], item["memory_recall_status"]) for item in checkpoints] == [
        (10, "pending"),
        (10, "completed"),
    ]
    assert [item["current_turn"] for item in checkpoints] == [0, 0]
    formal_messages = request_log[0]["messages"]
    assert any(message.role == MessageRole.ASSISTANT and message.tool_calls and message.tool_calls[0].id == "recall-10" for message in formal_messages)
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "recall-10" for message in formal_messages)
    assert response["choices"][0]["message"]["content"] == "formal answer"


async def _empty_batch():
    return None


@pytest.mark.asyncio
async def test_memory_recall_rechecks_after_new_user_batch_with_new_boundary(monkeypatch):
    cfg = _build_cfg(SimpleNamespace(enabled=True), max_turns=1)
    checkpoints = []
    request_log = []
    event_log = []
    precheck_calls = []
    follow_up = UserInputBatch(
        messages=(InternalMessage(id=20, role=MessageRole.USER, content="follow-up"),),
        source_message_ids=(20,),
    )
    fetch_values = iter([None, follow_up, None, None])

    async def fetch_additional():
        return next(fetch_values)

    async def precheck(context):
        boundary = context.current_user_boundary_message_id
        precheck_calls.append(boundary)
        assistant_message, tool_message = _recall_messages(boundary)
        context.messages.extend([assistant_message, tool_message])
        context.turn_messages.extend([assistant_message, tool_message])
        return build_result(context, "completed")

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [
            InternalMessage(role=MessageRole.ASSISTANT, content="first answer"),
            InternalMessage(role=MessageRole.ASSISTANT, content="second answer"),
        ],
        request_log,
        event_log,
        precheck,
    )

    await _dispatch_non_stream(checkpoint_callback=save_checkpoint, additional_fetcher=fetch_additional)

    assert precheck_calls == [10, 20]
    for boundary in (10, 20):
        statuses = [checkpoint["memory_recall_status"] for checkpoint in checkpoints if checkpoint.get("memory_recall_boundary_message_id") == boundary]
        assert statuses[0] == "pending"
        assert statuses[-1] == "completed"
    assert any(message.id == 20 for message in request_log[1]["messages"])
    assert any(message.role == MessageRole.ASSISTANT and message.tool_calls and message.tool_calls[0].id == "recall-20" for message in request_log[1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_status", ["completed", "pending"])
async def test_memory_recall_resume_completed_reuses_checkpoint_and_pending_retries(monkeypatch, resume_status):
    cfg = _build_cfg(SimpleNamespace(enabled=True), max_turns=1)
    checkpoints = []
    request_log = []
    event_log = []
    precheck_calls = []
    assistant_message, tool_message = _recall_messages(10)
    checkpoint_messages = (
        [
            InternalMessage(id=10, role=MessageRole.USER, content="original"),
            assistant_message,
            tool_message,
        ]
        if resume_status == "completed"
        else [InternalMessage(id=10, role=MessageRole.USER, content="original")]
    )

    async def precheck(context):
        precheck_calls.append(context.current_user_boundary_message_id)
        assistant, tool = _recall_messages(context.current_user_boundary_message_id)
        context.messages.extend([assistant, tool])
        context.turn_messages.extend([assistant, tool])
        return build_result(context, "completed")

    async def save_checkpoint(checkpoint):
        checkpoints.append(dict(checkpoint))

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [InternalMessage(role=MessageRole.ASSISTANT, content="resumed answer")],
        request_log,
        event_log,
        precheck,
    )

    await _dispatch_non_stream(
        checkpoint_callback=save_checkpoint,
        additional_fetcher=lambda: _empty_batch(),
        resume_state={
            "messages": [message.model_dump(mode="json") for message in checkpoint_messages],
            "turn_messages": [],
            "files_to_user": [],
            "current_turn": 0,
            "memory_recall_boundary_message_id": 10,
            "memory_recall_status": resume_status,
        },
    )

    assert precheck_calls == ([] if resume_status == "completed" else [10])
    if resume_status == "pending":
        assert [checkpoint["memory_recall_status"] for checkpoint in checkpoints] == ["pending", "completed"]
    else:
        assert checkpoints == []
    formal_messages = request_log[0]["messages"]
    assert any(message.role == MessageRole.ASSISTANT and message.tool_calls and message.tool_calls[0].id == "recall-10" for message in formal_messages)
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "recall-10" for message in formal_messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_config", [_MISSING, SimpleNamespace(enabled=False)])
async def test_memory_recall_is_not_prechecked_without_enabled_memory(monkeypatch, memory_config):
    cfg = _build_cfg(memory_config, max_turns=1) if memory_config is not _MISSING else _build_cfg(max_turns=1)
    request_log = []
    event_log = []
    precheck_calls = []

    async def precheck(context):
        precheck_calls.append(context.current_user_boundary_message_id)
        raise AssertionError("memory recall must be disabled")

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [InternalMessage(role=MessageRole.ASSISTANT, content="normal answer")],
        request_log,
        event_log,
        precheck,
    )

    response = await _dispatch_non_stream(additional_fetcher=lambda: _empty_batch())

    assert precheck_calls == []
    assert len(request_log) == 1
    assert response["choices"][0]["message"]["content"] == "normal answer"


@pytest.mark.asyncio
async def test_stream_dispatch_runs_memory_recall_precheck_in_stream_mode(monkeypatch):
    cfg = _build_cfg(SimpleNamespace(enabled=True), max_turns=1)
    request_log = []
    event_log = []
    modes = []

    async def precheck(context):
        modes.append(context.dispatcher_mode)
        assistant_message, tool_message = _recall_messages(context.current_user_boundary_message_id)
        context.messages.extend([assistant_message, tool_message])
        context.turn_messages.extend([assistant_message, tool_message])
        return build_result(context, "completed")

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [InternalMessage(role=MessageRole.ASSISTANT, content="stream answer")],
        request_log,
        event_log,
        precheck,
    )

    events = [
        event
        async for event in _StreamDispatcher.dispatch_stream(
            db=_Session(),
            message="original",
            uid="user-1",
            session_id="session-1",
            persisted_initial_message=InternalMessage(id=10, role=MessageRole.USER, content="original"),
            frozen_user_message_ids=[10],
            persisted_profile_id=1,
            additional_user_messages_fetcher=_empty_batch,
        )
    ]

    assert modes == ["stream"]
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "recall-10" for message in request_log[0]["messages"])
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_formal_longterm_memory_mutation_receives_recall_boundary_as_source_message_id(monkeypatch):
    cfg = _build_cfg(SimpleNamespace(enabled=True), max_turns=1)
    request_log = []
    event_log = []
    source_message_ids = []
    mutation_call = InternalToolCall(
        id="mutation-1",
        name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        arguments={
            "operation": "create",
            "content": "a project fact",
            "memory_key": "project.fact",
            "memory_type": "project",
        },
    )

    async def precheck(context):
        return build_result(context, "completed")

    async def process_tool(tool_call, *args, **kwargs):
        source_message_ids.append(kwargs["source_message_id"])
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"operation":"create","status":"accepted"}',
        )

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [
            InternalMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=[mutation_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="mutation complete"),
        ],
        request_log,
        event_log,
        precheck,
        process_tool=process_tool,
    )

    response = await _dispatch_non_stream(additional_fetcher=lambda: _empty_batch())

    assert source_message_ids == [10]
    assert response["choices"][0]["message"]["content"] == "mutation complete"


@pytest.mark.asyncio
async def test_formal_non_stream_memory_content_too_long_retries_same_fact_without_recursive_tool_execution(monkeypatch):
    cfg = _build_cfg(max_turns=4)
    request_log = []
    event_log = []
    execution_calls = []
    execution_depth = 0
    max_execution_depth = 0
    factual_content = "MySQL compatibility is required and PostgreSQL support is optional."
    long_content = f"{factual_content} Keep this stable database compatibility requirement available across future sessions and do not add unrelated implementation explanation."
    first_call = InternalToolCall(
        id="memory-create-too-long",
        name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        arguments={
            "operation": "create",
            "content": long_content,
            "memory_key": "database.compatibility",
            "memory_type": "fact",
        },
    )
    second_call = InternalToolCall(
        id="memory-create-retry",
        name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        arguments={
            "operation": "create",
            "content": factual_content,
            "memory_key": "database.compatibility",
            "memory_type": "fact",
        },
    )

    async def precheck(context):
        return build_result(context, "completed")

    async def process_tool(tool_call, *args, **kwargs):
        nonlocal execution_depth, max_execution_depth
        execution_depth += 1
        max_execution_depth = max(max_execution_depth, execution_depth)
        execution_calls.append(tool_call)
        event_log.append(f"tool:{tool_call.id}")
        try:
            if tool_call.id == first_call.id:
                content = '{"operation":"create","status":"content_too_long","actual_tokens":161,"max_tokens":160,"retryable":true}'
            elif tool_call.id == second_call.id:
                content = '{"operation":"create","status":"accepted","job_id":91}'
            else:
                raise AssertionError(f"unexpected memory tool call: {tool_call.id}")
            return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=content)
        finally:
            execution_depth -= 1

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [
            InternalMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=[first_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=[second_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="memory operation completed"),
        ],
        request_log,
        event_log,
        precheck,
        process_tool=process_tool,
        expose_memory_tool=True,
    )

    response = await _dispatch_non_stream(additional_fetcher=lambda: _empty_batch())

    assert len(request_log) == 3
    assert all(item["tools"] == [MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA] for item in request_log)
    second_request_messages = request_log[1]["messages"]
    first_tool_result = next(message for message in second_request_messages if message.tool_call_id == first_call.id)
    assert json.loads(first_tool_result.content) == {
        "operation": "create",
        "status": "content_too_long",
        "actual_tokens": 161,
        "max_tokens": 160,
        "retryable": True,
    }
    third_request_messages = request_log[2]["messages"]
    second_tool_result = next(message for message in third_request_messages if message.tool_call_id == second_call.id)
    assert json.loads(second_tool_result.content) == {"operation": "create", "status": "accepted", "job_id": 91}
    assert [tool_call.id for tool_call in execution_calls] == [first_call.id, second_call.id]
    assert [tool_call.arguments["content"] for tool_call in execution_calls] == [long_content, factual_content]
    assert len(factual_content) < len(long_content)
    assert execution_depth == 0
    assert max_execution_depth == 1
    assert event_log == [
        "generate",
        f"tool:{first_call.id}",
        "generate",
        f"tool:{second_call.id}",
        "generate",
    ]
    assert response["choices"][0]["message"]["content"] == "memory operation completed"


@pytest.mark.asyncio
async def test_formal_non_stream_repeated_memory_content_too_long_ends_on_max_turn_summary(monkeypatch):
    cfg = _build_cfg(max_turns=3)
    request_log = []
    event_log = []
    execution_calls = []
    first_call = InternalToolCall(
        id="memory-create-too-long-1",
        name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        arguments={
            "operation": "create",
            "content": "MySQL compatibility is required and PostgreSQL support is optional.",
            "memory_key": "database.compatibility",
            "memory_type": "fact",
        },
    )
    second_call = InternalToolCall(
        id="memory-create-too-long-2",
        name=MANAGE_LONGTERM_MEMORY_TOOL_NAME,
        arguments={
            "operation": "create",
            "content": "MySQL compatibility is required and PostgreSQL support is optional.",
            "memory_key": "database.compatibility",
            "memory_type": "fact",
        },
    )

    async def precheck(context):
        return build_result(context, "completed")

    async def process_tool(tool_call, *args, **kwargs):
        execution_calls.append(tool_call)
        if tool_call.id not in {first_call.id, second_call.id}:
            raise AssertionError(f"unexpected memory tool call: {tool_call.id}")
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content='{"operation":"create","status":"content_too_long","actual_tokens":161,"max_tokens":160,"retryable":true}',
        )

    _install_dispatcher_stubs(
        monkeypatch,
        cfg,
        [
            InternalMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=[first_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content=None, tool_calls=[second_call]),
            InternalMessage(role=MessageRole.ASSISTANT, content="final summary"),
        ],
        request_log,
        event_log,
        precheck,
        process_tool=process_tool,
        expose_memory_tool=True,
    )

    response = await _dispatch_non_stream(additional_fetcher=lambda: _empty_batch())

    assert len(request_log) == 3
    assert all(item["tools"] == [MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA] for item in request_log[:2])
    assert request_log[-1]["tools"] is None
    summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=cfg.tool.max_turns)
    assert any(message.role == MessageRole.USER and summary_notice in (message.content or "") for message in request_log[-1]["messages"])
    assert [tool_call.id for tool_call in execution_calls] == [first_call.id, second_call.id]
    assert response["choices"][0]["message"]["content"] == "final summary"
