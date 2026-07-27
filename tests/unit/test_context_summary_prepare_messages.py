from importlib import import_module
from types import SimpleNamespace

import pytest

from app.core.prompts import (
    CONTEXT_SUMMARY_WRAPPER,
    SYSTEM_CONTEXT_WRAPPER,
    SYSTEM_RUNTIME_CONTEXT_POLICY,
)
from app.core.utils.context_summary.common import ContextSummaryState
from app.core.utils.dispatcher import markdown_instruction as markdown_instruction_module
from app.core.utils.dispatcher.markdown_instruction import (
    append_user_runtime_instruction_text,
    build_max_output_tokens_instruction,
    materialize_latest_user_environment_prompt,
)
from app.models.message import InternalMessage, MessageRole

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")


def test_runtime_context_prompts_allow_tools_for_actual_user_requests():
    assert "This policy does not restrict tool use required to fulfill the user's actual request." in SYSTEM_RUNTIME_CONTEXT_POLICY
    assert "It does not restrict tool use needed to fulfill the user's actual request." in SYSTEM_CONTEXT_WRAPPER
    assert "Do not call tools to query, verify, or update" not in SYSTEM_RUNTIME_CONTEXT_POLICY
    assert "DO NOT call any tools or execute any commands" not in SYSTEM_CONTEXT_WRAPPER


