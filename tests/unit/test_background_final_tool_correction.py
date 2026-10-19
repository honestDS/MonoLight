import json
from types import SimpleNamespace

import pytest

from app.core.constants import (
    END_SESSION_TOOL_NAME,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED,
    MANAGE_TODO_TOOL_NAME,
)
from app.core.dispatchers import background as background_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.exceptions import LLMException, LLMModelCapabilityException
from app.core.prompts import (
    BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT,
    BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT,
)
from app.core.tools.end_session import END_SESSION_TOOL_SCHEMA
from app.core.tools.send_file_to_user import SEND_FILE_TO_USER_TOOL_SCHEMA
from app.core.utils.assistant_files import parse_assistant_files_content
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole


def assert_rejected_feedback(request, tool_call_id, correction_prompt, *, expect_rejected=True):
    messages = request["messages"]
    tool_index = next(index for index, message in enumerate(messages) if message.role == MessageRole.TOOL and message.tool_call_id == tool_call_id)
    assert any(message.role == MessageRole.ASSISTANT and any(call.id == tool_call_id for call in message.tool_calls or []) for message in messages[:tool_index])
    feedback = json.loads(messages[tool_index].content)
    if expect_rejected:
        assert feedback["status"] == "rejected"
        assert feedback["executed"] is False
    assert correction_prompt in feedback["instruction"]


