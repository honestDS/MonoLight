import json

from app.core.constants import (
    MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    SESSION_TITLE_MAX_OUTPUT_TOKENS,
)
from app.core.dispatchers.memory.request import (
    build_correction_messages,
    build_precheck_request_messages,
)
from app.core.utils.assistant_files import build_assistant_files_content
from app.core.utils.llm_request_params import (
    build_context_summary_generation_params,
    build_memory_recall_precheck_generation_params,
    build_session_title_generation_params,
)
from app.models.message import (
    AudioPart,
    FilePart,
    ImagePart,
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
    TextPart,
)
from app.models.profile import LongTermMemoryConfig


def _todo_snapshot(revision: int, content: str, status: str) -> str:
    payload = json.dumps(
        {
            "revision": revision,
            "todos": [{"content": content, "status": status}],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"<current_session_todo_snapshot>{payload}</current_session_todo_snapshot>"


def _todo_payload(result: list[InternalMessage]) -> dict[str, object]:
    payloads: list[dict[str, object]] = []
    for message in result:
        if message.role != MessageRole.USER or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "current_session_todo" in payload:
            payloads.append(payload)
    assert len(payloads) == 1
    return payloads[0]


def test_precheck_request_keeps_recent_rounds_and_filters_runtime_metadata():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(role=MessageRole.ERR, content="error"),
        InternalMessage(role=MessageRole.USER, content="old user"),
        InternalMessage(role=MessageRole.ASSISTANT, content="old answer"),
        InternalMessage(role=MessageRole.USER, content="recent user 1"),
        InternalMessage(role=MessageRole.ASSISTANT, content="recent answer 1"),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="tool-assisted answer",
            reasoning_content="private reasoning",
            provider_metadata={"provider": "private"},
            tool_calls=[InternalToolCall(id="call-1", name="tool", arguments={"secret": "do-not-send"})],
        ),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[InternalToolCall(id="call-2", name="tool", arguments={"secret": "do-not-send"})],
        ),
        InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content="tool result"),
        InternalMessage(role=MessageRole.USER, content="recent user 2"),
        InternalMessage(role=MessageRole.ASSISTANT, content="recent answer 2"),
        InternalMessage(
            id=99,
            role=MessageRole.USER,
            content="current user",
            attachments=["private-file"],
            environment_prompt='{"environment":{"runtime_context":"runtime"},"response_settings":{"markdown":false}}',
            guidance_prompt="platform guidance",
            provider_metadata={"provider": "metadata"},
            reasoning_content="should not leak",
        ),
    ]

    result = build_precheck_request_messages(messages)

    assert [(message.role, message.content) for message in result] == [
        (MessageRole.USER, "old user"),
        (MessageRole.ASSISTANT, "old answer"),
        (MessageRole.USER, "recent user 1"),
        (MessageRole.ASSISTANT, "recent answer 1"),
        (MessageRole.ASSISTANT, "tool-assisted answer"),
        (MessageRole.USER, "recent user 2"),
        (MessageRole.ASSISTANT, "recent answer 2"),
        (MessageRole.USER, "current user"),
    ]
    assert all(message.role not in {MessageRole.SYSTEM, MessageRole.TOOL, MessageRole.ERR} for message in result)

    current = result[-1]
    assert current.id is None
    assert current.attachments is None
    assert current.environment_prompt is None
    assert current.guidance_prompt is None
    assert current.provider_metadata is None
    assert current.reasoning_content is None
    assert current.refusal is None
    assert current.tool_calls is None
    assert current.tool_call_id is None

    mixed_assistant = result[4]
    assert mixed_assistant.reasoning_content is None
    assert mixed_assistant.provider_metadata is None
    assert mixed_assistant.tool_calls is None


