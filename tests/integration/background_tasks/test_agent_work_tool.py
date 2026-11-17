import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    FILE_TOOL_NAME,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.security import get_current_user
from app.core.session_reply_queue.child_delivery import resume_child_work_for_parent
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentBoardPost, SubagentToolCall, SubagentWork
from app.models.user import User


def _provider_tool_names(payload: dict) -> set[str]:
    names = set()
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        function = tool.get("function")
        if name is None and isinstance(function, dict):
            name = function.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


@pytest.mark.asyncio
async def test_agent_work_create_executes_private_child_and_resumes_parent(
    image_chat_runtime,
    tmp_path: Path,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        target = (tmp_path / "agent-work.txt").resolve()
        parent_secret = "parent-session-secret"
        child_secret = "child-must-not-see-parent-secret"
        board_marker = f"child-verification-marker-{runtime.session_id}-{target.name}"
        child_instruction = f"Independently update {target}: write exactly 'alpha\\n', edit alpha to beta, then apply a unified patch changing beta to gamma. Report the verified result."

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            enabled_tools = list(configs["tool"]["enabled_tools"])
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*enabled_tools, AGENT_WORK_TOOL_NAME, SHELL_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_call = InternalToolCall(
            id="agent-work-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "文件更新", "message": child_instruction},
        )
        assert parent_secret not in child_instruction
        assert child_secret not in child_instruction
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]))

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "agent-work-root-request",
                "message": f"Complete the main task with {parent_secret} and {child_secret}.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]

        parent_done = await runtime.run_next()
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert parent.work_type == SessionReplyWorkType.FOREGROUND_REPLY

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
            assert child.work_type == SessionReplyWorkType.AGENT_WORK
            assert child.source_type == SessionReplySourceType.PARENT_WORK
            assert child.source_id == str(parent_work_id)
            assert child.uid == parent.uid and child.session_id == parent.session_id and child.profile_id == parent.profile_id
            assert child.subagent_id is not None
            child_spec = child.execution_state["agent_work"]
            assert child_spec["parent_work_id"] == parent_work_id
            assert child_spec["subagent_id"] == child.subagent_id

            subagent = await db.get(Subagent, child.subagent_id)
            assert subagent is not None
            assert subagent.name == "文件更新"
            assert (subagent.uid, subagent.session_id, subagent.profile_id) == (
                parent.uid,
                parent.session_id,
                parent.profile_id,
            )
            assert subagent.active_child_work_id == child_work_id
            assert subagent.latest_child_work_id == child_work_id

            round_row = await db.get(SubagentWork, child_work_id)
            assert round_row is not None
            assert (round_row.subagent_id, round_row.child_work_id, round_row.current_work_id) == (
                subagent.id,
                child_work_id,
                child_work_id,
            )
            assert round_row.result is None

            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            assert (call.subagent_id, call.child_work_id, call.parent_work_id) == (
                subagent.id,
                child_work_id,
                parent_work_id,
            )
            assert call.arguments == {"operation": "create", "name": "文件更新", "message": child_instruction}
            assert call.delivered is False

            assistant_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.work_id.is_(None),
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TOOL_CALL,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            persisted_create_calls = []
            for assistant_row in assistant_rows:
                assistant_message = InternalMessage.model_validate_json(assistant_row.content or "{}")
                persisted_create_calls.extend(tool_call for tool_call in assistant_message.tool_calls or [] if tool_call.name == create_call.name and tool_call.arguments == create_call.arguments)
            assert len(persisted_create_calls) == 1
            persisted_create_call = persisted_create_calls[0]
            internal_create_call_id = persisted_create_call.id
            assert persisted_create_call.name == create_call.name
            assert persisted_create_call.arguments == create_call.arguments
            assert internal_create_call_id == call.tool_call_id
            assert child_spec["parent_tool_call_id"] == internal_create_call_id

            instruction_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.work_id == child_work_id,
                            Message.role == MessageRole.USER,
                            Message.type == MessageType.TEXT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [row.content for row in instruction_rows] == [child_instruction]
            private_instruction_id = instruction_rows[0].id
            private_instruction_content = instruction_rows[0].content
            assert private_instruction_id is not None
            assert private_instruction_content == child_instruction

        history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assert all(item.get("id") != private_instruction_id for item in history)
        assert all(item.get("content") != private_instruction_content for item in history)
        root_provider_dump = json.dumps(runtime.provider_payloads[0], ensure_ascii=False)
        assert parent_secret in root_provider_dump and child_secret in root_provider_dump
        assert "[Subagent delegation guidance]" in root_provider_dump

        child_board_post_call = InternalToolCall(
            id="child-board-post-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "board_post",
                "stage": "verification",
                "content": board_marker,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="child-write-call",
                            name=FILE_TOOL_NAME,
                            arguments={
                                "operation": "write",
                                "path": str(target),
                                "content": "alpha\n",
                            },
                        )
                    ],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="child-edit-call",
                            name=FILE_TOOL_NAME,
                            arguments={
                                "operation": "edit",
                                "path": str(target),
                                "old_text": "alpha",
                                "new_text": "beta",
                            },
                        )
                    ],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="child-patch-call",
                            name=FILE_TOOL_NAME,
                            arguments={
                                "operation": "patch",
                                "path": str(target),
                                "patch": "@@\n-beta\n+gamma\n",
                            },
                        )
                    ],
                ),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[child_board_post_call],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="子任务已完成"),
            ]
        )
        child_provider_start = len(runtime.provider_payloads)
        child_done = await runtime.run_next()
        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert target.read_text(encoding="utf-8") == "gamma\n"

        child_provider_payloads = runtime.provider_payloads[child_provider_start:]
        assert child_provider_payloads
        child_provider_dump = json.dumps(child_provider_payloads, ensure_ascii=False)
        assert parent_secret not in child_provider_dump
        assert child_secret not in child_provider_dump
        assert "[Subagent delegation guidance]" not in child_provider_dump
        assert "You are an independent subagent executing work" in child_provider_dump
        for payload in child_provider_payloads:
            tool_names = _provider_tool_names(payload)
            assert FILE_TOOL_NAME in tool_names
            assert AGENT_WORK_TOOL_NAME in tool_names
            assert SHELL_TOOL_NAME in tool_names
            assert TERMINAL_TOOL_NAME in tool_names
            assert TERMINAL_WATCH_TOOL_NAME in tool_names
            agent_work_definitions = []
            for tool in payload.get("tools", []):
                if not isinstance(tool, dict):
                    continue
                definition = tool.get("function") if isinstance(tool.get("function"), dict) else tool
                if definition.get("name") == AGENT_WORK_TOOL_NAME:
                    agent_work_definitions.append(definition)
            assert len(agent_work_definitions) == 1
            parameters = agent_work_definitions[0]["parameters"]
            assert set(parameters["properties"]) == {"operation", "stage", "content"}
            assert {"operation", "stage", "content"}.issubset(parameters["required"])
            assert parameters["properties"]["operation"]["enum"] == ["board_post"]

        async with runtime.factory() as db:
            child = await db.get(SessionReplyWorkItem, child_work_id)
            assert child is not None
            assert child.status == SessionReplyWorkStatus.SUCCEEDED
            assert child.subagent_id is not None
            round_row = await db.get(SubagentWork, child_work_id)
            assert round_row is not None
            assert round_row.result is not None
            assert round_row.result["completed"] is True
            assert round_row.result["status"] == "succeeded"
            assert round_row.result["result"]["content"] == "子任务已完成"

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
            tool_payloads = []
            for row in tool_rows:
                tool_message = InternalMessage.model_validate_json(row.content or "{}")
                assert tool_message.tool_execution_status == "succeeded"
                tool_payloads.append(json.loads(tool_message.content or "{}"))
            file_tool_payloads = [payload for payload in tool_payloads if payload != {"status": "saved"}]
            assert [(payload["operation"], payload["status"]) for payload in file_tool_payloads] == [
                ("write", "success"),
                ("edit", "success"),
                ("patch", "success"),
            ]
            board_post_results = [payload for payload in tool_payloads if payload == {"status": "saved"}]
            assert len(board_post_results) == 1
            board_post_result = board_post_results[0]
            assert board_post_result == {"status": "saved"}

            board_posts = (
                (
                    await db.execute(
                        select(SubagentBoardPost)
                        .where(
                            SubagentBoardPost.uid == child.uid,
                            SubagentBoardPost.session_id == child.session_id,
                            SubagentBoardPost.profile_id == child.profile_id,
                            SubagentBoardPost.subagent_id == child.subagent_id,
                            SubagentBoardPost.child_work_id == child_work_id,
                        )
                        .order_by(SubagentBoardPost.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(board_posts) == 2
            board_post = board_posts[0]
            completion_board_post = board_posts[1]
            assert board_post.id is not None
            assert board_post.stage == "verification"
            assert board_post.content == board_marker
            assert board_post.child_work_id == child_work_id
            assert board_post.subagent_id == child.subagent_id
            assert completion_board_post.id is not None
            assert completion_board_post.stage == "succeeded"
            assert completion_board_post.content == "子任务已完成"
            assert completion_board_post.child_work_id == child_work_id
            assert completion_board_post.subagent_id == child.subagent_id

            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.tool_call_id == internal_create_call_id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert call.delivered is True
            assert call.tool_result_message_id is not None
            create_result_row = await db.get(Message, call.tool_result_message_id)
            assert create_result_row is not None
            create_result_message = InternalMessage.model_validate_json(create_result_row.content or "{}")
            create_result = json.loads(create_result_message.content or "{}")
            assert create_result_message.tool_execution_status == "succeeded"
            assert create_result == {
                "status": "queued",
                "subagent_id": child.subagent_id,
                "work_id": child_work_id,
            }

        parent_board_read_call = InternalToolCall(
            id="parent-board-read-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "board_read",
                "subagent_id": child.subagent_id,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[parent_board_read_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content="主任务已完成"),
            ]
        )
        parent_provider_start = len(runtime.provider_payloads)
        parent_done = await runtime.run_next()
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs

        parent_payloads = runtime.provider_payloads[parent_provider_start:]
        assert len(parent_payloads) == 2
        assert board_marker not in json.dumps(parent_payloads[0], ensure_ascii=False)
        assert board_marker in json.dumps(parent_payloads[1], ensure_ascii=False)

        async with runtime.factory() as db:
            assistant_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TOOL_CALL,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            persisted_board_read_calls = []
            for assistant_row in assistant_rows:
                assistant_message = InternalMessage.model_validate_json(assistant_row.content or "{}")
                persisted_board_read_calls.extend(tool_call for tool_call in assistant_message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments == parent_board_read_call.arguments)
            assert len(persisted_board_read_calls) == 1
            persisted_board_read_call = persisted_board_read_calls[0]
            assert persisted_board_read_call.arguments == parent_board_read_call.arguments
            assert isinstance(persisted_board_read_call.id, str) and persisted_board_read_call.id

            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            board_read_results = []
            for result_row in result_rows:
                result_message = InternalMessage.model_validate_json(result_row.content or "{}")
                if result_message.tool_call_id != persisted_board_read_call.id:
                    continue
                assert result_message.tool_execution_status == "succeeded"
                board_read_results.append(json.loads(result_message.content or "{}"))
            assert len(board_read_results) == 1
            board_read_result = board_read_results[0]
            assert set(board_read_result) == {"posts"}
            assert len(board_read_result["posts"]) == 2
            returned_post = board_read_result["posts"][0]
            assert returned_post == {
                "work_id": child_work_id,
                "stage": "verification",
                "content": board_marker,
            }
            returned_completion_post = board_read_result["posts"][1]
            assert returned_completion_post == {
                "work_id": child_work_id,
                "stage": "succeeded",
                "content": "子任务已完成",
            }

            leaked_stage_messages = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.USER,
                            Message.type == MessageType.TEXT,
                            Message.content == board_marker,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert leaked_stage_messages == []

        final_history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert final_history_response.status_code == 200
        final_history = final_history_response.json()["data"]
        assert any(item.get("content") == "主任务已完成" for item in final_history)
        assert all(item.get("content") != board_marker for item in final_history)

        async with runtime.factory() as db:
            markers_before = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.type == MessageType.SUBAGENT_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(markers_before) == 2
            stage_marker, completion_marker = markers_before
            stage_payload = json.loads(stage_marker.content or "{}")
            completion_payload = json.loads(completion_marker.content or "{}")
            assert stage_marker.role == MessageRole.USER
            assert len(stage_marker.dedupe_key) == 64
            assert stage_marker.dedupe_key != f"subagent-board:{parent_work_id}:{board_post.id}"
            assert stage_payload["type"] == "subagent_board_update"
            assert stage_payload["source"] == "subagent"
            assert stage_payload["subagent_id"] == child.subagent_id
            assert stage_payload["work_id"] == child_work_id
            assert stage_payload["post_id"] == board_post.id
            assert stage_payload["stage"] == "verification"
            assert "content" not in stage_payload
            assert board_marker not in (stage_marker.content or "")
            assert completion_marker.role == MessageRole.USER
            assert len(completion_marker.dedupe_key) == 64
            assert completion_marker.dedupe_key != f"subagent-result:{call.id}"
            assert completion_payload["type"] == "external_work_resume"
            assert completion_payload["source"] == "subagent"
            assert completion_payload["tool_call"] == {
                "id": internal_create_call_id,
                "name": AGENT_WORK_TOOL_NAME,
                "arguments": create_call.arguments,
            }
            assert completion_payload["results"] == [
                {
                    "status": "succeeded",
                    "subagent_id": child.subagent_id,
                    "work_id": child_work_id,
                    "content": "子任务已完成",
                }
            ]
            marker_snapshot = [(row.id, row.dedupe_key, row.content) for row in markers_before]

        async with runtime.factory() as db:
            assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False

        async with runtime.factory() as db:
            markers_after = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.type == MessageType.SUBAGENT_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [(row.id, row.dedupe_key, row.content) for row in markers_after] == marker_snapshot


@pytest.mark.parametrize("scope", ["uid", "session", "profile"])
@pytest.mark.parametrize("operation", ["query_result", "send_message", "cancel", "board_read"])
@pytest.mark.asyncio
async def test_agent_work_cross_scope_operations_fail_without_side_effects(
    image_chat_runtime,
    scope: str,
    operation: str,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        private_secret = f"private-child-{scope}-{operation}"
        child_instruction = f"Complete this private instruction and never expose {private_secret}."
        create_call = InternalToolCall(
            id="model-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "跨范围子任务", "message": child_instruction},
        )
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[create_call],
            )
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"cross-scope-create-{scope}-{operation}",
                "message": "Create a private child task.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]
        parent_submitted = await runtime.run_next()
        assert parent_submitted is not None
        assert parent_submitted.id == parent_work_id
        assert parent_submitted.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            assistant_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TOOL_CALL,
                        )
                    )
                )
                .scalars()
                .all()
            )
            persisted_create_calls = []
            for row in assistant_rows:
                assistant_message = InternalMessage.model_validate_json(row.content or "{}")
                persisted_create_calls.extend(tool_call for tool_call in assistant_message.tool_calls or [] if tool_call.name == create_call.name and tool_call.arguments == create_call.arguments)
            assert len(persisted_create_calls) == 1
            persisted_create_call = persisted_create_calls[0]

            create_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            create_results = []
            for row in create_rows:
                result = InternalMessage.model_validate_json(row.content or "{}")
                if result.tool_call_id == persisted_create_call.id:
                    create_results.append((result, json.loads(result.content or "{}")))
            assert len(create_results) == 1
            create_result, create_payload = create_results[0]
            assert create_result.tool_call_id == persisted_create_call.id
            assert set(create_payload) == {"status", "work_id", "subagent_id"}
            assert create_payload["status"] == "queued"
            subagent_id = create_payload["subagent_id"]
            child_work_id = create_payload["work_id"]
            assert isinstance(subagent_id, str) and subagent_id
            assert isinstance(child_work_id, int) and child_work_id > 0
            assert create_payload == {
                "status": "queued",
                "work_id": child_work_id,
                "subagent_id": subagent_id,
            }

            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            subagent = await db.get(Subagent, subagent_id)
            round_row = await db.get(SubagentWork, child_work_id)
            assert parent is not None and child is not None and subagent is not None and round_row is not None
            assert child.status == SessionReplyWorkStatus.READY_FOR_LLM
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
            messages = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            parent_snapshot = parent.model_dump(mode="json")
            child_snapshot = child.model_dump(mode="json")
            subagent_snapshot = subagent.model_dump(mode="json")
            round_snapshot = round_row.model_dump(mode="json")
            call_snapshots = [call.model_dump(mode="json") for call in calls]
            message_snapshots = [message.model_dump(mode="json") for message in messages]

        actor_uid = runtime.uid
        actor_session_id = runtime.session_id
        actor_profile_id = runtime.profile.id
        async with runtime.factory() as db:
            owner_profile = await db.get(Profile, runtime.profile.id)
            assert owner_profile is not None
            if scope == "session":
                actor_session_id = "cross-scope-session"
                db.add(
                    ChatSession(
                        session_id=actor_session_id,
                        uid=actor_uid,
                        profile_id=actor_profile_id,
                        profile_override_id=actor_profile_id,
                        source="http",
                        stream_llm_request=False,
                        goal_mode=False,
                        max_turns=8,
                        show_tool_calls=False,
                    )
                )
            elif scope == "profile":
                actor_profile = Profile(
                    uid=actor_uid,
                    name="cross-scope-profile",
                    configs=deepcopy(owner_profile.configs),
                )
                db.add(actor_profile)
                await db.flush()
                assert actor_profile.id is not None
                actor_profile_id = actor_profile.id
                session = await db.get(ChatSession, runtime.session_id)
                assert session is not None
                session.profile_id = actor_profile_id
                session.profile_override_id = actor_profile_id
            else:
                actor_uid = "cross-scope-user"
                actor_session_id = "cross-scope-user-session"
                actor_profile = Profile(
                    uid=actor_uid,
                    name="cross-scope-user-profile",
                    configs=deepcopy(owner_profile.configs),
                )
                db.add_all([User(uid=actor_uid, username="cross-scope-user"), actor_profile])
                await db.flush()
                assert actor_profile.id is not None
                actor_profile_id = actor_profile.id
                db.add(
                    ChatSession(
                        session_id=actor_session_id,
                        uid=actor_uid,
                        profile_id=actor_profile_id,
                        profile_override_id=actor_profile_id,
                        source="http",
                        stream_llm_request=False,
                        goal_mode=False,
                        max_turns=8,
                        show_tool_calls=False,
                    )
                )
            await db.commit()

        actor = SimpleNamespace(uid=actor_uid, username=actor_uid, is_superuser=False, is_active=True)
        runtime.client._transport.app.dependency_overrides[get_current_user] = lambda: actor
        arguments = {"operation": operation, "subagent_id": subagent_id}
        if operation == "send_message":
            arguments["message"] = f"unauthorized-{private_secret}"
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="model-cross-scope-call", name=AGENT_WORK_TOOL_NAME, arguments=arguments)],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content="ordinary foreground completion"),
            ]
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": actor_session_id,
                "request_id": f"cross-scope-{scope}-{operation}",
                "message": "Attempt the private operation.",
            },
        )
        assert accepted.status_code == 200
        other_work_id = accepted.json()["data"]["work_id"]
        worker_id = f"cross-scope-worker-{scope}-{operation}"
        async with runtime.factory() as db:
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
                independent_only=False,
            )
            assert claimed is not None and claimed.id == other_work_id
        await SessionReplyConsumer()._run_claimed(other_work_id, worker_id, claimed.attempt_count, claimed.max_attempts)
        assert not runtime.outputs

        async with runtime.factory() as db:
            other_work = await db.get(SessionReplyWorkItem, other_work_id)
            assert other_work is not None
            assert other_work.status == SessionReplyWorkStatus.SUCCEEDED
            failed_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == actor_session_id,
                            Message.uid == actor_uid,
                            Message.profile_id == actor_profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assistant_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == actor_session_id,
                            Message.uid == actor_uid,
                            Message.profile_id == actor_profile_id,
                            Message.role == MessageRole.ASSISTANT,
                            Message.type == MessageType.TOOL_CALL,
                        )
                    )
                )
                .scalars()
                .all()
            )
            persisted_failed_calls = []
            for row in assistant_rows:
                assistant_message = InternalMessage.model_validate_json(row.content or "{}")
                persisted_failed_calls.extend(tool_call for tool_call in assistant_message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments == arguments)
            assert len(persisted_failed_calls) == 1
            persisted_failed_call = persisted_failed_calls[0]
            failed_results = []
            for row in failed_rows:
                result = InternalMessage.model_validate_json(row.content or "{}")
                payload = json.loads(result.content or "{}")
                if result.tool_call_id == persisted_failed_call.id:
                    failed_results.append((result, payload))
            assert len(failed_results) == 1
            failed_message, failed_payload = failed_results[0]
            assert failed_message.tool_call_id == persisted_failed_call.id
            assert failed_message.tool_execution_status == "failed"
            assert set(failed_payload) == {"error"}
            failed_dump = json.dumps(failed_payload, ensure_ascii=False)
            assert subagent_id not in failed_dump
            assert str(child_work_id) not in failed_dump
            assert private_secret not in failed_dump
            assert child_instruction not in failed_dump

            parent_after = await db.get(SessionReplyWorkItem, parent_work_id)
            child_after = await db.get(SessionReplyWorkItem, child_work_id)
            subagent_after = await db.get(Subagent, subagent_id)
            round_after = await db.get(SubagentWork, child_work_id)
            assert parent_after is not None and child_after is not None and subagent_after is not None and round_after is not None
            assert parent_after.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert parent_after.model_dump(mode="json") == parent_snapshot
            assert child_after.model_dump(mode="json") == child_snapshot
            assert subagent_after.model_dump(mode="json") == subagent_snapshot
            assert round_after.model_dump(mode="json") == round_snapshot
            calls_after = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
            assert [call.model_dump(mode="json") for call in calls_after] == call_snapshots
            messages_after = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == runtime.session_id,
                            Message.uid == runtime.uid,
                            Message.profile_id == runtime.profile.id,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            assert [message.model_dump(mode="json") for message in messages_after] == message_snapshots
