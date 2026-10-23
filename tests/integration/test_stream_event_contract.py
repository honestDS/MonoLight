import json

import pytest
from sqlalchemy import select

from app.core.crud.channel import cursor as channel_cursor_module
from app.core.dispatchers import dispatcher as dispatcher_module
from app.models.channel import ModelChannel
from app.models.message import (
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
)
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.user import User
from app.providers.llm.client import LLMClient
from app.schemas.response import LLMResponse
from tests.integration.stream_event_test_support import dispatcher_stream_event


@pytest.mark.asyncio
async def test_dispatcher_stream_event_contract_with_file_tool_and_persistence(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    uid = "stream-contract"
    session_id = "stream-contract-session"
    request_id = "contract-request"
    note_path = tmp_path / "note.txt"
    note_path.write_text("contract note\n", encoding="utf-8")

    profile_config = ProfileConfig(
        channel={
            "chat_channel": {
                "rules": [
                    {
                        "channel_id": 1,
                        "model_id": "contract-model",
                        "priority": 1,
                        "weight": 1,
                    }
                ]
            }
        },
        security={},
        tool={"enabled_tools": ["file_tool"]},
        other={},
        memory={"enabled": False},
    )

    async with database_factory(foreign_keys=True) as session_factory:
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid=uid, username="stream-contract"),
                    ModelChannel(
                        id=1,
                        name="stream-contract-channel",
                        api_key="enc:v1:test",
                        base_url="https://llm.invalid",
                        model_ids=[
                            {
                                "model_id": "contract-model",
                                "usage": "CHAT",
                                "protocol": "OPENAI",
                                "context_window_k": 32,
                                "max_tokens": 512,
                            }
                        ],
                    ),
                    Profile(
                        id=1,
                        uid=uid,
                        name="stream contract profile",
                        configs=profile_config.model_dump(mode="json"),
                    ),
                    ChatSession(
                        session_id=session_id,
                        uid=uid,
                        profile_id=1,
                        profile_override_id=1,
                        source="ws",
                        goal_mode=False,
                        max_turns=5,
                    ),
                ]
            )
            await db.commit()

            monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _self: "test-key")
            monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
            monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)

            generation_calls: list[dict[str, object]] = []

            async def generate_with_stream_callback(**kwargs):
                generation_calls.append(kwargs)
                if len(generation_calls) == 1:
                    await kwargs["on_reasoning"]("Inspecting.")
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            reasoning_content="Inspecting.",
                            tool_calls=[
                                InternalToolCall(
                                    id="contract-read",
                                    name="file_tool",
                                    arguments={"operation": "read", "path": str(note_path)},
                                )
                            ],
                        ),
                        model="contract-model",
                        finish_reason="tool_calls",
                    )
                if len(generation_calls) == 2:
                    await kwargs["on_content"]("Completed.")
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            content="Completed.",
                        ),
                        model="contract-model",
                        finish_reason="stop",
                    )
                raise AssertionError("unexpected additional LLM generation")

            monkeypatch.setattr(
                LLMClient,
                "generate_with_stream_callback",
                staticmethod(generate_with_stream_callback),
            )

            events = [
                event
                async for event in dispatcher_module.ChatDispatcher.dispatch_stream(
                    db,
                    message="Read the note.",
                    uid=uid,
                    session_id=session_id,
                    request_id=request_id,
                )
            ]

            assert len(generation_calls) == 2
            assert all(call["api_key"] == "test-key" for call in generation_calls)
            assert all(call["model_id"] == "contract-model" for call in generation_calls)

            excluded_types = {"task_start", "metadata", "llm_request_metadata", "agent_loop_output"}
            contract_events = [event for event in events if event["type"] not in excluded_types]
            assert [event["type"] for event in contract_events] == [
                "agent_loop_start",
                "reasoning",
                "turn_end",
                "tool_start",
                "tool_end",
                "agent_loop_start",
                "content",
                "turn_end",
                "done",
            ]

            interaction_events = contract_events[:-1]
            start_events = [event for event in interaction_events if event["type"] == "agent_loop_start"]
            turn_end_events = [event for event in interaction_events if event["type"] == "turn_end"]
            assert len(start_events) == 2
            assert len(turn_end_events) == 2
            first_response_id, final_response_id = [event["response_id"] for event in start_events]
            assert isinstance(first_response_id, str) and first_response_id
            assert isinstance(final_response_id, str) and final_response_id
            assert first_response_id != final_response_id

            tool_end_event = next(event for event in interaction_events if event["type"] == "tool_end")
            tool_result_content = tool_end_event["result"]
            tool_result_payload = json.loads(tool_result_content)
            assert tool_result_payload["status"] == "success"
            assert tool_result_payload["operation"] == "read"
            assert tool_result_payload["path"] == str(note_path)
            assert tool_result_payload["content"] == "1: contract note"

            first_assistant_message_id = turn_end_events[0]["message_id"]
            final_assistant_message_id = turn_end_events[1]["message_id"]
            assert isinstance(first_assistant_message_id, int)
            assert isinstance(final_assistant_message_id, int)

            expected_interaction_events = [
                {
                    "type": "agent_loop_start",
                    "turn": 1,
                    "response_id": first_response_id,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "reasoning",
                    "content": "Inspecting.",
                    "turn": 1,
                    "response_id": first_response_id,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "turn_end",
                    "turn": 1,
                    "response_id": first_response_id,
                    "message_id": first_assistant_message_id,
                    "reasoning_content": "Inspecting.",
                    "finish_reason": "tool_calls",
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "tool_start",
                    "name": "file_tool",
                    "arguments": {"operation": "read", "path": str(note_path)},
                    "tool_call_id": "contract-read",
                    "response_id": first_response_id,
                    "tool_call_index": 0,
                    "tool_call_count": 1,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "tool_end",
                    "name": "file_tool",
                    "result": tool_result_content,
                    "tool_call_id": "contract-read",
                    "response_id": first_response_id,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "agent_loop_start",
                    "turn": 2,
                    "response_id": final_response_id,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "content",
                    "content": "Completed.",
                    "turn": 2,
                    "response_id": final_response_id,
                    "session_id": session_id,
                    "request_id": request_id,
                },
                {
                    "type": "turn_end",
                    "turn": 2,
                    "response_id": final_response_id,
                    "message_id": final_assistant_message_id,
                    "content": "Completed.",
                    "finish_reason": "stop",
                    "session_id": session_id,
                    "request_id": request_id,
                },
            ]
            assert interaction_events == expected_interaction_events

            helper_interaction_events = [
                dispatcher_stream_event("agent_loop_start", response_id=first_response_id, turn=1),
                dispatcher_stream_event("reasoning", response_id=first_response_id, turn=1, content="Inspecting."),
                dispatcher_stream_event(
                    "turn_end",
                    response_id=first_response_id,
                    turn=1,
                    message_id=first_assistant_message_id,
                    reasoning_content="Inspecting.",
                    finish_reason="tool_calls",
                ),
                dispatcher_stream_event(
                    "tool_start",
                    response_id=first_response_id,
                    name="file_tool",
                    arguments={"operation": "read", "path": str(note_path)},
                    tool_call_id="contract-read",
                    tool_call_index=0,
                    tool_call_count=1,
                ),
                dispatcher_stream_event(
                    "tool_end",
                    response_id=first_response_id,
                    name="file_tool",
                    result=tool_result_content,
                    tool_call_id="contract-read",
                ),
                dispatcher_stream_event("agent_loop_start", response_id=final_response_id, turn=2),
                dispatcher_stream_event("content", response_id=final_response_id, turn=2, content="Completed."),
                dispatcher_stream_event(
                    "turn_end",
                    response_id=final_response_id,
                    turn=2,
                    message_id=final_assistant_message_id,
                    content="Completed.",
                    finish_reason="stop",
                ),
            ]
            normalized_interaction_events = [{key: value for key, value in event.items() if key not in {"session_id", "request_id"}} for event in interaction_events]
            assert normalized_interaction_events == helper_interaction_events

            persisted_messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
            assert [message.role for message in persisted_messages] == [
                MessageRole.USER,
                MessageRole.ASSISTANT,
                MessageRole.TOOL,
                MessageRole.ASSISTANT,
            ]
            assert [message.type for message in persisted_messages] == [
                MessageType.TEXT,
                MessageType.TOOL_CALL,
                MessageType.TOOL_RESULT,
                MessageType.TEXT,
            ]
            assert persisted_messages[0].content == "Read the note."
            assert persisted_messages[0].is_processed is True
            assert persisted_messages[1].id == first_assistant_message_id
            assert persisted_messages[1].reasoning_content == "Inspecting."
            assert json.loads(persisted_messages[1].content or "{}")["tool_calls"][0] == {
                "id": "contract-read",
                "name": "file_tool",
                "arguments": {"operation": "read", "path": str(note_path)},
            }
            persisted_tool_message = InternalMessage.model_validate_json(persisted_messages[2].content)
            assert persisted_tool_message.role == MessageRole.TOOL
            assert persisted_tool_message.tool_call_id == "contract-read"
            assert persisted_tool_message.content == tool_result_content
            assert persisted_messages[3].id == final_assistant_message_id
            assert persisted_messages[3].content == "Completed."

            done_event = contract_events[-1]
            assert set(done_event) == {
                "type",
                "session_id",
                "request_id",
                "history",
                "files",
                "response",
                "response_id",
            }
            assert done_event["type"] == "done"
            assert done_event["session_id"] == session_id
            assert done_event["request_id"] == request_id
            assert done_event["response_id"] == final_response_id
            response = done_event["response"]
            parsed_response = LLMResponse.model_validate(response)
            assert parsed_response.choices[0].created_at > 0
            choice = response["choices"][0]
            assert choice["message"]["content"] == "Completed."
            assert choice["message"]["role"] == "assistant"
            assert choice["finish_reason"] == "stop"
            assert [item["role"] for item in response["history"]] == ["assistant", "tool", "assistant"]
            assert response["history"][1]["content"] == tool_result_content
            assert response["history"][2]["content"] == "Completed."
            assert response["files"] is None
            assert done_event["history"] == response["history"]
            assert done_event["files"] == response["files"]
            assert done_event == dispatcher_stream_event(
                "done",
                response_id=final_response_id,
                session_id=session_id,
                request_id=request_id,
                response=response,
            )
