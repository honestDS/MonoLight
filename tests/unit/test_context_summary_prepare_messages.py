from importlib import import_module
from types import SimpleNamespace

import pytest

from app.core.prompts import CONTEXT_SUMMARY_WRAPPER
from app.core.utils.context_summary.common import ContextSummaryState
from app.models.message import InternalMessage, MessageRole

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")


@pytest.mark.asyncio
async def test_prepare_messages_counts_system_prompt_runtime_instruction_and_current_input_for_summary(monkeypatch):
    ensure_summary_calls = []

    async def build_system_prompt(_db, _profile):
        return "system prompt"

    async def build_runtime_instructions(_db, _session_id):
        return "runtime instruction"

    async def ensure_summary(*_args, **kwargs):
        ensure_summary_calls.append(kwargs)
        return ContextSummaryState(content=None, message_id=None)

    async def get_messages(*_args, **_kwargs):
        return []

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(prepare_module, "ensure_context_summary", ensure_summary)
    monkeypatch.setattr(prepare_module.ContextManager, "get_messages", get_messages)
    monkeypatch.setattr(
        prepare_module,
        "estimate_tokens",
        lambda content: {
            "system prompt": 120,
            "runtime instruction": 30,
        }.get(content, 0),
    )

    async def check_work_validity():
        return True

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
        context_summary_work_validity_checker=check_work_validity,
    )

    assert ensure_summary_calls[0]["current_message"] == current_message
    assert ensure_summary_calls[0]["reserved_tokens"] == 150
    assert ensure_summary_calls[0]["frozen_user_message_ids"] == [7]
    assert ensure_summary_calls[0]["work_validity_checker"] is check_work_validity


@pytest.mark.asyncio
async def test_prepare_messages_combines_summary_with_history_after_boundary(monkeypatch):
    get_messages_calls = []

    async def build_system_prompt(_db, _profile):
        return "stable system prompt"

    async def build_runtime_instructions(_db, _session_id):
        return ""

    async def ensure_summary(*_args, **_kwargs):
        return ContextSummaryState(content="compressed old turns", message_id=20)

    async def get_messages(*_args, **kwargs):
        get_messages_calls.append(kwargs)
        return [
            InternalMessage(id=21, role=MessageRole.USER, content="recent question"),
            InternalMessage(id=22, role=MessageRole.ASSISTANT, content="recent answer"),
        ]

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(prepare_module, "ensure_context_summary", ensure_summary)
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
    assert messages[1].role == MessageRole.ASSISTANT
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

    async def build_runtime_instructions(_db, _session_id):
        return ""

    async def ensure_summary(*_args, **_kwargs):
        return ContextSummaryState(content="unchanged summary", message_id=20)

    async def get_messages(*_args, **_kwargs):
        return [message.model_copy(deep=True) for message in history_versions.pop(0)]

    monkeypatch.setattr(prepare_module, "build_system_prompt", build_system_prompt)
    monkeypatch.setattr(prepare_module, "build_user_runtime_instructions", build_runtime_instructions)
    monkeypatch.setattr(prepare_module, "ensure_context_summary", ensure_summary)
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
