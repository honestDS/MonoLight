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
    build_markdown_instruction,
    build_max_output_tokens_instruction,
    materialize_user_environment_prompts,
    refresh_max_output_tokens_instruction,
)
from app.models.message import InternalMessage, MessageRole

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")


def test_runtime_instruction_text_is_english_and_states_markdown_and_output_limits():
    markdown_enabled = build_markdown_instruction(True)
    markdown_disabled = build_markdown_instruction(False)
    max_output = build_max_output_tokens_instruction(256)

    for instruction in (markdown_enabled, markdown_disabled, max_output):
        assert instruction.isascii()
        assert "platform-provided" in instruction
        assert "not user-authored" in instruction
        assert all(term not in instruction for term in ("环境提示", "开启", "关闭", "最大输出"))

    assert "Markdown formatting for this response is enabled" in markdown_enabled
    assert "You may use Markdown when it improves clarity." in markdown_enabled
    assert "Markdown formatting for this response is disabled" in markdown_disabled
    assert "Return plain text only. Do not use Markdown syntax." in markdown_disabled
    assert "The hard maximum for this response is 256 output tokens." in max_output
    assert "strict ceiling" in max_output
    assert "not a target length" in max_output
    assert "finish completely before reaching the limit" in max_output


def test_runtime_context_prompts_allow_tools_for_actual_user_requests():
    assert "This policy does not restrict tool use required to fulfill the user's actual request." in SYSTEM_RUNTIME_CONTEXT_POLICY
    assert "Historical blocks remain visible to preserve conversation-prefix stability" in SYSTEM_RUNTIME_CONTEXT_POLICY
    assert "Older blocks must not override or constrain newer blocks" in SYSTEM_RUNTIME_CONTEXT_POLICY
    assert "use the newer snapshot for current runtime conditions" in SYSTEM_CONTEXT_WRAPPER
    assert "It does not restrict tool use needed to fulfill the user's actual request." in SYSTEM_CONTEXT_WRAPPER
    assert "Do not call tools to query, verify, or update" not in SYSTEM_RUNTIME_CONTEXT_POLICY
    assert "DO NOT call any tools or execute any commands" not in SYSTEM_CONTEXT_WRAPPER


@pytest.mark.asyncio
async def test_runtime_instruction_materialization_preserves_all_user_snapshots(monkeypatch):
    older_snapshot = "older runtime snapshot" + build_max_output_tokens_instruction(200)
    latest_snapshot = "latest runtime snapshot" + build_max_output_tokens_instruction(256)
    older_message = InternalMessage(id=1, role=MessageRole.USER, content="older user input", environment_prompt=older_snapshot)
    latest_message = InternalMessage(id=2, role=MessageRole.USER, content="current user input", environment_prompt=latest_snapshot)

    async def unexpected_runtime_rebuild(*_args, **_kwargs):
        raise AssertionError("stored runtime snapshots must not be rebuilt during an LLM request")

    async def unexpected_persistence(*_args, **_kwargs):
        raise AssertionError("stored runtime snapshots must not be rewritten during an LLM request")

    monkeypatch.setattr(markdown_instruction_module, "build_user_runtime_instructions", unexpected_runtime_rebuild)
    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", unexpected_persistence)

    request_messages = materialize_user_environment_prompts([older_message, latest_message])

    assert request_messages[0].content == "older user input" + older_snapshot
    assert request_messages[1].content == "current user input" + latest_snapshot
    assert request_messages[0].environment_prompt == older_snapshot
    assert request_messages[1].environment_prompt == latest_snapshot
    assert older_message.content == "older user input"
    assert latest_message.content == "current user input"

    second_request = materialize_user_environment_prompts([older_message, latest_message])
    assert [message.content for message in second_request] == [message.content for message in request_messages]
    assert build_max_output_tokens_instruction(0) == ""