@pytest.mark.asyncio
async def test_runtime_instruction_is_rebuilt_persisted_and_materialized_on_latest_user_message(monkeypatch):
    stale_instruction = "stale runtime instruction" + build_max_output_tokens_instruction(200)
    latest_runtime_instruction = "latest runtime instruction" + build_max_output_tokens_instruction(256)
    quoted_instruction = "quoted notice" + build_max_output_tokens_instruction(999)
    older_message = InternalMessage(id=1, role=MessageRole.USER, content=quoted_instruction, environment_prompt=stale_instruction)
    latest_message = InternalMessage(id=2, role=MessageRole.USER, content="current user input", environment_prompt=stale_instruction)
    persisted_environment_prompts = []

    async def build_runtime_instructions(_db, session_id, max_tokens):
        assert session_id == "session-1"
        assert max_tokens == 256
        return latest_runtime_instruction

    async def set_environment_prompt(_db, message_id, environment_prompt):
        persisted_environment_prompts.append((message_id, environment_prompt))
        return True

    monkeypatch.setattr(markdown_instruction_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", set_environment_prompt)

    request_messages = await materialize_latest_user_environment_prompt(
        object(),
        "session-1",
        [older_message, latest_message],
        256,
    )

    assert older_message.content == quoted_instruction
    assert latest_message.content == "current user input"
    assert request_messages[0].content == quoted_instruction
    assert request_messages[1].content == "current user input" + latest_runtime_instruction
    assert request_messages[1].environment_prompt == latest_runtime_instruction
    assert latest_message.environment_prompt == stale_instruction
    assert persisted_environment_prompts == [(2, latest_runtime_instruction)]
    assert build_max_output_tokens_instruction(0) == ""


@pytest.mark.asyncio
async def test_runtime_instruction_materialization_appends_guidance_after_environment_prompt(monkeypatch):
    environment_prompt = "环境提示"
    guidance_prompt = "[系统提示信息]永久引导[系统提示信息结束]"
    latest_message = InternalMessage(
        id=2,
        role=MessageRole.USER,
        content="用户正文",
        guidance_prompt=guidance_prompt,
    )
    persisted_environment_prompts = []

    async def build_runtime_instructions(_db, session_id, max_tokens):
        assert session_id == "session-1"
        assert max_tokens == 256
        return environment_prompt

    async def set_environment_prompt(_db, message_id, prompt):
        persisted_environment_prompts.append((message_id, prompt))
        return True

    monkeypatch.setattr(markdown_instruction_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", set_environment_prompt)

    request_messages = await materialize_latest_user_environment_prompt(
        object(),
        "session-1",
        [latest_message],
        256,
    )

    assert request_messages[0].content == "用户正文" + environment_prompt + "\n\n" + guidance_prompt
    assert request_messages[0].guidance_prompt == guidance_prompt
    second_request_messages = await materialize_latest_user_environment_prompt(
        object(),
        "session-1",
        [latest_message],
        256,
    )

    assert second_request_messages[0].content == request_messages[0].content
    assert latest_message.content == "用户正文"
    assert latest_message.guidance_prompt == guidance_prompt
    assert persisted_environment_prompts == [(2, environment_prompt), (2, environment_prompt)]


def test_runtime_instruction_assignment_does_not_change_message_content():
    message = InternalMessage(role=MessageRole.USER, content="current user input")

    append_user_runtime_instruction_text(message, "runtime instruction")

    assert message.content == "current user input"
    assert message.environment_prompt == "runtime instruction"


@pytest.mark.asyncio
async def test_prepare_messages_only_reads_existing_summary_state(monkeypatch):
    summary_state_calls = []
    get_messages_calls = []
    runtime_instruction_calls = []

    async def build_system_prompt(_db, _profile):
        return "system prompt"

    async def build_runtime_instructions(_db, _session_id, max_tokens):
        runtime_instruction_calls.append(max_tokens)
        return "runtime instruction"

    async def get_summary_state(*_args, **kwargs):
        summary_state_calls.append(kwargs)
        return ContextSummaryState(content=None, message_id=None)

    async def get_messages(*_args, **kwargs):
        get_messages_calls.append(kwargs)
        return []

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(
        prepare_module,
        "build_user_runtime_instructions",
        build_runtime_instructions,
    )
    monkeypatch.setattr(
        prepare_module,
        "get_context_summary_state",
        get_summary_state,
    )
    monkeypatch.setattr(prepare_module.ContextManager, "get_messages", get_messages)
    monkeypatch.setattr(
        prepare_module,
        "estimate_tokens",
        lambda content: {
            "system prompt": 120,
            "runtime instruction": 30,
        }.get(content, 0),
    )

    current_message = "current user input"
    await prepare_module.prepare_messages(
        object(),
        "session-1",
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(),
        InternalMessage(id=7, role=MessageRole.USER, content=current_message),
        current_message,
        True,
        context_window_k=4,
        max_tokens=512,
    )

    assert summary_state_calls == [
        {
            "session_id": "session-1",
            "uid": "user-1",
        }
    ]
    assert runtime_instruction_calls == [512]
    assert get_messages_calls[0]["reserved_tokens"] == 150


@pytest.mark.asyncio
async def test_prepare_messages_appends_additional_system_prompt_and_reserves_its_tokens(monkeypatch):
    get_messages_calls = []
    combined_system_prompt = "base system prompt\n\nchannel instruction"

    async def build_system_prompt(_db, _profile):
        return "base system prompt"

    async def build_runtime_instructions(_db, _session_id, _max_tokens):
        return ""

    async def get_summary_state(*_args, **_kwargs):
        return ContextSummaryState(content=None, message_id=None)

    async def get_messages(*_args, **kwargs):
        get_messages_calls.append(kwargs)
        return []

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(prepare_module, "get_context_summary_state", get_summary_state)
    monkeypatch.setattr(prepare_module.ContextManager, "get_messages", get_messages)
    monkeypatch.setattr(prepare_module, "estimate_tokens", lambda content: 123 if content == combined_system_prompt else 0)

    messages = await prepare_module.prepare_messages(
        object(),
        "session-1",
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(),
        None,
        "",
        False,
        additional_system_prompt=" channel instruction ",
    )

    assert get_messages_calls[0]["reserved_tokens"] == 123
    assert messages[0].role == MessageRole.SYSTEM
    assert messages[0].content == combined_system_prompt


@pytest.mark.asyncio
async def test_prepare_messages_combines_summary_with_history_after_boundary(monkeypatch):
    get_messages_calls = []

    async def build_system_prompt(_db, _profile):
        return "stable system prompt"

    async def build_runtime_instructions(_db, _session_id, _max_tokens):
        return ""

    async def get_summary_state(*_args, **_kwargs):
        return ContextSummaryState(content="compressed old turns", message_id=20)

    async def get_messages(*_args, **kwargs):
        get_messages_calls.append(kwargs)
        return [
            InternalMessage(id=21, role=MessageRole.USER, content="recent question"),
            InternalMessage(id=22, role=MessageRole.ASSISTANT, content="recent answer"),
        ]

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(
        prepare_module,
        "get_context_summary_state",
        get_summary_state,
    )
    monkeypatch.setattr(prepare_module.ContextManager, "get_messages", get_messages)

    messages = await prepare_module.prepare_messages(
        object(),
        "session-1",
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(),
        None,
        "",
        False,
        context_window_k=4,
        max_tokens=512,
    )

    assert get_messages_calls[0]["after_id"] == 20
    assert messages[0].role == MessageRole.SYSTEM
    assert messages[0].content == "stable system prompt"
    assert messages[1].role == MessageRole.USER
    assert messages[1].content == CONTEXT_SUMMARY_WRAPPER.format(
        through_message_id=20,
        content="compressed old turns",
    )
    assert [message.id for message in messages[2:]] == [21, 22]


@pytest.mark.asyncio
async def test_prepare_messages_keeps_provider_prefix_stable_across_unsummarized_turns(monkeypatch):
    history_versions = [
        [
            InternalMessage(id=21, role=MessageRole.USER, content="recent question"),
            InternalMessage(id=22, role=MessageRole.ASSISTANT, content="recent answer"),
        ],
        [
            InternalMessage(id=21, role=MessageRole.USER, content="recent question"),
            InternalMessage(id=22, role=MessageRole.ASSISTANT, content="recent answer"),
            InternalMessage(id=23, role=MessageRole.USER, content="new question"),
            InternalMessage(id=24, role=MessageRole.ASSISTANT, content="new answer"),
        ],
    ]

    async def build_system_prompt(_db, _profile):
        return "stable system prompt"

    async def build_runtime_instructions(_db, _session_id, _max_tokens):
        return ""

    async def get_summary_state(*_args, **_kwargs):
        return ContextSummaryState(content="unchanged summary", message_id=20)

    async def get_messages(*_args, **_kwargs):
        return [message.model_copy(deep=True) for message in history_versions.pop(0)]

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(
        prepare_module,
        "get_context_summary_state",
        get_summary_state,
    )
    monkeypatch.setattr(prepare_module.ContextManager, "get_messages", get_messages)

    first_request = await prepare_module.prepare_messages(
        object(),
        "session-1",
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(),
        None,
        "",
        False,
        context_window_k=4,
        max_tokens=512,
    )
    second_request = await prepare_module.prepare_messages(
        object(),
        "session-1",
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(),
        None,
        "",
        False,
        context_window_k=4,
        max_tokens=512,
    )

    def provider_prefix(messages):
        return [
            message.model_dump(
                mode="json",
                exclude={"id", "attachments", "created_at"},
                exclude_none=True,
            )
            for message in messages
        ]

    assert provider_prefix(second_request[: len(first_request)]) == provider_prefix(first_request)
    assert [message.id for message in second_request[len(first_request) :]] == [23, 24]
