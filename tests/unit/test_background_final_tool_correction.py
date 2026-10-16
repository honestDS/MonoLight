import json
from types import SimpleNamespace

import pytest

from app.core.constants import END_SESSION_TOOL_NAME, MANAGE_TODO_TOOL_NAME
from app.core.dispatchers import background as background_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.prompts import (
    BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT,
    BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT,
    TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT,
)
from app.core.tools.end_session import END_SESSION_TOOL_SCHEMA
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole


@pytest.mark.parametrize(
    ("correction_succeeds", "has_files", "repeated_tool_content"),
    [
        (True, True, None),
        (False, True, None),
        (False, False, None),
        (False, True, "正在重新发送文件。"),
        (False, False, "已再次执行操作。"),
        (None, True, None),
    ],
)
@pytest.mark.parametrize("goal_mode", [False, True])
@pytest.mark.asyncio
async def test_final_tool_call_is_corrected_to_text_without_user_visible_error(monkeypatch, correction_succeeds, has_files, repeated_tool_content, goal_mode):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(max_parallel_tools=5, goal_mode=goal_mode),
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
    repeated_tool_call = InternalToolCall(
        id="call-send-repeated",
        name="send_file_to_user",
        arguments={"files": [{"path": "/tmp/generated.png"}]},
    )
    corrected_message = InternalMessage(role=MessageRole.ASSISTANT, content="图片已生成并发送。") if correction_succeeds else InternalMessage(role=MessageRole.ASSISTANT, content=repeated_tool_content, tool_calls=[repeated_tool_call])
    final_response_message = InternalMessage(role=MessageRole.ASSISTANT, content="") if correction_succeeds is None else InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[repeated_tool_call])
    responses = [
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[initial_tool_call]),
            model="test-model",
        ),
        InternalResponse(
            message=final_response_message,
            model="test-model",
        ),
        InternalResponse(
            message=corrected_message,
            model="test-model",
        ),
    ]
    requests = []
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
                {
                    "type": "function",
                    "function": {
                        "name": "send_file_to_user",
                        "description": "Send a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
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
                "call_context": kwargs["call_context"],
                "tools": kwargs["tools"],
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
        assert kwargs["dispatch_mode"] == "background"
        assert kwargs["dispatch_source"] == "background_task_proactive_reply"
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=initial_tool_call.id,
            content='{"status":"success"}',
        )

    async def fake_save(*_args, **_kwargs):
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

    final_msg, turn_messages, files = await BackgroundDispatcherMixin._generate_reply_from_history(
        object(),
        uid="u1",
        session_id="s1",
        profile=profile,
        call_context="background_task_proactive_reply",
        allow_tools=True,
        restrict_tools_to_background_allowlist=False,
    )

    assert final_msg.tool_calls in (None, [])
    if correction_succeeds is None:
        expected_text = ""
    elif correction_succeeds:
        expected_text = "图片已生成并发送。"
    elif has_files:
        expected_text = "后台任务已完成，但未能生成最终说明，请查看本回复附带的任务结果"
    else:
        expected_text = "后台任务已完成，但未能生成最终说明"
    assert expected_text in final_msg.content
    if repeated_tool_content:
        assert repeated_tool_content not in final_msg.content
    if not has_files:
        assert "附带的任务结果" not in final_msg.content
    assert "后台主动回复的最终结果不允许继续调用工具" not in final_msg.content
    assert files == ([sent_file] if has_files else [])
    assert len(turn_messages) == 3
    expected_contexts = [
        "background_task_proactive_reply",
        "background_task_proactive_reply_final",
    ]
    if correction_succeeds is not None:
        expected_contexts.append("background_task_proactive_reply_final_tool_correction")
    assert [request["call_context"] for request in requests] == expected_contexts
    first_request_tool_names = {tool["function"]["name"] for tool in requests[0]["tools"]}
    assert MANAGE_TODO_TOOL_NAME not in first_request_tool_names
    assert END_SESSION_TOOL_NAME not in first_request_tool_names
    assert requests[1]["tools"] is None
    assert todo_snapshot in next(message.content for message in requests[1]["messages"] if message.role == MessageRole.TOOL)
    assert original_user_message.content == original_user_content
    assert original_user_message.guidance_prompt == original_user_guidance
    assert original_user_message.environment_prompt == original_user_environment
    assert not any(message.role == MessageRole.USER for message in turn_messages)

    for request in requests:
        request_messages = request["messages"]
        assert request_messages[-1].role == MessageRole.USER
        request_payload = json.loads(request_messages[-1].content)
        assert request_payload["user_message"] is None
        assert request_payload["platform_constraints"] == original_platform_constraints
        platform_guidance = request_payload["platform_guidance"]
        assert platform_guidance.count(original_user_guidance) == 1
        assert platform_guidance.count(BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT) == 1
        assert platform_guidance.count(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT) == (1 if request["tools"] is None else 0)

    if correction_succeeds is not None:
        assert requests[2]["tools"] is None
        correction_messages = requests[2]["messages"]
        correction_tool_index = next(index for index, message in enumerate(correction_messages) if message.role == MessageRole.TOOL and message.tool_call_id == repeated_tool_call.id)
        assert any(message.role == MessageRole.ASSISTANT and any(tool_call.id == repeated_tool_call.id for tool_call in message.tool_calls or []) for message in correction_messages[:correction_tool_index])
        correction_tool_message = correction_messages[correction_tool_index]
        assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT in correction_tool_message.content


@pytest.mark.parametrize("allow_tools", [False, True])
@pytest.mark.asyncio
async def test_reply_corrects_illegal_tool_calls_without_executing_or_persisting_them(monkeypatch, allow_tools):
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
    ]
    if allow_tools:
        responses.append(
            InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="已根据现有上下文直接回复。", tool_calls=[]),
                model="test-model",
            )
        )
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
                {
                    "type": "function",
                    "function": {
                        "name": "send_file_to_user",
                        "description": "Send a file",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
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
                "call_context": kwargs["call_context"],
                "tools": kwargs["tools"],
                "require_content": kwargs.get("require_content"),
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

    final_msg, turn_messages, files = await BackgroundDispatcherMixin._generate_reply_from_history(
        object(),
        uid="u1",
        session_id="s1",
        profile=profile,
        call_context="session_reply",
        allow_tools=allow_tools,
        final_message_dedupe_key="result-key",
        request_metadata_callback=request_metadata_callback,
    )

    assert len(requests) == (3 if allow_tools else 2)
    for request in requests:
        if request["tools"] is None:
            expected_final_guidance_count = 1
        else:
            assert {tool["function"]["name"] for tool in request["tools"]} == {"send_file_to_user"}
            expected_final_guidance_count = 0
        assert request["request_metadata_callback"] is request_metadata_callback
        request_messages = request["messages"]
        assert request_messages[-1].role == MessageRole.USER
        user_payloads = [json.loads(message.content) for message in request_messages if message.role == MessageRole.USER]
        assert sum(payload["user_message"] is None for payload in user_payloads) == 1
        request_payload = user_payloads[-1]
        assert request_payload["user_message"] is None
        platform_guidance = request_payload["platform_guidance"]
        assert platform_guidance.count(BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT) == 1
        assert platform_guidance.count(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT) == expected_final_guidance_count
    if allow_tools:
        assert requests[0]["tools"] is not None
        assert requests[1]["tools"] is not None
        assert requests[2]["tools"] is None
        assert requests[2]["call_context"] == "session_reply_text_only"
    else:
        assert requests[0]["tools"] is None
        assert requests[1]["tools"] is None
    assert requests[1]["call_context"] == ("session_reply_tool_correction" if allow_tools else "session_reply_text_only_tool_correction")
    assert requests[1]["require_content"] is (None if allow_tools else True)
    assert requests[1]["request_metadata_callback"] is request_metadata_callback
    correction_messages = requests[1]["messages"]
    correction_tool_index = next(index for index, message in enumerate(correction_messages) if message.role == MessageRole.TOOL and message.tool_call_id == initial_tool_call.id)
    assert any(message.role == MessageRole.ASSISTANT and any(tool_call.id == initial_tool_call.id for tool_call in message.tool_calls or []) for message in correction_messages[:correction_tool_index])
    correction_tool_message = correction_messages[correction_tool_index]
    if allow_tools:
        assert BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT in correction_tool_message.content
    else:
        assert '"type": "background_proactive_tools_disabled_tool_correction"' in correction_tool_message.content
        assert '"error": "Tool calls are disabled for this reply."' in correction_tool_message.content
        assert TEXT_ONLY_REPLY_TOOL_CORRECTION_PROMPT in correction_tool_message.content
    if allow_tools:
        text_only_messages = requests[2]["messages"]
        repeated_tool_index = next(index for index, message in enumerate(text_only_messages) if message.role == MessageRole.TOOL and message.tool_call_id == repeated_tool_call.id)
        assert any(message.role == MessageRole.ASSISTANT and any(tool_call.id == repeated_tool_call.id for tool_call in message.tool_calls or []) for message in text_only_messages[:repeated_tool_index])
        repeated_tool_message = text_only_messages[repeated_tool_index]
        assert BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT in repeated_tool_message.content
    assert tool_execution_called is False
    assert final_msg.content == "已根据现有上下文直接回复。"
    assert final_msg.tool_calls == []
    assert turn_messages == [final_msg]
    assert files == []
    assert saved_messages == [(final_msg, "result-key")]