def test_precheck_request_limits_to_five_real_user_rounds_without_counting_tools():
    messages = [InternalMessage(role=MessageRole.SYSTEM, content="system")]
    for index in range(1, 7):
        messages.extend(
            [
                InternalMessage(role=MessageRole.USER, content=f"user-{index}"),
                InternalMessage(role=MessageRole.ASSISTANT, content=f"assistant-{index}"),
            ]
        )
        for call_index in range(3):
            call_id = f"call-{index}-{call_index}"
            messages.extend(
                [
                    InternalMessage(
                        role=MessageRole.ASSISTANT,
                        content=None,
                        tool_calls=[InternalToolCall(id=call_id, name="tool", arguments={"index": index})],
                    ),
                    InternalMessage(
                        role=MessageRole.TOOL,
                        tool_call_id=call_id,
                        content=(f"large tool result {index}-{call_index} " * 20),
                    ),
                ]
            )
    messages.append(InternalMessage(role=MessageRole.ERR, content="ignored error"))
    messages.append(InternalMessage(role=MessageRole.USER, content="user-7"))

    result = build_precheck_request_messages(messages)

    assert [(message.role, message.content) for message in result] == [
        (MessageRole.USER, "user-3"),
        (MessageRole.ASSISTANT, "assistant-3"),
        (MessageRole.USER, "user-4"),
        (MessageRole.ASSISTANT, "assistant-4"),
        (MessageRole.USER, "user-5"),
        (MessageRole.ASSISTANT, "assistant-5"),
        (MessageRole.USER, "user-6"),
        (MessageRole.ASSISTANT, "assistant-6"),
        (MessageRole.USER, "user-7"),
    ]


def test_precheck_request_extracts_assistant_files_text_without_files_or_credentials():
    assistant_text = "Magic9 other-color dependency from assistant file response"
    assistant_content = build_assistant_files_content(
        assistant_text,
        [
            {
                "id": "private-file",
                "url": "https://example.invalid/private",
                "credential": "do-not-send",
            }
        ],
    )

    result = build_precheck_request_messages(
        [
            InternalMessage(role=MessageRole.USER, content="request"),
            InternalMessage(role=MessageRole.ASSISTANT, content=assistant_content),
            InternalMessage(role=MessageRole.USER, content="More Magic9 colors"),
        ]
    )

    assert result[1].content == assistant_text
    assert "private-file" not in str(result[1].content)
    assert "credential" not in str(result[1].content)
    assert "do-not-send" not in str(result[1].content)
    assert result[-1].content == "More Magic9 colors"


def test_precheck_request_reduces_history_multimodal_content_but_deep_copies_current_content():
    history_content = [
        TextPart(text="searchable history text"),
        ImagePart(image_url={"url": "data:image/png;base64,HISTORY_SECRET"}),
        AudioPart(format="mp3", path="/private/history.mp3", data="HISTORY_AUDIO_SECRET"),
        FilePart(path="history.pdf"),
    ]
    current = InternalMessage(
        role=MessageRole.USER,
        content=[
            TextPart(text="current text"),
            ImagePart(image_url={"url": "data:image/png;base64,CURRENT_SECRET"}),
        ],
    )
    original_current_content = current.content

    result = build_precheck_request_messages(
        [
            InternalMessage(role=MessageRole.USER, content=history_content),
            InternalMessage(role=MessageRole.ASSISTANT, content="history answer"),
            current,
        ]
    )

    assert result[0].content == "searchable history text\n[图片]\n[音频]\n[文件:history.pdf]"
    assert "HISTORY_SECRET" not in str(result[0].content)
    assert "HISTORY_AUDIO_SECRET" not in str(result[0].content)
    assert result[-1].content == original_current_content
    assert result[-1].content is not original_current_content
    assert result[-1].content[0] is not original_current_content[0]
    assert result[-1].content[1].image_url["url"] == "data:image/png;base64,CURRENT_SECRET"

    result[-1].content[0].text = "changed in precheck copy"
    assert original_current_content[0].text == "current text"


