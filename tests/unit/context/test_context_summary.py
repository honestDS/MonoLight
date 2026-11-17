from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.context import ContextManager
from app.core.prompts import (
    CONTEXT_SUMMARY_COMPRESS_PROMPT,
    CONTEXT_SUMMARY_PROMPT,
)
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
from app.core.utils.context_summary.stage import GeneratedSummaryResult
from app.core.utils.dispatcher import frozen_context as frozen_context_module
from app.models.message import (
    ImagePart,
    InternalConversationSummary,
    InternalMessage,
    InternalToolCall,
    MessageRole,
    StoredConversationSummary,
    TextPart,
)
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
        "at most one explicit next step",
        "completed",
        "failed",
        "unfinished",
        "tool arguments",
        "raw",
        "output",
        "intermediate process",
        "not a user instruction",
        "user_message",
        "never generate, quote, paraphrase, rewrite, or modify",
        "code tasks",
        "file paths and symbol names",
        "current structure and control/data flow",
        "intended insertion or modification points",
        "related call sites",
        "verification scope and results",
        "verified facts",
        "hypotheses",
        "the file is important",
        "non-code tasks",
        "not merely consulted sources",
        "status lists",
        "routine progress narration",
        "multi-step plans",
        "Remaining Work",
    ]

    for phrase in required_phrases:
        assert phrase in prompt


@pytest.mark.parametrize(
    "prompt",
    [CONTEXT_SUMMARY_PROMPT, CONTEXT_SUMMARY_COMPRESS_PROMPT],
)
def test_summary_prompts_use_current_output_sections(prompt):
    sections = [
        "## Goal & Requirements",
        "## Decisions & Constraints",
        "## Continuation-Critical Context",
        "## Verified Results",
        "## Remaining Work",
        "## Open Questions",
    ]

    assert [prompt.index(section) for section in sections] == sorted(prompt.index(section) for section in sections)
    assert "## Entities" not in prompt
    assert "## Progress & Unfinished" not in prompt


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


def test_frozen_compaction_segment_never_splits_pending_tool_chain():
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="request"),
        InternalMessage(
            id=2,
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-1", name="shell", arguments={"command": "echo first"})],
        ),
        InternalMessage(id=3, role=MessageRole.TOOL, tool_call_id="call-1", content="first result"),
        InternalMessage(
            id=4,
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-2", name="shell", arguments={"command": "echo pending"})],
        ),
    ]

    segment = frozen_context_module._select_frozen_compaction_segment(messages)

    assert [message.id for message in segment] == [1, 2, 3]
    assert segment[-1].role == MessageRole.TOOL


@pytest.mark.parametrize(
    ("raw_content", "attachments", "has_old_history"),
    [
        pytest.param(
            [
                TextPart(text="describe the latest image"),
                ImagePart(image_url={"url": "data:image/png;base64,latest"}),
            ],
            None,
            True,
            id="multimodal-user-with-completed-tool-round",
        ),
        pytest.param(
            "latest attached request",
            ["latest.pdf"],
            True,
            id="attachment-user-with-completed-tool-round",
        ),
        pytest.param(
            "unreplied raw input",
            None,
            False,
            id="unreplied-raw-user-without-history",
        ),
    ],
)
def test_frozen_compaction_segment_protects_latest_raw_user_boundary(raw_content, attachments, has_old_history):
    old_history = (
        [
            InternalMessage(id=1, role=MessageRole.USER, content="old question"),
            InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
            InternalMessage(id=3, role=MessageRole.USER, content="old tool request"),
            InternalMessage(
                id=4,
                role=MessageRole.ASSISTANT,
                tool_calls=[InternalToolCall(id="old-call", name="shell", arguments={"command": "echo old"})],
            ),
            InternalMessage(id=5, role=MessageRole.TOOL, tool_call_id="old-call", content="old result"),
        ]
        if has_old_history
        else []
    )
    raw_user = InternalMessage(
        id=6 if has_old_history else 1,
        role=MessageRole.USER,
        content=raw_content,
        attachments=attachments,
    )
    latest_tool_round = [
        InternalMessage(
            id=raw_user.id + 1,
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="latest-call", name="shell", arguments={"command": "echo latest"})],
        ),
        InternalMessage(
            id=raw_user.id + 2,
            role=MessageRole.TOOL,
            tool_call_id="latest-call",
            content="latest result",
        ),
    ]
    messages = [*old_history, raw_user, *latest_tool_round] if has_old_history else [raw_user]

    segment = frozen_context_module._select_frozen_compaction_segment(messages)
    retained = messages[len(segment) :]

    assert [message.id for message in segment] == ([1, 2, 3, 4, 5] if has_old_history else [])
    assert retained[0] == raw_user
    if has_old_history:
        assert retained[1:] == latest_tool_round
    else:
        assert retained == [raw_user]


