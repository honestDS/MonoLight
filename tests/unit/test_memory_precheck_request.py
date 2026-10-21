import json

import pytest

from app.core.constants import DEFAULT_CHAT_MAX_TOKENS
from app.core.dispatchers.memory.request import (
    build_correction_messages,
    build_precheck_request_messages,
)
from app.core.prompts import LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT
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
    VoiceTextPart,
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


@pytest.mark.parametrize("reasoning_content", [None, "", "private reasoning"])
def test_precheck_request_keeps_recent_rounds_and_filters_runtime_metadata(reasoning_content):
    assistant_metadata = {
        "protocol": "openai_responses",
        "source": "response.output",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_123",
                "summary": [{"type": "summary_text", "text": "private reasoning"}],
            }
        ],
    }
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
            reasoning_content=reasoning_content,
            provider_metadata=assistant_metadata,
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
    original_messages = [message.model_dump(mode="json") for message in messages]

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
    assert mixed_assistant.reasoning_content == reasoning_content
    assert mixed_assistant.provider_metadata == messages[6].provider_metadata
    assert mixed_assistant.provider_metadata is not messages[6].provider_metadata
    assert mixed_assistant.provider_metadata["output"] is not messages[6].provider_metadata["output"]
    assert mixed_assistant.tool_calls is None
    assert [message.model_dump(mode="json") for message in messages] == original_messages

    mixed_assistant.provider_metadata["output"][0]["summary"][0]["text"] = "changed in precheck copy"
    assert messages[6].provider_metadata["output"][0]["summary"][0]["text"] == "private reasoning"
    assert [message.model_dump(mode="json") for message in messages] == original_messages


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


def test_precheck_request_projects_only_text_from_history_and_current_multimodal_content():
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
            AudioPart(format="wav", path="/private/current.wav", data="CURRENT_AUDIO_SECRET"),
            FilePart(path="current.pdf"),
        ],
    )
    messages = [
        InternalMessage(role=MessageRole.USER, content=history_content),
        InternalMessage(role=MessageRole.ASSISTANT, content="history answer"),
        current,
    ]
    original_messages = [message.model_dump(mode="json") for message in messages]

    result = build_precheck_request_messages(messages)

    assert [(message.role, message.content) for message in result] == [
        (MessageRole.USER, "searchable history text"),
        (MessageRole.ASSISTANT, "history answer"),
        (MessageRole.USER, "current text"),
    ]
    assert [message.model_dump(mode="json") for message in messages] == original_messages
    assert history_content[1].image_url == {"url": "data:image/png;base64,HISTORY_SECRET"}
    assert current.content[1].image_url == {"url": "data:image/png;base64,CURRENT_SECRET"}


@pytest.mark.parametrize(
    "current_content",
    [
        None,
        "",
        " \t\n",
        [],
        [ImagePart(image_url={"url": "data:image/png;base64,ONLY_IMAGE"})],
        [TextPart(text=" \t "), ImagePart(image_url={"url": "data:image/png;base64,WHITESPACE_IMAGE"})],
    ],
    ids=["none", "empty", "whitespace", "empty-list", "image-only", "whitespace-text-and-image"],
)
@pytest.mark.parametrize(
    "todo_snapshot",
    [None, _todo_snapshot(8, "parameter todo task", "pending")],
    ids=["tool-embedded-todo", "todo-snapshot-parameter"],
)
def test_precheck_request_returns_empty_when_current_user_has_no_real_text(current_content, todo_snapshot):
    embedded_todo = _todo_snapshot(7, "tool todo task", "in_progress")
    messages = [
        InternalMessage(
            role=MessageRole.USER,
            content='<conversation_summary through_message_id="42">summary with useful text</conversation_summary>',
        ),
        InternalMessage(role=MessageRole.USER, content="older user text must not replace current input"),
        InternalMessage(role=MessageRole.ASSISTANT, content="older assistant text"),
        InternalMessage(role=MessageRole.TOOL, content=f"tool result\n\n{embedded_todo}"),
        InternalMessage(
            role=MessageRole.USER,
            content=current_content,
            environment_prompt="environment text must not become user input",
            guidance_prompt="guidance text must not become user input",
        ),
    ]

    result = build_precheck_request_messages(messages, todo_snapshot=todo_snapshot)

    assert result == []