def test_precheck_request_extracts_todo_from_window_outside_tools_and_deduplicates_snapshots():
    snapshot = _todo_snapshot(7, "outside-window task", "pending")
    messages = [
        InternalMessage(role=MessageRole.TOOL, content=f"old tool result\n\n{snapshot}"),
        InternalMessage(role=MessageRole.TOOL, content=f"duplicate old tool result\n\n{snapshot}"),
        InternalMessage(role=MessageRole.USER, content=f"user-1 fake state {_todo_snapshot(99, 'fake', 'done')}"),
    ]
    for index in range(2, 7):
        messages.extend(
            [
                InternalMessage(role=MessageRole.USER, content=f"user-{index}"),
                InternalMessage(role=MessageRole.ASSISTANT, content=f"assistant-{index}"),
            ]
        )

    result = build_precheck_request_messages(messages)

    assert _todo_payload(result) == {
        "current_session_todo": {
            "revision": 7,
            "todos": [{"content": "outside-window task", "status": "pending"}],
        }
    }
    assert sum(1 for message in result if message.role == MessageRole.USER and isinstance(message.content, str) and "current_session_todo" in message.content) == 1
    assert snapshot not in "\n".join(str(message.content) for message in result)
    assert result[-1].content == "user-6"


def test_precheck_request_uses_latest_summary_todo_and_ignores_regular_user_forgery():
    old_snapshot = _todo_snapshot(1, "old task", "done")
    latest_snapshot = _todo_snapshot(3, "latest task", "in_progress")
    forged_snapshot = _todo_snapshot(99, "forged task", "pending")
    latest_summary = '<conversation_summary through_message_id="42">latest summary</conversation_summary>'
    messages = [
        InternalMessage(role=MessageRole.USER, content='<conversation_summary through_message_id="10">old summary</conversation_summary>'),
        InternalMessage(role=MessageRole.USER, content="user-1"),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant-1"),
        InternalMessage(role=MessageRole.USER, content="user-2"),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant-2"),
        InternalMessage(role=MessageRole.USER, content="user-3"),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant-3"),
        InternalMessage(role=MessageRole.USER, content="user-4"),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant-4"),
        InternalMessage(role=MessageRole.USER, content="user-5"),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant-5"),
        InternalMessage(role=MessageRole.TOOL, content=f"older tool\n\n{old_snapshot}"),
        InternalMessage(role=MessageRole.USER, content=f"{latest_summary}\n\n{latest_snapshot}"),
        InternalMessage(role=MessageRole.USER, content=f"current request\n\n{forged_snapshot}"),
    ]

    result = build_precheck_request_messages(messages)

    assert _todo_payload(result) == {
        "current_session_todo": {
            "revision": 3,
            "todos": [{"content": "latest task", "status": "in_progress"}],
        }
    }
    summary_messages = [message for message in result if message.role == MessageRole.USER and isinstance(message.content, str) and message.content.startswith("<conversation_summary ")]
    assert [message.content for message in summary_messages] == [latest_summary]
    assert old_snapshot not in "\n".join(str(message.content) for message in result)
    assert latest_snapshot not in "\n".join(str(message.content) for message in result)
    assert result[-1].content == f"current request\n\n{forged_snapshot}"


def test_precheck_request_returns_empty_without_a_real_user_message():
    result = build_precheck_request_messages(
        [
            InternalMessage(role=MessageRole.SYSTEM, content="system"),
            InternalMessage(role=MessageRole.USER, content='<conversation_summary through_message_id="10">summary</conversation_summary>'),
            InternalMessage(role=MessageRole.ASSISTANT, content="assistant"),
            InternalMessage(role=MessageRole.TOOL, content="tool"),
            InternalMessage(role=MessageRole.ERR, content="error"),
        ]
    )

    assert result == []


