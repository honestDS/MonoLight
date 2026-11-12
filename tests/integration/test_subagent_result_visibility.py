import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.core.background_tasks.terminal_watch_coordinator import complete_terminal_watch_round
from app.core.background_tasks.terminal_watch_reply import route_terminal_auto_watch_reply
from app.core.constants import AGENT_WORK_TOOL_NAME, SHELL_TOOL_NAME, TERMINAL_TOOL_NAME, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.models.background_task import BackgroundTask
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
from app.models.session_todo import SessionTodoPlan
from app.models.subagent import SubagentToolCall
from app.models.terminal_session import TerminalSession


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

        parent_create_done = await runtime.run_next(independent_only=False)
        assert parent_create_done is not None
        assert parent_create_done.id == parent_work_id
        assert parent_create_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

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
            assert call.tool_result_message_id is not None
            tool_result_id = call.tool_result_message_id

        initial_reply_response = await runtime.client.get(f"/api/v1/chat/reply-works/{parent_work_id}")
        assert initial_reply_response.status_code == 200
        initial_reply_data = initial_reply_response.json()["data"]
        assert initial_reply_data["status"] == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value
        assert initial_reply_data["response"] is None

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
            assert call.tool_result_message_id == tool_result_id
            tool_result_id = call.tool_result_message_id
            tool_result = await db.get(Message, tool_result_id)
            assert tool_result is not None
            assert tool_result.role == MessageRole.TOOL
            assert tool_result.type == MessageType.TOOL_RESULT
            tool_result_message = InternalMessage.model_validate_json(tool_result.content or "{}")
            assert tool_result_message.type == MessageType.TOOL_RESULT
            assert tool_result_message.tool_call_id == call.tool_call_id
            tool_result_payload = json.loads(tool_result_message.content or "{}")
            expected_child_result = {
                "subagent_id": call.subagent_id,
                "work_id": child_work_id,
                "status": "succeeded",
                "content": child_summary,
            }
            assert tool_result_payload == expected_child_result

            markers = (await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
            assert len(markers) == 1
            marker = markers[0]
            assert marker.id is not None
            marker_id = marker.id
            assert marker.role == MessageRole.USER
            assert marker.dedupe_key == f"subagent-result:{call.id}"
            marker_payload = json.loads(marker.content or "{}")
            assert marker_payload["results"] == [expected_child_result]

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


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.asyncio
async def test_subagent_tool_context_isolated_from_parent_and_sibling_terminal_state(
    image_chat_runtime,
    protocol: str,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        main_todo_secret = "MAIN_TODO_SECRET_CONTEXT_ISOLATED_7A"
        main_terminal_id = "main-terminal-context-isolated-7a".ljust(32, "m")
        main_command = "echo MAIN_TERMINAL_COMMAND_SECRET_7A"
        main_context_secret = "MAIN_REQUEST_CONTEXT_SECRET_7A"
        main_purpose = "MAIN_PURPOSE_SECRET_7A"
        sibling_terminal_id = "sibling-terminal-context-isolated-7a".ljust(32, "s")
        sibling_command = "echo SIBLING_TERMINAL_COMMAND_SECRET_7A"
        sibling_context_secret = "SIBLING_REQUEST_CONTEXT_SECRET_7A"
        sibling_purpose = "SIBLING_PURPOSE_SECRET_7A"
        child_command = "echo CHILD_TERMINAL_COMMAND_SECRET_7A"
        child_summary = "CHILD_SUMMARY_VISIBLE_TO_PARENT_ONLY_7A"
        parent_summary = "PARENT_SUMMARY_AFTER_CHILD_CONTEXT_CHECK_7A"

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME, SHELL_TOOL_NAME]))
            profile.configs = configs
            session = await db.get(ChatSession, runtime.session_id)
            assert session is not None
            db.add(
                SessionTodoPlan(
                    session_id=runtime.session_id,
                    uid=runtime.uid,
                    revision=7,
                    todos=[{"content": main_todo_secret, "status": "in_progress"}],
                )
            )
            db.add(
                TerminalSession(
                    terminal_session_id=main_terminal_id,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    original_tool_call_id="main-context-terminal-call",
                    profile_id=profile.id,
                    command=main_command,
                    working_directory=".",
                    status=TerminalSessionStatus.RUNNING,
                    allowed_actions=[action.value for action in TerminalAction],
                )
            )
            db.add(
                BackgroundTask(
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    profile_id=profile.id,
                    tool_call_id="main-context-watch-call",
                    tool_name=TERMINAL_WATCH_TOOL_NAME,
                    arguments={"terminal_session_id": main_terminal_id, "after_offset": 0, "condition": "process_exit"},
                    extra={
                        "terminal_session_id": main_terminal_id,
                        "request_context": [{"content": main_context_secret}],
                        "purpose": main_purpose,
                    },
                )
            )
            await db.commit()

        create_calls = [
            InternalToolCall(
                id="context-isolation-create-child-a",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "create",
                    "name": "context-isolation-child-a",
                    "message": "Use an interactive shell tool and report the verified result without exposing internal command details.",
                },
            ),
            InternalToolCall(
                id="context-isolation-create-child-b",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "create",
                    "name": "context-isolation-child-b",
                    "message": "Complete the independent sibling task and report its concise result.",
                },
            ),
        ]
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls),
            ]
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "context-isolation-parent-create",
                "message": "Create both independent children and continue the parent task.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 2
            child, sibling = children
            assert child.id is not None and sibling.id is not None
            assert child.subagent_id is not None and sibling.subagent_id is not None
            assert child.subagent_id != sibling.subagent_id
            sibling_watch = BackgroundTask(
                uid=runtime.uid,
                session_id=runtime.session_id,
                profile_id=child.profile_id,
                tool_call_id="sibling-context-watch-call",
                tool_name=TERMINAL_WATCH_TOOL_NAME,
                arguments={"terminal_session_id": sibling_terminal_id, "after_offset": 0, "condition": "process_exit"},
                extra={
                    "auto_terminal_watch": True,
                    "terminal_session_id": sibling_terminal_id,
                    "origin_work_id": sibling.id,
                    "origin_work_identity": sibling.dedupe_key,
                    "request_context": [{"content": sibling_context_secret}],
                    "purpose": sibling_purpose,
                },
            )
            db.add(
                TerminalSession(
                    terminal_session_id=sibling_terminal_id,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    original_tool_call_id="sibling-context-terminal-call",
                    profile_id=child.profile_id,
                    command=sibling_command,
                    working_directory=".",
                    status=TerminalSessionStatus.RUNNING,
                    allowed_actions=[action.value for action in TerminalAction],
                )
            )
            db.add(sibling_watch)
            await db.commit()
            child_work_id = child.id
            sibling_work_id = sibling.id

        child_shell_call = InternalToolCall(
            id="context-isolation-child-shell-call",
            name=SHELL_TOOL_NAME,
            arguments={"command": child_command, "execution_mode": "interactive"},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[child_shell_call]))
        child_initial = await runtime.run_next(independent_only=True)
        assert child_initial is not None
        assert child_initial.id == child_work_id
        assert child_initial.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            auto_tasks = (
                (
                    await db.execute(
                        select(BackgroundTask)
                        .where(
                            BackgroundTask.uid == runtime.uid,
                            BackgroundTask.session_id == runtime.session_id,
                            BackgroundTask.profile_id == runtime.profile.id,
                            BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                        )
                        .order_by(BackgroundTask.id)
                    )
                )
                .scalars()
                .all()
            )
            child_auto_tasks = [task for task in auto_tasks if isinstance(task.extra, dict) and task.extra.get("origin_work_id") == child_work_id and task.extra.get("auto_terminal_watch") is True]
            assert len(child_auto_tasks) == 1
            child_auto_task = child_auto_tasks[0]
            child_terminal_id = child_auto_task.extra["terminal_session_id"]
            child_terminal = await db.get(TerminalSession, child_terminal_id)
            assert child_terminal is not None
            assert child_terminal.command == child_command

            tool_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.work_id == child_work_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            shell_call_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.work_id == child_work_id,
                            Message.type == MessageType.TOOL_CALL,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            persisted_shell_calls = [tool_call for row in shell_call_rows for tool_call in (InternalMessage.model_validate_json(row.content or "{}").tool_calls or []) if tool_call.name == child_shell_call.name and tool_call.arguments == child_shell_call.arguments]
            assert len(persisted_shell_calls) == 1
            child_tool_row = next(row for row in tool_rows if json.loads(row.content or "{}").get("tool_call_id") == persisted_shell_calls[0].id)
            child_suffix = child_tool_row.model_context_suffix
            assert isinstance(child_suffix, str) and child_suffix
            assert "<current_session_todo_snapshot>" not in child_suffix
            assert main_todo_secret not in child_suffix
            assert child_terminal_id in child_suffix
            assert child_command in child_suffix
            for forbidden in (main_terminal_id, main_command, main_context_secret, main_purpose, sibling_terminal_id, sibling_command, sibling_context_secret, sibling_purpose):
                assert forbidden not in child_suffix

            child_terminal.status = TerminalSessionStatus.EXITED
            child_terminal.exit_code = 0
            child_terminal.failure_reason = None
            child_terminal.next_output_offset = 0
            child_terminal.next_output_sequence = 1
            db.add(child_terminal)
            await db.commit()

            worker_id = "context-isolation-terminal-watch-worker"
            claimed_auto = await background_task_crud.try_claim(db, task_id=child_auto_task.id, worker_id=worker_id, lease_seconds=300)
            assert claimed_auto is not None
            assert await complete_terminal_watch_round(
                db,
                task=claimed_auto,
                worker_id=worker_id,
                content={
                    "terminal_session_id": child_terminal_id,
                    "event": "process_exit",
                    "after_offset": 0,
                    "session_status": TerminalSessionStatus.EXITED.value,
                    "exit_code": 0,
                    "failure_reason": None,
                    "output": "",
                    "start_offset": 0,
                    "next_offset": 0,
                    "latest_offset": 0,
                    "output_truncated": False,
                    "eof": True,
                    "condition": "process_exit",
                    "matched": True,
                },
            )
            await db.refresh(claimed_auto)
            assert await route_terminal_auto_watch_reply(db, claimed_auto)

        async with runtime.factory() as db:
            resumed_child = await db.get(SessionReplyWorkItem, child_work_id)
            assert resumed_child is not None
            assert resumed_child.status == SessionReplyWorkStatus.READY_FOR_LLM

        child_resume_start = len(runtime.provider_payloads)
        child_terminal_read_call = InternalToolCall(
            id="context-isolation-child-terminal-read-call",
            name=TERMINAL_TOOL_NAME,
            arguments={
                "operation": "read",
                "terminal_session_id": child_terminal_id,
                "read_output": False,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[child_terminal_read_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=child_summary),
            ]
        )
        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        child_resume_payloads = runtime.provider_payloads[child_resume_start:]
        assert child_resume_payloads
        child_resume_dump = json.dumps(child_resume_payloads, ensure_ascii=False)

        def _contains_suffix(value) -> bool:
            if isinstance(value, str):
                return child_suffix in value
            if isinstance(value, dict):
                return any(_contains_suffix(nested) for nested in value.values())
            if isinstance(value, list):
                return any(_contains_suffix(nested) for nested in value)
            return False

        assert any(_contains_suffix(payload) for payload in child_resume_payloads)
        assert child_terminal_id in child_resume_dump
        assert child_command in child_resume_dump
        for forbidden in (main_todo_secret, main_terminal_id, main_command, main_context_secret, main_purpose, sibling_terminal_id, sibling_command, sibling_context_secret, sibling_purpose):
            assert forbidden not in child_resume_dump

        parent_resume_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_summary))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        parent_resume_dump = json.dumps(runtime.provider_payloads[parent_resume_start:], ensure_ascii=False)
        assert child_summary in parent_resume_dump
        assert child_command not in parent_resume_dump
        assert child_terminal_id not in parent_resume_dump
        assert sibling_command not in parent_resume_dump
        assert sibling_terminal_id not in parent_resume_dump

        async with runtime.factory() as db:
            sibling = await db.get(SessionReplyWorkItem, sibling_work_id)
            assert sibling is not None
            assert sibling.status == SessionReplyWorkStatus.READY_FOR_LLM
