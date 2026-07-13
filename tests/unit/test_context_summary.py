from importlib import import_module
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core import context_summary as summary_module
from app.core.context import ContextManager
from app.core.context_summary import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS, ContextSummaryState, _select_recent_rounds, _select_summary_segment, _serialize_message
from app.core.context_summary_selection import ContextSummaryModelSnapshot
from app.core.prompts import CONTEXT_SUMMARY_WRAPPER
from app.models.message import ImagePart, InternalMessage, InternalToolCall, MessageRole, TextPart
from app.models.profile import ProfileConfig

prepare_module = import_module("app.core.utils.dispatcher.prepare_messages")


def test_select_summary_segment_keeps_recent_turns_and_ends_before_user():
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="first question"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="first answer"),
        InternalMessage(id=3, role=MessageRole.USER, content="second question"),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="second answer"),
        InternalMessage(id=5, role=MessageRole.USER, content="recent question"),
        InternalMessage(id=6, role=MessageRole.ASSISTANT, content="recent answer"),
    ]

    segment = _select_summary_segment(messages, target_tokens=10_000)

    assert [message.id for message in segment] == [1, 2, 3, 4]
    assert messages[len(segment)].role == MessageRole.USER


def test_select_summary_segment_does_not_split_tool_chain():
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="run it"),
        InternalMessage(
            id=2,
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-1", name="shell", arguments={"command": "echo ok"})],
        ),
        InternalMessage(id=3, role=MessageRole.TOOL, tool_call_id="call-1", content="ok"),
        InternalMessage(id=4, role=MessageRole.USER, content="what happened"),
        InternalMessage(id=5, role=MessageRole.ASSISTANT, content="it succeeded"),
    ]

    segment = _select_summary_segment(messages, target_tokens=10_000)

    assert [message.id for message in segment] == [1, 2, 3]
    assert segment[-1].role == MessageRole.TOOL


def test_select_recent_rounds_returns_last_two_user_led_rounds():
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="first question"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="first answer"),
        InternalMessage(id=3, role=MessageRole.USER, content="second question"),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="second answer"),
        InternalMessage(id=5, role=MessageRole.USER, content="third question"),
        InternalMessage(id=6, role=MessageRole.ASSISTANT, content="third answer"),
    ]

    recent = _select_recent_rounds(messages, 2)

    assert [message.id for message in recent] == [3, 4, 5, 6]


def test_select_summary_segment_requires_a_complete_old_turn():
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="only question"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="only answer"),
    ]

    assert _select_summary_segment(messages, target_tokens=10_000) == []


def test_select_summary_segment_skips_oversized_first_turn():
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="x" * 20_000),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="answer"),
        InternalMessage(id=3, role=MessageRole.USER, content="recent"),
    ]

    assert _select_summary_segment(messages, target_tokens=10) == []


def test_serialize_message_supports_multimodal_content():
    message = InternalMessage(
        id=1,
        role=MessageRole.USER,
        content=[
            TextPart(text="describe"),
            ImagePart(image_url={"url": "data:image/png;base64,abc"}),
        ],
        attachments=["ignored.png"],
    )

    serialized = _serialize_message(message)

    assert '"type":"text"' in serialized
    assert '"type":"image_url"' in serialized
    assert "attachments" not in serialized
    assert "created_at" not in serialized


def test_summary_state_builds_stable_system_message():
    state = ContextSummaryState(content="The user selected option A.", message_id=12)

    message = state.as_message()

    assert message is not None
    assert message.role == MessageRole.SYSTEM
    assert message.content == CONTEXT_SUMMARY_WRAPPER.format(content=state.content)