@pytest.mark.parametrize(
    ("outcome", "success_on_correction", "has_files", "repeated_tool_content"),
    [
        ("plain", 0, True, None),
        ("success", 1, True, None),
        ("success", 3, True, None),
        ("failure", 3, True, None),
        ("failure", 3, False, None),
        ("failure", 3, True, "已再次执行操作。"),
    ],
)
@pytest.mark.parametrize("goal_mode", [False, True])
@pytest.mark.asyncio
async def test_final_tool_call_is_corrected_to_text_without_user_visible_error(monkeypatch, outcome, success_on_correction, has_files, repeated_tool_content, goal_mode):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(
            max_parallel_tools=5,
            goal_mode=goal_mode,
            enabled_tools=["send_file_to_user"],
        ),
        security=SimpleNamespace(audit_channel_id=None, audit_model_id=None),
    )
    sent_file = {
        "id": "file-token",
        "name": "generated.png",
        "download_url": "/api/v1/download-sent?token=file-token",
        "previewable": True,
    }
    initial_tool_call = InternalToolCall(
        id="call-send-initial",
        name="send_file_to_user",
        arguments={"files": [{"path": "/tmp/generated.png"}]},
    )
    responses = [
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[initial_tool_call]),
            model="test-model",
        ),
    ]
    rejected_tool_calls = []
    if outcome == "plain":
        responses.append(
            InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=""),
                model="test-model",
            )
        )
    else:
        final_tool_call = InternalToolCall(
            id="call-send-final-0",
            name="send_file_to_user",
            arguments={"files": [{"path": "/tmp/forbidden-final.png"}]},
        )
        rejected_tool_calls.append(final_tool_call)
        responses.append(
            InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[final_tool_call]),
                model="test-model",
            )
        )
        correction_limit = success_on_correction if outcome == "success" else 3
        for correction_index in range(1, correction_limit + 1):
            if outcome == "success" and correction_index == success_on_correction:
                responses.append(
                    InternalResponse(
                        message=InternalMessage(role=MessageRole.ASSISTANT, content="图片已生成并发送。"),
                        model="test-model",
                    )
                )
                break
            repeated_tool_call = InternalToolCall(
                id=f"call-send-final-{correction_index}",
                name="send_file_to_user",
                arguments={"files": [{"path": "/tmp/forbidden-final.png"}]},
            )
            rejected_tool_calls.append(repeated_tool_call)
            response_content = repeated_tool_content if correction_index == 3 else None
            responses.append(
                InternalResponse(
                    message=InternalMessage(
                        role=MessageRole.ASSISTANT,
                        content=response_content,
                        tool_calls=[repeated_tool_call],
                    ),
                    model="test-model",
                )
            )

    requests = []
    tool_execution_calls = []
    saved_messages = []
    todo_snapshot = '<current_session_todo_snapshot>{"revision":2,"todos":[{"content":"finish","status":"in_progress"}]}</current_session_todo_snapshot>'
    original_user_content = "请生成图片"
    original_user_guidance = "原始渠道指导"
    original_platform_constraints = "仅限当前聊天渠道"
    original_user_environment = json.dumps(
        {"platform_constraints": original_platform_constraints},
        ensure_ascii=False,
    )
    original_user_message = InternalMessage(
        role=MessageRole.USER,
        content=original_user_content,
        guidance_prompt=original_user_guidance,
        environment_prompt=original_user_environment,
    )

    async def fake_get_user(_db, _uid):
        return SimpleNamespace(username="tester")

    async def fake_validate_profile_and_cfg(_db, _profile):
        return cfg

    async def fake_get_tools_for_profile(_db, _profile, allow_background):
        assert allow_background is False
        return (
            [
                SEND_FILE_TO_USER_TOOL_SCHEMA,
                {
                    "type": "function",
                    "function": {
                        "name": MANAGE_TODO_TOOL_NAME,
                        "description": "Todo",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                END_SESSION_TOOL_SCHEMA,
            ],
            None,
        )

    async def fake_prepare_messages(*_args, **_kwargs):
        return [original_user_message]

    async def fake_generate_chat_with_fallback(_db, **kwargs):
        chat_params = {
            "context_window_k": 128,
            "max_tokens": 256,
            "temperature": 0,
            "top_p": 1,
            "chat_timeout": 30,
        }
        chat_channel = SimpleNamespace(id=11)
        model_entry = {"model_id": "test-model", "protocol": "OPENAI"}
        request_builder = kwargs["request_builder"]
        request_messages = request_builder(chat_params, chat_channel, model_entry)
        if hasattr(request_messages, "__await__"):
            request_messages = await request_messages
        repeated_request_messages = request_builder(chat_params, chat_channel, model_entry)
        if hasattr(repeated_request_messages, "__await__"):
            repeated_request_messages = await repeated_request_messages

        def message_shape(messages):
            return [(message.role, message.content, message.tool_calls) for message in messages]

        assert message_shape(request_messages) == message_shape(repeated_request_messages)
        requests.append(
            {
                "tools": kwargs["tools"],
                "tool_choice": kwargs.get("tool_choice"),
                "messages": request_messages,
            }
        )
        return (
            responses.pop(0),
            SimpleNamespace(id=11),
            {"model_id": "test-model", "protocol": "OPENAI"},
            None,
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            },
        )

    async def fake_process_single_tool(*_args, **kwargs):
        tool_execution_calls.append(kwargs)
        assert kwargs["dispatch_mode"] == "background"
        assert kwargs["dispatch_source"] == "background_task"
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=initial_tool_call.id,
            content='{"status":"success"}',
        )

    async def fake_save(_db, _session_id, _uid, _profile_id, message, *, dedupe_key=None):
        saved_messages.append((message, dedupe_key))
        return None

    async def fake_save_tool_response(_db, _session_id, _uid, _profile_id, tool_response, messages, turn_messages):
        tool_response.id = 100 + len(turn_messages)
        messages.append(tool_response)
        turn_messages.append(tool_response)
        return tool_response

    async def fake_persist_todo_snapshot(_db, *, uid, session_id, tool_results):
        if tool_results:
            tool_results[-1].content = f"{tool_results[-1].content}\n\n{todo_snapshot}"
        return todo_snapshot

    monkeypatch.setattr(background_module.user_crud, "get_by_uid", fake_get_user)
    monkeypatch.setattr(background_module, "validate_profile_and_cfg", fake_validate_profile_and_cfg)
    monkeypatch.setattr(background_module, "get_tools_for_profile", fake_get_tools_for_profile)
    monkeypatch.setattr(background_module, "prepare_messages", fake_prepare_messages)
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", fake_generate_chat_with_fallback)
    monkeypatch.setattr(background_module, "process_single_tool_with_isolated_db", fake_process_single_tool)
    monkeypatch.setattr(background_module, "extract_files_to_user", lambda _responses: [sent_file] if has_files else [])
    monkeypatch.setattr(background_module, "save_assistant_message", fake_save)
    monkeypatch.setattr(background_module, "save_tool_response", fake_save_tool_response)
    monkeypatch.setattr(background_module, "persist_session_todo_snapshot_on_tool_results", fake_persist_todo_snapshot)

    async def generate_reply():
        return await BackgroundDispatcherMixin._generate_reply_from_history(
            object(),
            uid="u1",
            session_id="s1",
            profile=profile,
            call_context="background_task_proactive_reply",
            allow_tools=True,
            restrict_tools_to_background_allowlist=False,
        )

    if outcome == "failure":
        with pytest.raises(LLMModelCapabilityException) as exc_info:
            await generate_reply()
        assert exc_info.value.message == ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED
        assert not any(any(call.id in {rejected.id for rejected in rejected_tool_calls} for call in message.tool_calls or []) or (repeated_tool_content and repeated_tool_content in (message.content or "")) or "图片已生成并发送。" in (message.content or "") for message, _dedupe_key in saved_messages)
    else:
        final_msg, turn_messages, files = await generate_reply()
        expected_text = "图片已生成并发送。" if outcome == "success" else ""
        assert parse_assistant_files_content(final_msg.content) == expected_text
        assert final_msg.tool_calls in (None, [])
        assert files == ([sent_file] if has_files else [])
        if has_files:
            assert json.loads(final_msg.content)["files"] == [sent_file]
        assert not any(call.id in {rejected.id for rejected in rejected_tool_calls} for message in turn_messages for call in message.tool_calls or [])
        assert not any(message.role == MessageRole.USER for message in turn_messages)
        assert not any(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT in (message.content or "") for message in turn_messages)

    rejected_ids = {call.id for call in rejected_tool_calls}
    assert not any(any(call.id in rejected_ids for call in message.tool_calls or []) or BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT in (message.content or "") for message, _dedupe_key in saved_messages)

    assert len(tool_execution_calls) == 1
    expected_request_count = 2 if outcome == "plain" else 2 + (success_on_correction if outcome == "success" else 3)
    assert len(requests) == expected_request_count
    expected_tool_names = {"send_file_to_user"}
    for request in requests:
        assert request["tools"] is not None
        assert {tool["function"]["name"] for tool in request["tools"]} == expected_tool_names
        assert MANAGE_TODO_TOOL_NAME not in {tool["function"]["name"] for tool in request["tools"]}
        assert END_SESSION_TOOL_NAME not in {tool["function"]["name"] for tool in request["tools"]}
        assert request["tools"] == requests[0]["tools"]
        assert request["tool_choice"] == "auto"

    assert original_user_message.content == original_user_content
    assert original_user_message.guidance_prompt == original_user_guidance
    assert original_user_message.environment_prompt == original_user_environment

    for request_index, request in enumerate(requests):
        request_messages = request["messages"]
        assert request_messages[-1].role == MessageRole.USER
        request_payload = json.loads(request_messages[-1].content)
        assert request_payload["user_message"] is None
        assert request_payload["platform_constraints"] == original_platform_constraints
        platform_guidance = request_payload["platform_guidance"]
        assert platform_guidance.count(original_user_guidance) == 1
        assert platform_guidance.count(BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT) == 1
        expected_final_guidance_count = 0 if request_index == 0 else 1
        assert platform_guidance.count(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT) == expected_final_guidance_count
        if expected_final_guidance_count:
            assert platform_guidance.rstrip().endswith(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT)

    final_phase_requests = requests[1:]
    for request in final_phase_requests:
        execution_results = [message.content for message in request["messages"] if message.role == MessageRole.TOOL and message.tool_call_id == initial_tool_call.id]
        assert len(execution_results) == 1
        assert '{"status":"success"}' in execution_results[0]
        assert todo_snapshot in execution_results[0]

    for rejected_index, rejected_call in enumerate(rejected_tool_calls):
        correction_request_index = rejected_index + 2
        if correction_request_index < len(requests):
            assert_rejected_feedback(
                requests[correction_request_index],
                rejected_call.id,
                BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT,
            )