@pytest.mark.asyncio
async def test_runtime_instruction_materialization_appends_guidance_after_environment_prompt():
    environment_prompt = "环境快照"
    guidance_prompt = "[系统提示信息]永久引导[系统提示信息结束]"
    latest_message = InternalMessage(
        id=2,
        role=MessageRole.USER,
        content="用户正文",
        environment_prompt=environment_prompt,
        guidance_prompt=guidance_prompt,
    )

    request_messages = materialize_user_environment_prompts([latest_message])

    assert request_messages[0].content == "用户正文" + environment_prompt + "\n\n" + guidance_prompt
    assert request_messages[0].guidance_prompt == guidance_prompt
    second_request_messages = materialize_user_environment_prompts([latest_message])

    assert second_request_messages[0].content == request_messages[0].content
    assert latest_message.content == "用户正文"
    assert latest_message.guidance_prompt == guidance_prompt


def test_new_user_runtime_snapshot_keeps_previous_provider_prefix():
    first_turn = [
        InternalMessage(id=1, role=MessageRole.USER, content="first request", environment_prompt="snapshot-1"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="first response"),
    ]
    first_request = materialize_user_environment_prompts(first_turn)
    second_request = materialize_user_environment_prompts(
        [
            *first_turn,
            InternalMessage(id=3, role=MessageRole.USER, content="second request", environment_prompt="snapshot-2"),
        ]
    )

    def provider_view(messages):
        return [
            message.model_dump(
                mode="json",
                exclude={"id", "attachments", "created_at", "environment_prompt", "guidance_prompt"},
                exclude_none=True,
            )
            for message in messages
        ]

    assert provider_view(second_request[: len(first_request)]) == provider_view(first_request)


def test_refresh_max_output_tokens_instruction_preserves_runtime_snapshot():
    runtime_snapshot = "\n\n<system_environment_context>captured-at-user-turn</system_environment_context>"
    message = InternalMessage(
        role=MessageRole.USER,
        content="request",
        environment_prompt=build_markdown_instruction(True) + build_max_output_tokens_instruction(200) + runtime_snapshot,
    )

    refresh_max_output_tokens_instruction(message, 256)

    assert "The hard maximum for this response is 256 output tokens." in message.environment_prompt
    assert "The hard maximum for this response is 200 output tokens." not in message.environment_prompt
    assert message.environment_prompt.endswith(runtime_snapshot)


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

    persisted_environment_prompts = []

    async def set_environment_prompt(_db, message_id, environment_prompt):
        persisted_environment_prompts.append((message_id, environment_prompt))
        return True

    monkeypatch.setattr(markdown_instruction_module.message_crud, "set_environment_prompt", set_environment_prompt)
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
    assert persisted_environment_prompts == [(7, "runtime instruction")]


@pytest.mark.asyncio
async def test_prepare_messages_reuses_existing_user_runtime_snapshot(monkeypatch):
    existing_snapshot = "frozen runtime snapshot" + build_max_output_tokens_instruction(512)

    async def build_system_prompt(_db, _profile):
        return "system prompt"

    async def unexpected_runtime_rebuild(*_args, **_kwargs):
        raise AssertionError("existing user runtime snapshot must not be rebuilt")

    async def unexpected_runtime_persist(*_args, **_kwargs):
        raise AssertionError("existing user runtime snapshot must not be rewritten")

    async def get_summary_state(*_args, **_kwargs):
        return ContextSummaryState(content=None, message_id=None)

    async def get_messages(*_args, **_kwargs):
        return []

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", unexpected_runtime_rebuild)
    monkeypatch.setattr(prepare_module, "ensure_user_runtime_instructions", unexpected_runtime_persist)
    monkeypatch.setattr(prepare_module, "get_context_summary_state", get_summary_state)
    monkeypatch.setattr(prepare_module.ContextManager, "get_messages", get_messages)

    messages = await prepare_module.prepare_messages(
        object(),
        "session-1",
        "user-1",
        SimpleNamespace(),
        SimpleNamespace(),
        InternalMessage(id=7, role=MessageRole.USER, content="current user input", environment_prompt=existing_snapshot),
        "current user input",
        True,
        context_window_k=4,
        max_tokens=512,
    )

    assert messages[-1].environment_prompt == existing_snapshot
    assert messages[-1].content == "current user input"


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