def test_summary_survives_request_sliding_window_with_recent_original_messages():
    summary_content = CONTEXT_SUMMARY_WRAPPER.format(content="compressed old turns")
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content=f"stable prompt\n\n{summary_content}"),
        InternalMessage(id=21, role=MessageRole.USER, content="old recent question " * 120),
        InternalMessage(id=22, role=MessageRole.ASSISTANT, content="old recent answer " * 120),
        InternalMessage(id=23, role=MessageRole.USER, content="latest question"),
    ]

    request = ContextManager.trim_messages_for_model_request(
        messages=messages,
        uid="user-1",
        session_id="session-1",
        context_window_k=1,
        max_tokens=256,
        tools=None,
        safety_margin_tokens=64,
    )

    assert request[0].role == MessageRole.SYSTEM
    assert summary_content in request[0].content
    assert request[-1].id == 23
    assert request[-1].content == "latest question"


class _SummaryChannel:
    id = 1
    base_url = "https://example.invalid"
    protocol = "openai"
    name = "summary-channel"

    def get_decrypted_api_key(self):
        return "secret"


def _summary_cfg(threshold_percent: int = 90) -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(
            chat_channel=object(),
            context_summary_channel=object(),
        ),
        other=SimpleNamespace(context_summary_threshold_percent=threshold_percent),
    )


def _summary_history() -> list[InternalMessage]:
    return [
        InternalMessage(id=1, role=MessageRole.USER, content="u1" * 100),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="a1" * 100),
        InternalMessage(id=3, role=MessageRole.USER, content="u2" * 100),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="a2" * 100),
        InternalMessage(id=5, role=MessageRole.USER, content="recent"),
    ]


def _patch_summary_dependencies(monkeypatch, *, update_result=True, generation_error=None):
    selected_calls = []
    update_calls = []
    generated_calls = []

    async def get_state(_db, *, session_id, uid):
        return ContextSummaryState(content=None, message_id=None)

    async def build_snapshot(
        _db,
        *,
        expected_summary_message_id,
        before_id,
        frozen_user_message_ids=None,
        **_kwargs,
    ):
        target_id = (expected_summary_message_id or 0) + 4
        return summary_module.ContextSummarySnapshot(
            expected_summary_message_id=expected_summary_message_id,
            snapshot_before_id=before_id,
            snapshot_max_message_id=target_id + 1,
            persistent_summary_target_id=target_id,
            recent_round_start_ids=(target_id + 1,),
            frozen_user_message_ids=tuple(frozen_user_message_ids or ()),
            recent_messages=(InternalMessage(id=target_id + 1, role=MessageRole.USER, content="recent"),),
        )

    async def iter_rounds(_db, *, snapshot, **_kwargs):
        start_id = snapshot.expected_summary_message_id or 0
        yield [
            InternalMessage(id=start_id + 1, role=MessageRole.USER, content="u1" * 100),
            InternalMessage(id=start_id + 2, role=MessageRole.ASSISTANT, content="a1" * 100),
        ]
        yield [
            InternalMessage(id=start_id + 3, role=MessageRole.USER, content="u2" * 100),
            InternalMessage(id=start_id + 4, role=MessageRole.ASSISTANT, content="a2" * 100),
        ]

    async def select_model(*_args, **kwargs):
        selected_calls.append(kwargs)
        if kwargs.get("excluded_priorities"):
            return None
        return ContextSummaryModelSnapshot(
            channel_id=1,
            channel_name="summary-channel",
            model_id="summary-model",
            protocol="openai",
            base_url="https://example.invalid",
            api_key="secret",
            priority=1,
            context_window_tokens=4096,
            max_output_tokens=256,
            safety_margin_tokens=0,
            input_budget_tokens=3840,
        )

    async def call_model(*, model, prompt):
        generated_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "messages": [InternalMessage(role=MessageRole.USER, content=prompt)],
                "timeout": CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
            }
        )
        if generation_error is not None:
            raise generation_error
        return "compressed history"

    async def update_summary(*_args, **kwargs):
        update_calls.append(kwargs)
        return update_result

    monkeypatch.setattr(summary_module, "get_context_summary_state", get_state)
    monkeypatch.setattr(summary_module, "build_context_summary_snapshot", build_snapshot)
    monkeypatch.setattr(summary_module, "iter_persistent_summary_rounds", iter_rounds)
    monkeypatch.setattr(summary_module, "select_context_summary_model", select_model)
    monkeypatch.setattr(summary_module, "call_context_summary_model", call_model)
    monkeypatch.setattr(summary_module.session_crud, "update_context_summary", update_summary)
    return selected_calls, update_calls, generated_calls


