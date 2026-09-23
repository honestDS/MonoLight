from app.core.constants import (
    MEMORY_RECALL_PRECHECK_HISTORY_USER_ROUNDS,
    MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    SESSION_TITLE_MAX_OUTPUT_TOKENS,
)
from app.core.dispatchers.memory.request import build_precheck_request_messages
from app.core.utils.llm_request_params import (
    build_context_summary_generation_params,
    build_memory_recall_precheck_generation_params,
    build_session_title_generation_params,
)
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import LongTermMemoryConfig


def test_precheck_request_keeps_summary_recent_history_and_current_user_only():
    summary = InternalMessage(role=MessageRole.USER, content='<conversation_summary through_message_id="10">older summary</conversation_summary>')
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        summary,
        InternalMessage(role=MessageRole.USER, content="old user"),
        InternalMessage(role=MessageRole.ASSISTANT, content="old answer"),
        InternalMessage(role=MessageRole.USER, content="recent user 1"),
        InternalMessage(role=MessageRole.ASSISTANT, content="recent answer 1"),
        InternalMessage(role=MessageRole.ASSISTANT, content="tool-assisted answer", reasoning_content="private reasoning", tool_calls=[InternalToolCall(id="call-1", name="tool", arguments={})]),
        InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content="tool result"),
        InternalMessage(role=MessageRole.USER, content="recent user 2"),
        InternalMessage(role=MessageRole.ASSISTANT, content="recent answer 2"),
        InternalMessage(role=MessageRole.USER, content="current user"),
    ]

    result = build_precheck_request_messages(messages)

    assert MEMORY_RECALL_PRECHECK_HISTORY_USER_ROUNDS == 2
    assert [message.content for message in result] == [
        summary.content,
        "recent user 1",
        "recent answer 1",
        "tool-assisted answer",
        "recent user 2",
        "recent answer 2",
        "current user",
    ]
    assert all(message.role != MessageRole.TOOL for message in result)
    assert all(not message.tool_calls for message in result)
    assert all(message.reasoning_content is None for message in result)


def test_memory_precheck_setting_defaults_on_and_can_be_disabled():
    assert LongTermMemoryConfig().precheck_enabled is True
    assert LongTermMemoryConfig(precheck_enabled=False).precheck_enabled is False


def test_precheck_generation_params_force_small_output_and_clean_optional_fields():
    params = build_memory_recall_precheck_generation_params(
        model_entry={"temperature": 0.8, "reasoning_effort": None},
        protocol="openai",
    )

    assert params == {
        "temperature": 0.0,
        "max_tokens": MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    }

    reasoning_params = build_memory_recall_precheck_generation_params(
        model_entry={"temperature": 0.8, "reasoning_effort": "high", "max_tokens": 128},
        protocol="openai_responses",
    )
    assert reasoning_params == {
        "reasoning_effort": "low",
        "max_tokens": 128,
    }

    chat_completions_params = build_memory_recall_precheck_generation_params(
        model_entry={"temperature": 0.8, "reasoning_effort": "high"},
        protocol="openai",
    )
    assert chat_completions_params == {
        "reasoning_effort": "low",
        "max_tokens": MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    }


def test_internal_task_generation_params_share_reasoning_cleanup_semantics():
    title_params = build_session_title_generation_params(
        model_entry={"reasoning_effort": "max", "max_tokens": 128},
        protocol="openai_responses",
    )
    assert title_params == {
        "reasoning_effort": "low",
        "max_tokens": 128,
    }

    plain_title_params = build_session_title_generation_params(
        model_entry={"reasoning_effort": None, "temperature": 0.35, "max_tokens": 20480},
        protocol="openai",
    )
    assert plain_title_params == {
        "temperature": 0.35,
        "max_tokens": SESSION_TITLE_MAX_OUTPUT_TOKENS,
    }

    summary_params = build_context_summary_generation_params(
        model_entry={"reasoning_effort": None, "temperature": 0.8, "max_tokens": 4096},
        protocol="openai_responses",
        max_output_tokens=1024,
    )
    assert summary_params == {
        "temperature": 0.8,
        "max_tokens": 1024,
    }


def test_internal_task_generation_params_preserve_disabled_reasoning_without_sampling_params():
    precheck_params = build_memory_recall_precheck_generation_params(
        model_entry={"reasoning_effort": "none", "temperature": 0.8},
        protocol="openai",
    )
    assert precheck_params == {
        "reasoning_effort": "none",
        "max_tokens": MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    }

    title_params = build_session_title_generation_params(
        model_entry={"reasoning_effort": " NONE ", "temperature": 0.35},
        protocol="openai_responses",
    )
    assert title_params == {
        "reasoning_effort": "none",
        "max_tokens": SESSION_TITLE_MAX_OUTPUT_TOKENS,
    }

    summary_params = build_context_summary_generation_params(
        model_entry={"reasoning_effort": "none", "temperature": 0.8},
        protocol="openai_responses",
        max_output_tokens=1024,
    )
    assert summary_params == {
        "reasoning_effort": "none",
        "max_tokens": 1024,
    }