@pytest.mark.parametrize(
    ("user_parts", "expected_text"),
    [
        (
            [
                TextPart(text="real user text"),
                ImagePart(image_url={"url": "data:image/png;base64,USER_IMAGE"}),
            ],
            "real user text",
        ),
        ([], None),
    ],
    ids=["retains-user-text", "attachments-only"],
)
def test_precheck_request_omits_assembled_attachment_tail(user_parts, expected_text):
    attachment_tail = [
        TextPart(text="generated attachment details"),
        ImagePart(image_url={"url": "data:image/png;base64,GENERATED_IMAGE"}),
    ]
    current = InternalMessage(
        role=MessageRole.USER,
        content=[*user_parts, *attachment_tail],
        attachments=["generated-image", "generated-file"],
        assembled_attachment_part_count=len(attachment_tail),
    )
    original_current = current.model_dump(mode="json")

    result = build_precheck_request_messages([current])

    if expected_text is None:
        assert result == []
    else:
        assert [(message.role, message.content) for message in result] == [(MessageRole.USER, expected_text)]
    assert current.model_dump(mode="json") == original_current


def test_precheck_request_keeps_original_content_when_attachment_part_count_is_zero():
    current = InternalMessage(
        role=MessageRole.USER,
        content=[
            ImagePart(image_url={"url": "data:image/png;base64,ORIGINAL_IMAGE"}),
            TextPart(text="original user text"),
        ],
        attachments=["external-attachment.pdf"],
        assembled_attachment_part_count=0,
    )
    original_current = current.model_dump(mode="json")

    result = build_precheck_request_messages([current])

    assert [(message.role, message.content) for message in result] == [(MessageRole.USER, "original user text")]
    assert current.model_dump(mode="json") == original_current


def test_precheck_request_uses_attachment_count_for_legacy_messages_without_part_count():
    current = InternalMessage(
        role=MessageRole.USER,
        content=[
            TextPart(text="user-authored text"),
            TextPart(text="arbitrary generated file text"),
            ImagePart(image_url={"url": "data:image/png;base64,LEGACY_ATTACHMENT"}),
        ],
        attachments=["legacy-text-file", "legacy-image-file"],
        assembled_attachment_part_count=None,
    )
    original_current = current.model_dump(mode="json")

    result = build_precheck_request_messages([current])

    assert [(message.role, message.content) for message in result] == [(MessageRole.USER, "user-authored text")]
    assert current.model_dump(mode="json") == original_current


@pytest.mark.parametrize("text", ["[图片]", "[系统提示,此处不是用户说的话]", "未开启图像理解无法解析图片"])
@pytest.mark.parametrize("as_text_part", [False, True], ids=["string", "text-part"])
def test_precheck_request_preserves_user_text_that_looks_like_a_placeholder(text, as_text_part):
    content = [TextPart(text=text)] if as_text_part else text
    current = InternalMessage(role=MessageRole.USER, content=content)
    original_current = current.model_dump(mode="json")

    result = build_precheck_request_messages([current])

    assert [(message.role, message.content) for message in result] == [(MessageRole.USER, text)]
    assert current.model_dump(mode="json") == original_current


def test_precheck_request_preserves_transcribed_voice_text():
    current = InternalMessage(
        role=MessageRole.USER,
        content=[VoiceTextPart(text="transcribed voice text", input_source="voice")],
    )
    original_current = current.model_dump(mode="json")

    result = build_precheck_request_messages([current])

    assert [(message.role, message.content) for message in result] == [(MessageRole.USER, "transcribed voice text")]
    assert current.model_dump(mode="json") == original_current


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


@pytest.mark.parametrize(
    "content",
    [None, "", " \t\n", []],
    ids=["none", "empty", "whitespace", "empty-list"],
)
@pytest.mark.parametrize(
    "reasoning_content",
    [None, "", "private reasoning"],
    ids=["no-reasoning", "empty-reasoning", "reasoning-only"],
)
def test_build_correction_messages_omits_responses_without_visible_content(content, reasoning_content):
    base_messages = [
        InternalMessage(
            role=MessageRole.USER,
            content="current user",
            provider_metadata={"nested": {"value": "unchanged"}},
        ),
        InternalMessage(role=MessageRole.ASSISTANT, content="precheck context"),
    ]
    response = InternalResponse(
        model="test-model",
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            reasoning_content=reasoning_content,
            provider_metadata={"provider": "test", "reasoning": {"text": "private reasoning"}},
        ),
    )
    original_base = [message.model_dump(mode="json") for message in base_messages]
    original_response = response.model_dump(mode="json")

    result = build_correction_messages(base_messages, response)

    assert result[:-1] == base_messages
    assert len(result) == len(base_messages) + 1
    assert result[-1].role == MessageRole.USER
    assert result[-1].content == LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT
    assert all(actual is not original for actual, original in zip(result, base_messages))
    assert result[0].provider_metadata is not base_messages[0].provider_metadata
    assert result[0].provider_metadata["nested"] is not base_messages[0].provider_metadata["nested"]
    assert [message.model_dump(mode="json") for message in base_messages] == original_base
    assert response.model_dump(mode="json") == original_response