@pytest.mark.asyncio
async def test_frozen_context_compaction_summarizes_completed_prefix_only(monkeypatch):
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(id=1, role=MessageRole.USER, content="request exact instruction"),
        InternalMessage(
            id=2,
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-1", name="shell", arguments={"command": "echo first"})],
        ),
        InternalMessage(id=3, role=MessageRole.TOOL, tool_call_id="call-1", content="result " + "y" * 6000),
        InternalMessage(
            id=4,
            role=MessageRole.ASSISTANT,
            tool_calls=[InternalToolCall(id="call-2", name="shell", arguments={"command": "echo pending"})],
        ),
    ]
    snapshots = []

    async def generate_snapshot_summary_result(_db, **kwargs):
        snapshots.append(kwargs["snapshot"])
        return GeneratedSummaryResult(
            content="compressed completed work",
            message_count=3,
            completed_stage=SimpleNamespace(),
        )

    async def noop_cleanup(*_args, **_kwargs):
        return None

    monkeypatch.setattr(frozen_context_module, "generate_snapshot_summary_result", generate_snapshot_summary_result)
    monkeypatch.setattr(frozen_context_module, "cleanup_context_summary_work_safely", noop_cleanup)

    def measure_request_tokens(request_messages):
        return sum(len(message.model_dump_json()) for message in request_messages)

    compacted = await frozen_context_module.compact_frozen_context_messages(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=1),
        cfg=SimpleNamespace(other=SimpleNamespace(context_summary_threshold_percent=90)),
        messages=messages,
        context_window_k=128,
        max_tokens=512,
        tools=[],
        measure_request_tokens=measure_request_tokens,
        force=True,
    )

    assert len(snapshots) == 1
    snapshot_tool_call_ids = [tool_call.id for round_messages in snapshots[0].frozen_rounds for message in round_messages for tool_call in message.tool_calls or []]
    assert "call-1" in snapshot_tool_call_ids
    assert "call-2" not in snapshot_tool_call_ids
    assert compacted[0].role == MessageRole.SYSTEM
    assert compacted[1].role == MessageRole.USER
    assert compacted[1].content == messages[1].content
    assert compacted[1].conversation_summary is not None
    assert compacted[1].conversation_summary.content == "compressed completed work"
    assert compacted[1].conversation_summary.through_message_id == 3
    assert compacted[1].conversation_summary.covered_user_message_id == 1
    assert compacted[2:] == messages[4:]
    covered_message_id, covered_content = frozen_context_module._covered_user_from_segment(compacted)
    assert covered_message_id == 1
    assert covered_content == messages[1].content


def test_serialize_message_supports_multimodal_content():
    message = InternalMessage(
        id=1,
        role=MessageRole.USER,
        content=[
            TextPart(text="describe"),
            ImagePart(image_url={"url": "data:image/png;base64,abc"}),
        ],
        attachments=["ignored.png"],
        assembled_attachment_part_count=1,
    )

    serialized = _serialize_message(message)

    assert '"type":"text"' in serialized
    assert '"type":"image_url"' in serialized
    assert "attachments" not in serialized
    assert "assembled_attachment_part_count" not in serialized
    assert "created_at" not in serialized


@pytest.mark.parametrize(
    ("content", "expected_content", "expected_summary", "expected_covered_message_id"),
    [
        pytest.param(
            StoredConversationSummary(conversation_summary="The user selected option A.").model_dump_json(exclude_none=True),
            None,
            "The user selected option A.",
            None,
            id="without-covered-user-message",
        ),
        pytest.param(
            StoredConversationSummary(
                conversation_summary="The user selected option A.",
                user_message="Please select option A.",
                covered_user_message_id=11,
            ).model_dump_json(exclude_none=True),
            "Please select option A.",
            "The user selected option A.",
            11,
            id="with-covered-user-message",
        ),
    ],
)
def test_summary_state_builds_bounded_user_message(
    content,
    expected_content,
    expected_summary,
    expected_covered_message_id,
):
    state = ContextSummaryState(content=content, message_id=12)

    message = state.as_message()

    assert message is not None
    assert message.role == MessageRole.USER
    assert message.content == expected_content
    assert message.conversation_summary is not None
    assert message.conversation_summary.through_message_id == 12
    assert message.conversation_summary.content == expected_summary
    assert message.conversation_summary.covered_user_message_id == expected_covered_message_id


def test_summary_request_validation_preserves_full_message_sequence():
    summary_message = InternalMessage(
        role=MessageRole.USER,
        content="old covered request",
        conversation_summary=InternalConversationSummary(
            through_message_id=20,
            content="compressed old turns",
            covered_user_message_id=19,
        ),
    )
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable prompt"),
        summary_message,
        InternalMessage(id=21, role=MessageRole.USER, content="old recent question " * 40),
        InternalMessage(id=22, role=MessageRole.ASSISTANT, content="old recent answer " * 40),
        InternalMessage(id=23, role=MessageRole.USER, content="latest question"),
    ]

    request = ContextManager.prepare_messages_for_model_request(
        messages=messages,
        context_window_k=20,
        max_tokens=256,
        safety_margin_tokens=64,
    )

    assert request[0].role == MessageRole.SYSTEM
    assert request[1].role == MessageRole.USER
    assert request[1] is not summary_message
    assert request[1].content == summary_message.content
    assert request[1].conversation_summary == summary_message.conversation_summary
    assert request[1].conversation_summary is not summary_message.conversation_summary
    assert [message.id for message in request if message.id is not None] == [21, 22, 23]
    assert request[-1].id == 23
    assert request[-1].content == "latest question"


def test_request_validation_does_not_reject_local_estimate_or_slide_history():
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

    original_ids = [message.id for message in messages]
    request = ContextManager.prepare_messages_for_model_request(
        messages=messages,
        context_window_k=1,
        max_tokens=256,
        safety_margin_tokens=64,
    )

    assert [message.id for message in request] == original_ids
    assert [message.id for message in messages] == original_ids


def test_request_validation_keeps_persisted_tool_chain_unchanged():
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
                    name="file_tool",
                    arguments={"operation": "write", "path": "generated.txt", "content": "file content " * 3000},
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

    request = ContextManager.prepare_messages_for_model_request(
        messages=messages,
        context_window_k=50,
        max_tokens=256,
        safety_margin_tokens=64,
    )

    non_system = [message for message in request if message.role != MessageRole.SYSTEM]
    assert [message.id for message in non_system if message.id is not None] == [1, 2, 3, 4, 5]
    assert non_system[1].tool_calls is not None
    assert non_system[1].tool_calls[0].id == "call-1"
    assert non_system[2].role == MessageRole.TOOL
    assert non_system[2].content == "file write result " * 1000


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