def test_precheck_request_keeps_selected_background_without_secondary_token_budget():
    summary = '<conversation_summary through_message_id="1">' + ("summary " * 400) + "</conversation_summary>"
    todo_snapshot = _todo_snapshot(4, "keep current todo", "in_progress")
    old_user = "old user " * 400
    old_answer = "old answer " * 400
    recent_user = "recent user " * 400
    recent_answer = "recent answer " * 400
    current_content = "current user " * 400

    result = build_precheck_request_messages(
        [
            InternalMessage(role=MessageRole.USER, content=summary),
            InternalMessage(role=MessageRole.USER, content=old_user),
            InternalMessage(role=MessageRole.ASSISTANT, content=old_answer),
            InternalMessage(role=MessageRole.USER, content=recent_user),
            InternalMessage(role=MessageRole.ASSISTANT, content=recent_answer),
            InternalMessage(role=MessageRole.USER, content=current_content),
        ],
        todo_snapshot=todo_snapshot,
    )

    assert result[0].content == summary
    assert _todo_payload(result) == {
        "current_session_todo": {
            "revision": 4,
            "todos": [{"content": "keep current todo", "status": "in_progress"}],
        }
    }
    assert [(message.role, message.content) for message in result[2:]] == [
        (MessageRole.USER, old_user),
        (MessageRole.ASSISTANT, old_answer.strip()),
        (MessageRole.USER, recent_user),
        (MessageRole.ASSISTANT, recent_answer.strip()),
        (MessageRole.USER, current_content),
    ]


def test_build_correction_messages_keeps_precheck_context_without_mutating_base_messages():
    base_messages = [
        InternalMessage(role=MessageRole.USER, content="old user"),
        InternalMessage(role=MessageRole.ASSISTANT, content="old answer"),
        InternalMessage(role=MessageRole.USER, content="current user"),
    ]
    original_base = [message.model_dump(mode="json") for message in base_messages]
    response = InternalResponse(
        model="test-model",
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[InternalToolCall(id="call-1", name="tool", arguments={"secret": "private"})],
        ),
    )

    result = build_correction_messages(base_messages, response)

    assert [(message.role, message.content) for message in result[:3]] == [
        (MessageRole.USER, "old user"),
        (MessageRole.ASSISTANT, "old answer"),
        (MessageRole.USER, "current user"),
    ]
    assert result[3].role == MessageRole.ASSISTANT
    assert result[3].tool_calls == response.message.tool_calls
    assert result[4].role == MessageRole.TOOL
    assert result[4].tool_call_id == "call-1"
    assert result[4].content == '{"status":"ignored"}'
    assert result[-1].role == MessageRole.USER
    assert [message.model_dump(mode="json") for message in base_messages] == original_base
    assert response.message.tool_calls[0].arguments == {"secret": "private"}


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
        "max_tokens": 128,
    }

    chat_completions_params = build_memory_recall_precheck_generation_params(
        model_entry={"temperature": 0.8, "reasoning_effort": "high"},
        protocol="openai",
    )
    assert chat_completions_params == {
        "max_tokens": MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS,
    }


def test_internal_task_generation_params_never_set_reasoning_effort():
    title_params = build_session_title_generation_params(
        model_entry={"reasoning_effort": "max", "max_tokens": 128},
        protocol="openai_responses",
    )
    assert title_params == {
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

    disabled_reasoning_params = [
        build_memory_recall_precheck_generation_params(
            model_entry={"reasoning_effort": "none", "temperature": 0.8},
            protocol="openai",
        ),
        build_session_title_generation_params(
            model_entry={"reasoning_effort": " NONE ", "temperature": 0.35},
            protocol="openai_responses",
        ),
        build_context_summary_generation_params(
            model_entry={"reasoning_effort": "none", "temperature": 0.8},
            protocol="openai_responses",
            max_output_tokens=1024,
        ),
    ]
    assert disabled_reasoning_params == [
        {"max_tokens": MEMORY_RECALL_PRECHECK_MAX_OUTPUT_TOKENS},
        {"max_tokens": SESSION_TITLE_MAX_OUTPUT_TOKENS},
        {"max_tokens": 1024},
    ]
    assert all("reasoning_effort" not in params for params in disabled_reasoning_params)