@pytest.mark.parametrize("allow_tools", [False, True])
@pytest.mark.parametrize("final_content", ["已根据现有上下文直接回复。", ""])
@pytest.mark.asyncio
async def test_reply_corrects_illegal_tool_calls_without_executing_or_persisting_them(monkeypatch, allow_tools, final_content):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(channel=SimpleNamespace(chat_channel=object()))
    initial_tool_call = InternalToolCall(
        id="call-illegal-initial",
        name="execute_shell",
        arguments={"command": "do not run"},
    )
    repeated_tool_call = InternalToolCall(
        id="call-illegal-corrected",
        name="execute_shell",
        arguments={"command": "still do not run"},
    )
    initial_message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[initial_tool_call])
    corrected_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="已根据现有上下文直接回复。",
        tool_calls=[repeated_tool_call],
    )
    responses = [
        InternalResponse(message=initial_message, model="test-model"),
        InternalResponse(message=corrected_message, model="test-model"),
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=final_content, tool_calls=[]),
            model="test-model",
        ),
    ]
    requests = []
    saved_messages = []
    tool_execution_called = False

    async def fake_get_user(_db, _uid):
        return SimpleNamespace(username="tester")

    async def fake_validate_profile_and_cfg(_db, _profile):
        return cfg

    async def fake_get_tools_for_profile(_db, _profile, allow_background):
        assert allow_background is False
        return (
            [
                SEND_FILE_TO_USER_TOOL_SCHEMA,
            ],
            None,
        )

    async def fake_prepare_messages(*_args, **_kwargs):
        return [InternalMessage(role=MessageRole.USER, content="请直接回答")]

    async def fake_generate_chat_with_fallback(_db, **kwargs):
        request_messages = kwargs["request_builder"](
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            },
            SimpleNamespace(id=11),
            {"model_id": "test-model", "protocol": "OPENAI"},
        )
        if hasattr(request_messages, "__await__"):
            request_messages = await request_messages
        requests.append(
            {
                "tools": kwargs["tools"],
                "tool_choice": kwargs.get("tool_choice"),
                "request_metadata_callback": kwargs.get("request_metadata_callback"),
                "messages": request_messages,
            }
        )
        return (
            responses.pop(0),
            SimpleNamespace(id=11),
            {"model_id": "test-model", "protocol": "OPENAI"},
            None,
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            },
        )

    async def fake_save(_db, _session_id, _uid, _profile_id, message, *, dedupe_key=None):
        saved_messages.append((message, dedupe_key))

    async def fake_process_single_tool(*_args, **_kwargs):
        nonlocal tool_execution_called
        tool_execution_called = True
        raise AssertionError("tools must not execute in this test")

    async def request_metadata_callback(_metadata):
        return None

    monkeypatch.setattr(background_module.user_crud, "get_by_uid", fake_get_user)
    monkeypatch.setattr(background_module, "validate_profile_and_cfg", fake_validate_profile_and_cfg)
    monkeypatch.setattr(background_module, "get_tools_for_profile", fake_get_tools_for_profile)
    monkeypatch.setattr(background_module, "prepare_messages", fake_prepare_messages)
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", fake_generate_chat_with_fallback)
    monkeypatch.setattr(background_module, "save_assistant_message", fake_save)
    monkeypatch.setattr(background_module, "process_single_tool_with_isolated_db", fake_process_single_tool)

    async def generate_reply():
        return await BackgroundDispatcherMixin._generate_reply_from_history(
            object(),
            uid="u1",
            session_id="s1",
            profile=profile,
            call_context="session_reply",
            allow_tools=allow_tools,
            final_message_dedupe_key="result-key",
            request_metadata_callback=request_metadata_callback,
        )

    if final_content:
        final_msg, turn_messages, files = await generate_reply()
    else:
        with pytest.raises(LLMException) as exc_info:
            await generate_reply()
        assert exc_info.value.message == ERR_LLM_EMPTY_RESPONSE

    assert len(requests) == 3
    for request_index, request in enumerate(requests):
        assert request["tools"] is not None
        assert {tool["function"]["name"] for tool in request["tools"]} == {"send_file_to_user"}
        assert request["tools"] == requests[0]["tools"]
        assert request["tool_choice"] == "auto"
        assert request["request_metadata_callback"] is request_metadata_callback
        request_messages = request["messages"]
        assert request_messages[-1].role == MessageRole.USER
        user_payloads = [json.loads(message.content) for message in request_messages if message.role == MessageRole.USER]
        assert sum(payload["user_message"] is None for payload in user_payloads) == 1
        request_payload = user_payloads[-1]
        assert request_payload["user_message"] is None
        platform_guidance = request_payload["platform_guidance"]
        assert platform_guidance.count(BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT) == 1
        expected_final_guidance_count = 1 if not allow_tools or request_index == 2 else 0
        assert platform_guidance.count(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT) == expected_final_guidance_count
        if expected_final_guidance_count:
            assert platform_guidance.rstrip().endswith(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT)

    if allow_tools:
        assert_rejected_feedback(requests[1], initial_tool_call.id, BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT, expect_rejected=False)
        assert_rejected_feedback(requests[2], repeated_tool_call.id, BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT, expect_rejected=False)
    else:
        assert_rejected_feedback(requests[1], initial_tool_call.id, BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT)
        assert_rejected_feedback(requests[2], repeated_tool_call.id, BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT)
    assert tool_execution_called is False

    if not final_content:
        assert saved_messages == []
        return

    assert final_msg.content == "已根据现有上下文直接回复。"
    assert final_msg.tool_calls == []
    assert turn_messages == [final_msg]
    assert files == []
    assert saved_messages == [(final_msg, "result-key")]
