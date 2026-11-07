import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall


def _provider_items(payload: dict, protocol: str) -> list[dict]:
    items = payload.get("messages" if protocol == "OPENAI" else "input")
    assert isinstance(items, list)
    return [item for item in items if isinstance(item, dict)]


def _assert_no_marker_projection(value, marker_id: int) -> None:
    if isinstance(value, dict):
        assert value.get("id") != marker_id
        assert value.get("type") != MessageType.SUBAGENT_RESULT.value
        for nested in value.values():
            _assert_no_marker_projection(nested, marker_id)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_marker_projection(nested, marker_id)


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("show_tool_calls", [True, False], ids=["show-tools", "hide-tools"])
@pytest.mark.parametrize("stream_delivery", [True, False], ids=["stream", "buffered"])
@pytest.mark.asyncio
async def test_subagent_result_delivery_is_hidden_from_user_history_but_kept_for_model(
    image_chat_runtime,
    protocol: str,
    show_tool_calls: bool,
    stream_delivery: bool,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        guidance_text = "GUIDANCE_REMAINS_IN_API_HISTORY_BUT_NOT_AS_MODEL_MESSAGE"
        ordinary_user_json = json.dumps(
            {
                "note": "ordinary user JSON must remain visible",
                "source": "subagent",
                "type": "external_work_resume",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        child_summary = "SUBAGENT_SUMMARY_MUST_REACH_PARENT_MODEL_ONLY"
        parent_summary = "PARENT_FINAL_SUMMARY_REMAINS_VISIBLE"

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs

            session = await db.get(ChatSession, runtime.session_id)
            assert session is not None
            session.show_tool_calls = show_tool_calls
            await db.commit()

            guidance = await message_crud.create_guidance(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                profile_id=profile.id,
                content=guidance_text,
            )
            assert guidance.id is not None
            guidance_id = guidance.id

        create_call = InternalToolCall(
            id="visibility-parent-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "结果汇总", "message": "Return the child result as a concise summary."},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]))

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "subagent-result-visibility-request",
                "message": ordinary_user_json,
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]
        assert isinstance(parent_work_id, int)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            execution_state = deepcopy(parent.execution_state)
            execution_state["stream_requested"] = stream_delivery
            parent.execution_state = execution_state
            await db.commit()

        parent_waiting = await runtime.run_next(independent_only=False)
        assert parent_waiting is not None
        assert parent_waiting.id == parent_work_id
        assert parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert child.id is not None
            child_work_id = child.id
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert call.delivered is False

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM

            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert call.delivered is True
            assert call.id is not None
            assert call.tool_result_message_id is not None
            tool_result_id = call.tool_result_message_id
            tool_result = await db.get(Message, tool_result_id)
            assert tool_result is not None
            assert tool_result.role == MessageRole.TOOL
            assert tool_result.type == MessageType.TOOL_RESULT
            tool_result_message = InternalMessage.model_validate_json(tool_result.content or "{}")
            assert tool_result_message.type == MessageType.TOOL_RESULT
            assert tool_result_message.tool_call_id == call.tool_call_id
            tool_result_payload = json.loads(tool_result_message.content or "{}")
            assert tool_result_payload["child_work_id"] == child_work_id
            assert child_summary in json.dumps(tool_result_payload, ensure_ascii=False)

            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
            assert len(markers) == 1
            marker = markers[0]
            assert marker.id is not None
            marker_id = marker.id
            assert marker.role == MessageRole.USER
            assert marker.dedupe_key == f"subagent-result:{call.id}"
            assert child_summary in (marker.content or "")

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            checkpoint_messages = [InternalMessage.model_validate(item) for item in checkpoint["messages"]]
            checkpoint_marker = next(message for message in checkpoint_messages if message.id == marker_id)
            assert checkpoint_marker.type == MessageType.SUBAGENT_RESULT
            assert child_summary in (checkpoint_marker.content or "")

        resume_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_summary))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED

        resume_payloads = runtime.provider_payloads[resume_provider_start:]
        assert resume_payloads
        resume_payload_dump = json.dumps(resume_payloads, ensure_ascii=False)
        assert child_summary in resume_payload_dump
        assert EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT in resume_payload_dump
        for payload in resume_payloads:
            assert all(item.get("type") != MessageType.SUBAGENT_RESULT.value for item in _provider_items(payload, protocol))

        provider_dump = json.dumps(runtime.provider_payloads, ensure_ascii=False)
        assert guidance_text not in provider_dump

        reply_response = await runtime.client.get(f"/api/v1/chat/reply-works/{parent_work_id}")
        assert reply_response.status_code == 200
        reply_data = reply_response.json()["data"]
        assert reply_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert reply_data["response"]["choices"][0]["message"]["content"] == parent_summary
        reply_history = reply_data["response"]["history"]
        assert any(item.get("content") == parent_summary for item in reply_history)
        _assert_no_marker_projection(reply_history, marker_id)
        reply_tool_results = [item for item in reply_history if item.get("id") == tool_result_id]
        if show_tool_calls:
            assert len(reply_tool_results) == 1
            assert reply_tool_results[0]["type"] == MessageType.TOOL_RESULT.value
        else:
            assert reply_tool_results == []

        history_responses = [
            await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            ),
            await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "after_id": max(0, guidance_id - 1), "size": 100},
            ),
        ]
        for history_response in history_responses:
            assert history_response.status_code == 200
            history = history_response.json()["data"]
            assert any(item.get("role") == MessageRole.USER.value and item.get("type") == MessageType.TEXT.value and item.get("content") == ordinary_user_json for item in history)
            assert any(item.get("role") == MessageRole.SYSTEM.value and item.get("type") == MessageType.GUIDANCE.value and item.get("content") == guidance_text for item in history)
            assert any(item.get("content") == parent_summary for item in history)
            _assert_no_marker_projection(history, marker_id)
            history_tool_results = [item for item in history if item.get("id") == tool_result_id]
            if show_tool_calls:
                assert len(history_tool_results) == 1
                assert history_tool_results[0]["type"] == MessageType.TOOL_RESULT.value
            else:
                assert history_tool_results == []

        if stream_delivery:
            async with runtime.factory() as db:
                stream_rows = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == parent_work_id).order_by(SessionReplyStreamEvent.sequence_no))).scalars().all()
            assert any(row.event.get("type") == "turn_end" for row in stream_rows)
            for row in stream_rows:
                _assert_no_marker_projection(row.event, marker_id)

        replayed_events = [event async for event in session_reply_queue_manager.wait_for_stream(parent_work_id, resume_mode=True)]
        done_events = [event for event in replayed_events if event.get("type") == "done"]
        assert len(done_events) == 1
        done_event = done_events[0]
        for history in (done_event["history"], done_event["response"]["history"]):
            assert any(item.get("content") == parent_summary for item in history)
            _assert_no_marker_projection(history, marker_id)