@pytest.mark.asyncio
async def test_ensure_context_summary_triggers_persists_boundary_and_uses_isolated_cursor(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    bound_fields = {}
    debug_calls = []

    class CapturingLogger:
        def bind(self, **kwargs):
            bound_fields.update(kwargs)
            return self

        def debug(self, message, **kwargs):
            debug_calls.append((message, kwargs))

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        if "compressed history" in content:
            return 40
        return 40

    monkeypatch.setattr(summary_module, "logger", CapturingLogger())
    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(content="compressed history", message_id=4)
    assert selected_calls[0]["profile_id"] == 9
    assert update_calls == [
        {
            "session_id": "session-1",
            "uid": "user-1",
            "expected_message_id": None,
            "summary": "compressed history",
            "message_id": 4,
        }
    ]
    assert len(generated_calls) == 1
    assert generated_calls[0]["timeout"] == CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
    assert debug_calls[0][0].startswith("Context summary check:")
    assert any(call[0].startswith("Context summary generated:") for call in debug_calls)
    assert bound_fields["uid"] == "user-1"
    assert bound_fields["session_id"] == "session-1"
    assert bound_fields["summarized_through_message_id"] == 4
    assert bound_fields["summarized_message_count"] == 4
    assert bound_fields["summary_tokens"] > 0
    assert bound_fields["compression_goal_tokens"] == bound_fields["summary_trigger_tokens"] // 2


@pytest.mark.parametrize("threshold_percent", [50, 60, 70, 80, 90])
def test_profile_config_accepts_context_summary_threshold_options(threshold_percent):
    cfg = ProfileConfig.model_validate({"other": {"context_summary_threshold_percent": threshold_percent}})

    assert cfg.other.context_summary_threshold_percent == threshold_percent


def test_profile_config_defaults_context_summary_threshold_to_ninety_percent():
    cfg = ProfileConfig.model_validate({})

    assert cfg.other.context_summary_threshold_percent == 90


@pytest.mark.parametrize("threshold_percent", [0, 49, 55, 100, "90"])
def test_profile_config_rejects_invalid_context_summary_threshold(threshold_percent):
    with pytest.raises(ValidationError):
        ProfileConfig.model_validate({"other": {"context_summary_threshold_percent": threshold_percent}})


@pytest.mark.asyncio
async def test_context_summary_triggers_only_after_configured_threshold(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 100

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    below_ninety_state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(90),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert below_ninety_state == ContextSummaryState(content=None, message_id=None)
    assert selected_calls == []
    assert update_calls == []
    assert generated_calls == []

    at_fifty_state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert at_fifty_state == ContextSummaryState(content="compressed history", message_id=4)
    assert len(selected_calls) == 1
    assert len(update_calls) == 1
    assert len(generated_calls) == 1


@pytest.mark.asyncio
async def test_context_summary_threshold_includes_tool_definition_tokens(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content.startswith("["):
            return 150
        if content.startswith('{"role":'):
            return 100
        return 0

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        tools=[{"type": "function", "function": {"name": "search"}}],
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(content="compressed history", message_id=4)
    assert len(selected_calls) == 1
    assert len(update_calls) == 1
    assert len(generated_calls) == 1


@pytest.mark.asyncio
async def test_ensure_context_summary_failure_returns_previous_state(monkeypatch):
    selected_calls, update_calls, _generated_calls = _patch_summary_dependencies(
        monkeypatch,
        generation_error=RuntimeError("provider unavailable"),
    )

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 40

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=None,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(content=None, message_id=None)
    assert update_calls == []
    assert len(selected_calls) == 2
    assert selected_calls[1]["excluded_priorities"] == {1}


@pytest.mark.asyncio
async def test_ensure_context_summary_concurrent_update_returns_winning_state(monkeypatch):
    selected_calls, update_calls, _generated_calls = _patch_summary_dependencies(
        monkeypatch,
        update_result=False,
    )
    states = iter(
        [
            ContextSummaryState(content="old summary", message_id=8),
            ContextSummaryState(content="newer concurrent summary", message_id=12),
        ]
    )

    async def get_state(_db, *, session_id, uid):
        return next(states)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 40

    monkeypatch.setattr(summary_module, "get_context_summary_state", get_state)
    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=None,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(content="newer concurrent summary", message_id=12)
    assert update_calls[0]["expected_message_id"] == 8
    assert selected_calls[0]["profile_id"] == 9


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

    assert ensure_summary_calls[0]["current_message"] == current_message
    assert ensure_summary_calls[0]["reserved_tokens"] == 150
    assert ensure_summary_calls[0]["frozen_user_message_ids"] == [7]


@pytest.mark.asyncio
async def test_context_summary_trigger_includes_reserved_and_current_message_tokens(monkeypatch):
    selected_calls, _update_calls, _generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "large current input":
            return 250
        if content.startswith('{"role":'):
            return 100
        return 0

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(90),
        before_id=10,
        current_message="large current input",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=300,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(content="compressed history", message_id=4)
    assert len(selected_calls) >= 1


@pytest.mark.asyncio
async def test_first_summary_prompt_excludes_recent_protected_rounds(monkeypatch):
    _selected_calls, _update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        if "Recent dialogue for task context only" in content:
            return 50
        if "Further compress the summary below" in content:
            return 50
        return 100

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state.content == "compressed history"
    prompt = generated_calls[0]["messages"][0].content
    assert "Recent dialogue for task context only" in prompt
    assert '"content":"recent"' not in prompt
    assert "(none)" in prompt
    assert "## Goal" in prompt


@pytest.mark.asyncio
async def test_summary_recompresses_until_half_threshold_goal(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    summaries = iter(
        [
            "long summary " * 40,
            "medium summary " * 10,
            "short summary",
        ]
    )

    async def call_model(*, model, prompt):
        generated_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "messages": [InternalMessage(role=MessageRole.USER, content=prompt)],
                "timeout": CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
            }
        )
        return next(summaries)

    monkeypatch.setattr(summary_module, "call_context_summary_model", call_model)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        if "long summary" in content:
            return 450
        if "medium summary" in content:
            return 400
        if "short summary" in content:
            return 20
        if content.startswith("<conversation_summary>"):
            if "long summary" in content:
                return 450
            if "medium summary" in content:
                return 400
            if "short summary" in content:
                return 20
            return 100
        return 50

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)

    state = await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state.content == "short summary"
    assert state.message_id == 4
    assert len(generated_calls) == 3
    assert len(update_calls) == 1
    assert update_calls[0]["summary"] == "short summary"
    assert "Further compress the summary below" in generated_calls[1]["messages"][0].content
    assert "Further compress the summary below" in generated_calls[2]["messages"][0].content
    assert "Conversation segment to compress" not in generated_calls[1]["messages"][0].content
    assert len(selected_calls) >= 1


@pytest.mark.asyncio
async def test_context_summary_uses_dedicated_timeout_not_chat_timeout(monkeypatch):
    _selected_calls, _update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 40

    monkeypatch.setattr(summary_module, "estimate_tokens", estimate_tokens)
    await summary_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert generated_calls
    assert generated_calls[0]["timeout"] == CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
    assert generated_calls[0]["timeout"] != 1


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
    assert "stable system prompt" in messages[0].content
    assert CONTEXT_SUMMARY_WRAPPER.format(content="compressed old turns") in messages[0].content
    assert [message.id for message in messages[1:]] == [21, 22]


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
