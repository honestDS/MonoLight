from types import SimpleNamespace

import pytest

from app.core.dispatchers import background as background_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.prompts import BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole


@pytest.mark.parametrize(
    ("correction_succeeds", "has_files", "repeated_tool_content"),
    [
        (True, True, None),
        (False, True, None),
        (False, False, None),
        (False, True, "正在重新发送文件。"),
        (False, False, "已再次执行操作。"),
    ],
)
@pytest.mark.asyncio
async def test_final_tool_call_is_corrected_to_text_without_user_visible_error(monkeypatch, correction_succeeds, has_files, repeated_tool_content):
    profile = SimpleNamespace(id=1)
    cfg = SimpleNamespace(
        channel=SimpleNamespace(chat_channel=object()),
        tool=SimpleNamespace(max_parallel_tools=5),
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
    responses = [
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[initial_tool_call]),
            model="test-model",
        ),
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[repeated_tool_call]),
            model="test-model",
        ),
        InternalResponse(
            message=corrected_message,
            model="test-model",
        ),
    ]
    requests = []

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
        return [InternalMessage(role=MessageRole.USER, content="请生成图片")]

    async def fake_generate_chat_with_fallback(_db, **kwargs):
        request_messages = kwargs["request_builder"](
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            }
        )
        if hasattr(request_messages, "__await__"):
            request_messages = await request_messages
        requests.append(
            {
                "call_context": kwargs["call_context"],
                "tools": kwargs["tools"],
                "messages": request_messages,
            }
        )
        return (
            responses.pop(0),
            None,
            {},
            None,
            {
                "context_window_k": 128,
                "max_tokens": 256,
                "temperature": 0,
                "top_p": 1,
                "chat_timeout": 30,
            },
        )

    async def fake_process_single_tool(*_args, **_kwargs):
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=initial_tool_call.id,
            content='{"status":"success"}',
        )

    async def fake_save(*_args, **_kwargs):
        return None

    async def fake_save_tool_response(_db, _session_id, _uid, _profile_id, tool_response, messages, turn_messages):
        messages.append(tool_response)
        turn_messages.append(tool_response)

    monkeypatch.setattr(background_module.user_crud, "get_by_uid", fake_get_user)
    monkeypatch.setattr(background_module, "validate_profile_and_cfg", fake_validate_profile_and_cfg)
    monkeypatch.setattr(background_module, "get_tools_for_profile", fake_get_tools_for_profile)
    monkeypatch.setattr(background_module, "prepare_messages", fake_prepare_messages)
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", fake_generate_chat_with_fallback)
    monkeypatch.setattr(background_module, "process_single_tool_with_isolated_db", fake_process_single_tool)
    monkeypatch.setattr(background_module, "extract_files_to_user", lambda _responses: [sent_file] if has_files else [])
    monkeypatch.setattr(background_module, "save_assistant_message", fake_save)
    monkeypatch.setattr(background_module, "save_tool_response", fake_save_tool_response)

    final_msg, turn_messages, files = await BackgroundDispatcherMixin._generate_reply_from_history(
        object(),
        uid="u1",
        session_id="s1",
        profile=profile,
        call_context="background_task_proactive_reply",
        allow_tools=True,
    )

    assert final_msg.tool_calls in (None, [])
    if correction_succeeds:
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
    assert [request["call_context"] for request in requests] == [
        "background_task_proactive_reply",
        "background_task_proactive_reply_final",
        "background_task_proactive_reply_final_tool_correction",
    ]
    assert requests[1]["tools"] is None
    assert requests[2]["tools"] is None

    correction_messages = requests[2]["messages"]
    correction_tool_message = correction_messages[-1]
    assert correction_tool_message.role == MessageRole.TOOL
    assert correction_tool_message.tool_call_id == repeated_tool_call.id
    assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT in correction_tool_message.content
