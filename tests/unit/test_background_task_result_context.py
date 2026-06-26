import json

from app.core.background_tasks.reply_trigger import _build_background_tool_result_message
from app.core.context import ContextManager
from app.core.utils.dispatcher.inject_system_prompt import inject_system_prompt_text
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType


class DummyTask:
    id = 7
    tool_call_id = "call_bg_1"
    tool_name = "execute_shell"
    arguments = {"command": "echo hi"}
    result = {"status": "succeeded", "tool_name": "execute_shell", "content": "hi"}
    status = BackgroundTaskStatus.SUCCEEDED
    error = None


class DummyImageTask:
    id = 8
    tool_call_id = "call_bg_image"
    tool_name = "generate_image"
    arguments = {"prompt": "draw a cat"}
    result = {
        "status": "succeeded",
        "tool_name": "generate_image",
        "content": json.dumps(
            {
                "status": "success",
                "instruction": "Call send_file_to_user with the file path below before replying to the user.",
                "send_file_to_user": {
                    "files": [
                        {
                            "path": "temp/users/u1/generated_images/cat.png",
                            "display_name": "cat.png",
                            "description": "Generated image",
                            "mime_type": "image/png",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
    }
    status = BackgroundTaskStatus.SUCCEEDED
    error = None


def test_background_task_result_payload_keeps_storable_type_with_tool_chain_data():
    payload = json.loads(_build_background_tool_result_message(DummyTask()))

    assert payload["type"] == "background_tool_result"
    assert payload["tool_call"] == {
        "id": "call_bg_1",
        "name": "execute_shell",
        "arguments": {"command": "echo hi"},
    }
    assert payload["tool_result"]["tool_call_id"] == "call_bg_1"
    assert json.loads(payload["tool_result"]["content"]) == DummyTask.result


def test_background_image_task_result_preserves_send_file_instruction():
    content = _build_background_tool_result_message(DummyImageTask())
    raw_message = Message(
        id=43,
        session_id="s1",
        uid="u1",
        role=MessageRole.SYSTEM,
        type=MessageType.BACKGROUND_TASK_RESULT,
        content=content,
        profile_id=1,
    )

    messages = parse_db_messages_to_internal([raw_message])
    tool_result = json.loads(messages[0].content)
    image_payload = json.loads(tool_result["content"])

    assert messages[1].tool_calls[0].name == "generate_image"
    assert "files_to_user" not in image_payload
    assert image_payload["send_file_to_user"]["files"][0]["path"].endswith("cat.png")


def test_parse_background_task_result_expands_to_tool_chain_without_changing_db_type():
    content = _build_background_tool_result_message(DummyTask())
    raw_message = Message(
        id=42,
        session_id="s1",
        uid="u1",
        role=MessageRole.SYSTEM,
        type=MessageType.BACKGROUND_TASK_RESULT,
        content=content,
        profile_id=1,
    )

    messages = parse_db_messages_to_internal([raw_message])

    assert raw_message.type == MessageType.BACKGROUND_TASK_RESULT
    assert [message.role for message in messages] == [MessageRole.TOOL, MessageRole.ASSISTANT]
    assert messages[0].tool_call_id == "call_bg_1"
    assert json.loads(messages[0].content) == DummyTask.result
    assert messages[1].tool_calls[0].id == "call_bg_1"
    assert messages[1].tool_calls[0].name == "execute_shell"

    final_messages, _log_data = ContextManager._strategy_atomic_truncate(
        uid="u1",
        session_id="s1",
        parsed_history=messages,
        limit_tokens=10000,
        current_msg_tokens=0,
        context_window_k=4,
    )
    assert [message.role for message in final_messages] == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert final_messages[0].tool_calls[0].id == "call_bg_1"
    assert final_messages[1].tool_call_id == "call_bg_1"


def test_inject_system_prompt_drops_background_result_system_messages():
    background_system_message = Message(
        id=42,
        session_id="s1",
        uid="u1",
        role=MessageRole.SYSTEM,
        type=MessageType.BACKGROUND_TASK_RESULT,
        content=json.dumps({"type": "background_tool_result", "task": {"tool_name": "demo"}}),
        profile_id=1,
    )
    parsed_messages = parse_db_messages_to_internal([background_system_message])
    injected_messages = inject_system_prompt_text(parsed_messages, "system prompt")

    assert injected_messages[0].role == MessageRole.SYSTEM
    assert injected_messages[0].content == "system prompt"
    assert all(not (message.role == MessageRole.SYSTEM and isinstance(message.content, str) and "background_tool_result" in message.content) for message in injected_messages[1:])
    assert [message.role for message in injected_messages[1:]] == [MessageRole.TOOL, MessageRole.ASSISTANT]
