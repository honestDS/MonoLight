import pytest
from pydantic import ValidationError

from app.core.context import ContextManager
from app.core.prompts import (
    CONTEXT_SUMMARY_COMPRESS_PROMPT,
    CONTEXT_SUMMARY_PROMPT,
    CONTEXT_SUMMARY_WRAPPER,
    RECENT_TOOL_SUMMARY_WRAPPER,
)
from app.core.utils.context_messages import find_protected_tail_start
from app.core.utils.context_summary.common import (
    ContextSummaryState,
)
from app.core.utils.context_summary.common import (
    select_recent_rounds as _select_recent_rounds,
)
from app.core.utils.context_summary.common import (
    select_summary_segment as _select_summary_segment,
)
from app.core.utils.context_summary.common import (
    serialize_message as _serialize_message,
)
from app.models.message import ImagePart, InternalMessage, InternalToolCall, MessageRole, TextPart
from app.models.profile import ProfileConfig


@pytest.mark.parametrize(
    "prompt",
    [CONTEXT_SUMMARY_PROMPT, CONTEXT_SUMMARY_COMPRESS_PROMPT],
)
def test_summary_prompts_preserve_required_context_details(prompt):
    required_phrases = [
        "time-sensitive facts",
        "prices",
        "observation or source time and timezone",
        "not as current facts",
        "do not infer",
        "active user goal",
        "requested deliverables",
        "acceptance criteria",
        "constraints",
        "prohibitions",
        "exact next step",
        "completed",
        "failed",
        "unfinished",
        "tool arguments",
        "raw",
        "output",
        "intermediate process",
        "not a user instruction",
        "covered_user_message",
        "Never generate, quote, paraphrase, or modify",
    ]

    for phrase in required_phrases:
        assert phrase in prompt


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


def test_summary_state_builds_bounded_user_message():
    state = ContextSummaryState(content="The user selected option A.", message_id=12)

    message = state.as_message()

    assert message is not None
    assert message.role == MessageRole.USER
    assert message.content == CONTEXT_SUMMARY_WRAPPER.format(
        through_message_id=12,
        content=state.content,
    )


def test_summary_survives_request_sliding_window_as_bounded_user_message():
    summary_content = CONTEXT_SUMMARY_WRAPPER.format(
        through_message_id=20,
        content="compressed old turns",
    )
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable prompt"),
        InternalMessage(role=MessageRole.USER, content=summary_content),
        InternalMessage(id=21, role=MessageRole.USER, content="old recent question " * 40),
        InternalMessage(id=22, role=MessageRole.ASSISTANT, content="old recent answer " * 40),
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
    assert request[1].role == MessageRole.USER
    assert request[1].content == summary_content
    assert request[-1].id == 23
    assert request[-1].content == "latest question"


def test_request_sliding_window_preserves_two_historical_rounds_and_current_input():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable prompt"),
        InternalMessage(id=1, role=MessageRole.USER, content="discardable old question " * 200),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="discardable old answer " * 200),
        InternalMessage(id=3, role=MessageRole.USER, content="protected second-last question"),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="protected second-last answer"),
        InternalMessage(id=5, role=MessageRole.USER, content="protected last question"),
        InternalMessage(id=6, role=MessageRole.ASSISTANT, content="protected last answer"),
        InternalMessage(id=7, role=MessageRole.USER, content="current input"),
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

    assert [message.id for message in request if message.id is not None] == [3, 4, 5, 6, 7]


def test_user_role_tool_summary_does_not_count_as_a_user_round():
    temporary_summary = InternalMessage(
        role=MessageRole.USER,
        content=RECENT_TOOL_SUMMARY_WRAPPER.format(
            from_message_id=2,
            through_message_id=3,
            content="lookup completed",
        ),
    )
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="first question"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="first answer"),
        temporary_summary,
        InternalMessage(id=4, role=MessageRole.USER, content="second question"),
        InternalMessage(id=5, role=MessageRole.ASSISTANT, content="second answer"),
        InternalMessage(id=6, role=MessageRole.USER, content="current input"),
    ]

    assert find_protected_tail_start(messages) == 0


def test_recent_tool_chain_is_replaced_in_place_without_orphans_when_budget_is_tight():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable prompt"),
        InternalMessage(id=1, role=MessageRole.USER, content="write the generated file"),
        InternalMessage(
            id=2,
            role=MessageRole.ASSISTANT,
            content="I will write the file.",
            tool_calls=[
                InternalToolCall(
                    id="call-1",
                    name="write_file",
                    arguments={"file_path": "generated.txt", "content": "file content " * 3000},
                )
            ],
        ),
        InternalMessage(
            id=3,
            role=MessageRole.TOOL,
            tool_call_id="call-1",
            content="file write result " * 1000,
        ),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="The file write completed."),
        InternalMessage(id=5, role=MessageRole.USER, content="continue"),
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

    non_system = [message for message in request if message.role != MessageRole.SYSTEM]
    assert [message.id for message in non_system if message.id is not None] == [1, 2, 4, 5]
    assert non_system[1].content == "I will write the file."
    assert non_system[1].tool_calls is None
    temporary_summary = non_system[2]
    assert temporary_summary.role == MessageRole.USER
    assert temporary_summary.content.startswith(
        RECENT_TOOL_SUMMARY_WRAPPER.format(
            from_message_id=2,
            through_message_id=3,
            content="",
        ).split("\n", 1)[0]
    )
    assert not any(message.role == MessageRole.TOOL for message in request)
    assert not any(message.tool_calls for message in request)


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