def test_build_correction_messages_keeps_visible_response_without_tool_calls():
    base_messages = [InternalMessage(role=MessageRole.USER, content="current user")]
    response = InternalResponse(
        model="test-model",
        message=InternalMessage(role=MessageRole.ASSISTANT, content="visible response"),
    )
    original_base = [message.model_dump(mode="json") for message in base_messages]
    original_response = response.model_dump(mode="json")

    result = build_correction_messages(base_messages, response)

    assert [(message.role, message.content) for message in result] == [
        (MessageRole.USER, "current user"),
        (MessageRole.ASSISTANT, "visible response"),
        (MessageRole.USER, LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT),
    ]
    assert result[1] is not response.message
    assert [message.model_dump(mode="json") for message in base_messages] == original_base
    assert response.model_dump(mode="json") == original_response


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
    original_response = response.model_dump(mode="json")

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
    assert response.model_dump(mode="json") == original_response
    assert response.message.tool_calls[0].arguments == {"secret": "private"}


def test_memory_precheck_setting_defaults_on_and_can_be_disabled():
    assert LongTermMemoryConfig().precheck_enabled is True
    assert LongTermMemoryConfig(precheck_enabled=False).precheck_enabled is False


@pytest.mark.parametrize(
    ("builder", "temperature"),
    [
        (build_session_title_generation_params, 0.35),
        (build_memory_recall_precheck_generation_params, 0.0),
    ],
    ids=["session-title", "memory-precheck"],
)
@pytest.mark.parametrize("protocol", ["openai", "openai_responses", "deepseek", "deepseek_responses"])
@pytest.mark.parametrize(
    ("include_max_tokens", "configured_max_tokens", "expected_max_tokens"),
    [
        (True, 1024, 1024),
        (True, 128, 128),
        (True, 0, 0),
        (True, None, DEFAULT_CHAT_MAX_TOKENS),
        (False, None, DEFAULT_CHAT_MAX_TOKENS),
    ],
    ids=["above-legacy-limits", "configured-small", "explicit-zero", "none-fallback", "missing-fallback"],
)
def test_title_and_precheck_generation_params_respect_model_token_budget(builder, temperature, protocol, include_max_tokens, configured_max_tokens, expected_max_tokens):
    model_entry = {"temperature": 0.35, "reasoning_effort": None}
    if include_max_tokens:
        model_entry["max_tokens"] = configured_max_tokens
    original_model_entry = model_entry.copy()

    params = builder(model_entry=model_entry, protocol=protocol)

    assert params == {"temperature": temperature, "max_tokens": expected_max_tokens}
    assert model_entry == original_model_entry


@pytest.mark.parametrize(
    ("builder", "temperature"),
    [
        (build_session_title_generation_params, 0.35),
        (build_memory_recall_precheck_generation_params, 0.0),
    ],
    ids=["session-title", "memory-precheck"],
)
@pytest.mark.parametrize("protocol", ["openai", "openai_responses", "deepseek", "deepseek_responses"])
@pytest.mark.parametrize("reasoning_effort", [None, "none", "low", "max"], ids=["unset", "none", "low", "max"])
def test_title_and_precheck_generation_params_preserve_configured_reasoning_effort(builder, temperature, protocol, reasoning_effort):
    model_entry = {
        "reasoning_effort": reasoning_effort,
        "temperature": 0.35,
        "max_tokens": 1024,
    }
    original_model_entry = model_entry.copy()

    params = builder(model_entry=model_entry, protocol=protocol)

    expected_params = {"max_tokens": 1024}
    if reasoning_effort is not None:
        expected_params["reasoning_effort"] = reasoning_effort
    if reasoning_effort is None or protocol not in {"openai", "openai_responses"}:
        expected_params["temperature"] = temperature
    assert params == expected_params
    assert model_entry == original_model_entry


@pytest.mark.parametrize("protocol", ["openai", "openai_responses", "deepseek", "deepseek_responses"])
@pytest.mark.parametrize("reasoning_effort", ["low", "none", None], ids=["low", "none", "unset"])
def test_context_summary_generation_params_preserve_configured_reasoning_effort(protocol, reasoning_effort):
    params = build_context_summary_generation_params(
        model_entry={
            "reasoning_effort": reasoning_effort,
            "temperature": 0.8,
            "max_tokens": 20480,
        },
        protocol=protocol,
        max_output_tokens=20480,
    )

    assert params["max_tokens"] == 20480
    assert "top_p" not in params
    if reasoning_effort is None:
        assert "reasoning_effort" not in params
    else:
        assert params["reasoning_effort"] == reasoning_effort

    if protocol in {"openai", "openai_responses"} and reasoning_effort is not None:
        assert "temperature" not in params
    else:
        assert params["temperature"] == 0.8
