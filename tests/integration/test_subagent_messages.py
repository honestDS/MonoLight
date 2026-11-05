import asyncio
import contextlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
from app.core.constants import AGENT_WORK_TOOL_NAME, FILE_TOOL_NAME
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from app.transformers.openai import OpenAIResponsesTransformer


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
async def test_running_subagent_reads_appended_message_on_next_model_request(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        target = (tmp_path / "subagent-message.txt").resolve()
        target.write_text("file-read-content\n", encoding="utf-8")
        child_instruction = f"Read {target} and report the verified file content."
        appended_message = "private follow-up for the running child"
        second_parent_secret = "second-parent-private-secret"
        child_summary = "child summary includes the file and follow-up"

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="model-create-call",
                        name=AGENT_WORK_TOOL_NAME,
                        arguments={"operation": "create", "message": child_instruction},
                    )
                ],
            )
        )
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "subagent-message-create-request",
                "message": "Create the child task and keep the main conversation independent.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        first_parent_work_id = accepted_data["work_id"]

        first_parent_waiting = await runtime.run_next()
        assert first_parent_waiting is not None
        assert first_parent_waiting.id == first_parent_work_id
        assert first_parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child_rows = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(first_parent_work_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(child_rows) == 1
            child_work_id = child_rows[0].id
            subagent_id = child_rows[0].subagent_id
            assert child_work_id is not None
            assert isinstance(subagent_id, str) and subagent_id

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="model-child-read-call",
                        name=FILE_TOOL_NAME,
                        arguments={"operation": "read", "path": str(target)},
                    )
                ],
            )
        )
        child_request_started = asyncio.Event()
        release_child_request = asyncio.Event()
        fixture_post_json = OpenAIResponsesTransformer._post_json

        async def wrapped_post_json(_transformer, **kwargs):
            response = await fixture_post_json(_transformer, **kwargs)
            if not child_request_started.is_set():
                child_request_started.set()
                await release_child_request.wait()
            return response

        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", wrapped_post_json)
        child_task: asyncio.Task | None = None
        try:
            child_task = asyncio.create_task(runtime.run_next())
            await asyncio.wait_for(child_request_started.wait(), timeout=5)

            async with runtime.factory() as db:
                child = await db.get(SessionReplyWorkItem, child_work_id)
                assert child is not None
                assert child.status == SessionReplyWorkStatus.RUNNING
                tool_results = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.TOOL,
                                Message.type == MessageType.TOOL_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert tool_results == []

            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="model-send-message-call",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={
                                "operation": "send_message",
                                "subagent_id": subagent_id,
                                "message": appended_message,
                            },
                        )
                    ],
                )
            )
            second_parent_accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "subagent-message-send-request",
                    "message": f"Continue the main conversation with {second_parent_secret}.",
                },
            )
            assert second_parent_accepted.status_code == 200
            second_parent_data = second_parent_accepted.json()["data"]
            assert second_parent_data["submission_status"] == "accepted"
            second_parent_work_id = second_parent_data["work_id"]
            assert second_parent_work_id != first_parent_work_id

            second_parent_waiting = await runtime.run_next()
            assert second_parent_waiting is not None
            assert second_parent_waiting.id == second_parent_work_id
            assert second_parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async with runtime.factory() as db:
                children = (
                    (
                        await db.execute(
                            select(SessionReplyWorkItem).where(
                                SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                                SessionReplyWorkItem.subagent_id == subagent_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(children) == 1
                assert children[0].id == child_work_id

                rounds = (await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == subagent_id))).scalars().all()
                assert len(rounds) == 1
                assert rounds[0].child_work_id == child_work_id

                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.child_work_id == child_work_id).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                assert {call.parent_work_id for call in calls} == {
                    first_parent_work_id,
                    second_parent_work_id,
                }
                assert all(call.subagent_id == subagent_id for call in calls)
                assert all(call.child_work_id == child_work_id for call in calls)
                call_ids = [call.tool_call_id for call in calls]
                assert all(isinstance(call_id, str) and call_id for call_id in call_ids)
                assert len(set(call_ids)) == 2
                assert {call.arguments["operation"] for call in calls} == {"create", "send_message"}

                persisted_parent_calls = []
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
                for row in assistant_rows:
                    message = InternalMessage.model_validate_json(row.content or "{}")
                    persisted_parent_calls.extend(tool_call for tool_call in message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME)
                assert len(persisted_parent_calls) == 2
                assert {tool_call.id for tool_call in persisted_parent_calls} == set(call_ids)

                appended_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == runtime.session_id,
                                Message.uid == runtime.uid,
                                Message.profile_id == runtime.profile.id,
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.USER,
                                Message.type == MessageType.TEXT,
                                Message.content == appended_message,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(appended_rows) == 1
                assert appended_rows[0].is_processed is False

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
            release_child_request.set()
            child_done = await asyncio.wait_for(child_task, timeout=10)
            child_task = None
            assert child_done is not None
            assert child_done.id == child_work_id
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

            child_payloads = [payload for payload in runtime.provider_payloads if FILE_TOOL_NAME in _provider_tool_names(payload) and AGENT_WORK_TOOL_NAME not in _provider_tool_names(payload)]
            assert len(child_payloads) == 2
            first_child_payload = json.dumps(child_payloads[0], ensure_ascii=False)
            next_child_payload = json.dumps(child_payloads[1], ensure_ascii=False)
            assert appended_message not in first_child_payload
            assert next_child_payload.count(appended_message) == 1
            assert second_parent_secret not in first_child_payload
            assert second_parent_secret not in next_child_payload

            async with runtime.factory() as db:
                child = await db.get(SessionReplyWorkItem, child_work_id)
                first_parent = await db.get(SessionReplyWorkItem, first_parent_work_id)
                second_parent = await db.get(SessionReplyWorkItem, second_parent_work_id)
                assert child is not None and first_parent is not None and second_parent is not None
                assert child.status == SessionReplyWorkStatus.SUCCEEDED
                assert first_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert second_parent.status == SessionReplyWorkStatus.READY_FOR_LLM

                round_rows = (await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == subagent_id))).scalars().all()
                assert len(round_rows) == 1
                assert round_rows[0].result is not None
                assert round_rows[0].result["completed"] is True
                assert round_rows[0].result["status"] == "succeeded"
                assert round_rows[0].result["result"]["content"] == child_summary

                child_tool_results = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.TOOL,
                                Message.type == MessageType.TOOL_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(child_tool_results) == 1
                child_tool_result = InternalMessage.model_validate_json(child_tool_results[0].content or "{}")
                assert child_tool_result.tool_execution_status == "succeeded"
                assert json.loads(child_tool_result.content or "{}")["operation"] == "read"

                appended_rows = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == runtime.session_id,
                                Message.uid == runtime.uid,
                                Message.profile_id == runtime.profile.id,
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.USER,
                                Message.type == MessageType.TEXT,
                                Message.content == appended_message,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(appended_rows) == 1
                assert appended_rows[0].is_processed is True

                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.child_work_id == child_work_id).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                result_payloads = []
                for call in calls:
                    assert call.delivered is True
                    assert call.tool_result_message_id is not None
                    result_row = await db.get(Message, call.tool_result_message_id)
                    assert result_row is not None
                    result_message = InternalMessage.model_validate_json(result_row.content or "{}")
                    assert result_message.tool_call_id == call.tool_call_id
                    assert result_message.tool_execution_status == "succeeded"
                    result_payload = json.loads(result_message.content or "{}")
                    assert result_payload["status"] == "success"
                    assert result_payload["operation"] == call.arguments["operation"]
                    assert result_payload["subagent_id"] == subagent_id
                    assert result_payload["work_id"] == child_work_id
                    assert result_payload["child_work_id"] == child_work_id
                    assert result_payload["work"]["work_id"] == child_work_id
                    assert result_payload["work"]["result"]["content"] == child_summary
                    result_payloads.append(result_payload)
                assert {payload["operation"] for payload in result_payloads} == {"create", "send_message"}

            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, content="first parent resumed"),
                    InternalMessage(role=MessageRole.ASSISTANT, content="second parent resumed"),
                ]
            )
            first_parent_done = await asyncio.wait_for(runtime.run_next(), timeout=10)
            second_parent_done = await asyncio.wait_for(runtime.run_next(), timeout=10)
            assert first_parent_done is not None and first_parent_done.id == first_parent_work_id
            assert second_parent_done is not None and second_parent_done.id == second_parent_work_id
            assert first_parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert second_parent_done.status == SessionReplyWorkStatus.SUCCEEDED

            history_response = await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            )
            assert history_response.status_code == 200
            history = history_response.json()["data"]
            assert all(appended_message not in json.dumps(item, ensure_ascii=False) for item in history if item.get("type") == MessageType.TEXT.value)

            async with runtime.factory() as db:
                first_parent = await db.get(SessionReplyWorkItem, first_parent_work_id)
                second_parent = await db.get(SessionReplyWorkItem, second_parent_work_id)
                child = await db.get(SessionReplyWorkItem, child_work_id)
                assert first_parent is not None and second_parent is not None and child is not None
                assert first_parent.status == SessionReplyWorkStatus.SUCCEEDED
                assert second_parent.status == SessionReplyWorkStatus.SUCCEEDED
                assert child.status == SessionReplyWorkStatus.SUCCEEDED
                assert await db.get(Subagent, subagent_id) is not None
                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.child_work_id == child_work_id).order_by(SubagentToolCall.id))).scalars().all()
                assert len(calls) == 2
                assert {call.parent_work_id for call in calls} == {
                    first_parent_work_id,
                    second_parent_work_id,
                }
                assert all(call.delivered is True for call in calls)
        finally:
            release_child_request.set()
            if child_task is not None and not child_task.done():
                child_task.cancel()
            if child_task is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(child_task, timeout=5)


@pytest.mark.asyncio
async def test_completed_subagent_starts_new_round_with_previous_context(
    image_chat_runtime,
    tmp_path: Path,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        target = (tmp_path / "subagent-context.txt").resolve()
        file_content = "round-one-unique-file-content"
        target.write_text(f"{file_content}\n", encoding="utf-8")
        child_instruction = f"Read {target} and report the exact file content."
        first_summary = f"first-round summary: {file_content}"
        create_call = InternalToolCall(
            id="round-one-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "message": child_instruction},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]))

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "subagent-context-create-request",
                "message": "Create the child and complete the first parent task.",
            },
        )
        assert accepted.status_code == 200
        first_parent_work_id = accepted.json()["data"]["work_id"]
        first_parent_waiting = await runtime.run_next()
        assert first_parent_waiting is not None
        assert first_parent_waiting.id == first_parent_work_id
        assert first_parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(first_parent_work_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            old_child_work_id = child.id
            subagent_id = child.subagent_id
            assert old_child_work_id is not None
            assert isinstance(subagent_id, str) and subagent_id

        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="round-one-file-read-call",
                            name=FILE_TOOL_NAME,
                            arguments={"operation": "read", "path": str(target)},
                        )
                    ],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content=first_summary),
            ]
        )
        first_child_done = await runtime.run_next()
        assert first_child_done is not None
        assert first_child_done.id == old_child_work_id
        assert first_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            old_round = await db.get(SubagentWork, old_child_work_id)
            assert old_round is not None and old_round.result is not None
            old_round_result = deepcopy(old_round.result)
            assert old_round_result["completed"] is True
            assert old_round_result["status"] == "succeeded"
            assert old_round_result["result"]["content"] == first_summary

            tool_rows = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.work_id == old_child_work_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            read_payload = None
            for row in tool_rows:
                result_message = InternalMessage.model_validate_json(row.content or "{}")
                if result_message.tool_execution_status != "succeeded":
                    continue
                payload = json.loads(result_message.content or "{}")
                if payload.get("operation") == "read":
                    read_payload = payload
                    break
            assert read_payload is not None
            assert read_payload["status"] == "success"
            assert read_payload["path"] == str(target)
            assert file_content in read_payload["content"]

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="first parent completed"))
        first_parent_done = await runtime.run_next()
        assert first_parent_done is not None
        assert first_parent_done.id == first_parent_work_id
        assert first_parent_done.status == SessionReplyWorkStatus.SUCCEEDED

        new_parent_secret = "new-parent-private-secret"
        new_child_message = "Continue with the saved first-round context and report the new result."
        query_latest_call = InternalToolCall(
            id="round-two-query-latest-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "query", "subagent_id": subagent_id},
        )
        query_old_call = InternalToolCall(
            id="round-two-query-old-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "query", "subagent_id": subagent_id, "work_id": old_child_work_id},
        )
        send_call = InternalToolCall(
            id="round-two-send-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "send_message",
                "subagent_id": subagent_id,
                "message": new_child_message,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_latest_call, query_old_call]),
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[send_call]),
            ]
        )
        new_parent_accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "subagent-context-new-parent-request",
                "message": f"Start a new parent task with {new_parent_secret}.",
            },
        )
        assert new_parent_accepted.status_code == 200
        new_parent_work_id = new_parent_accepted.json()["data"]["work_id"]
        new_parent_waiting = await runtime.run_next()
        assert new_parent_waiting is not None
        assert new_parent_waiting.id == new_parent_work_id
        assert new_parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            new_child = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(new_parent_work_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            new_child_work_id = new_child.id
            assert new_child_work_id is not None
            assert new_child_work_id != old_child_work_id

            subagent = await db.get(Subagent, subagent_id)
            assert subagent is not None
            assert subagent.active_child_work_id == new_child_work_id
            assert subagent.latest_child_work_id == new_child_work_id

            rounds = (await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == subagent_id).order_by(SubagentWork.child_work_id))).scalars().all()
            assert {round_row.child_work_id for round_row in rounds} == {old_child_work_id, new_child_work_id}
            old_round = next(round_row for round_row in rounds if round_row.child_work_id == old_child_work_id)
            new_round = next(round_row for round_row in rounds if round_row.child_work_id == new_child_work_id)
            assert old_round.result == old_round_result
            assert old_round.current_work_id == old_child_work_id
            assert new_round.current_work_id == new_child_work_id
            assert new_round.result is None

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
            query_calls = []
            for row in assistant_rows:
                message = InternalMessage.model_validate_json(row.content or "{}")
                query_calls.extend(tool_call for tool_call in message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments.get("operation") == "query" and tool_call.arguments.get("subagent_id") == subagent_id)
            assert {tool_call.arguments.get("work_id") for tool_call in query_calls} == {
                None,
                old_child_work_id,
            }
            query_call_ids = {tool_call.id for tool_call in query_calls}

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
            query_results = {}
            for row in result_rows:
                result_message = InternalMessage.model_validate_json(row.content or "{}")
                if result_message.tool_call_id not in query_call_ids:
                    continue
                assert result_message.tool_execution_status == "succeeded"
                query_results[result_message.tool_call_id] = json.loads(result_message.content or "{}")
            assert set(query_results) == query_call_ids
            for payload in query_results.values():
                assert payload["status"] == "success"
                assert payload["operation"] == "query"
                assert payload["subagent_id"] == subagent_id
                assert payload["work"]["work_id"] == old_child_work_id
                assert payload["work"]["status"] == "succeeded"
                assert payload["work"]["result"]["content"] == first_summary

        second_summary = "second-round summary from saved context"
        child_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_summary))
        second_child_done = await runtime.run_next()
        assert second_child_done is not None
        assert second_child_done.id == new_child_work_id
        assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

        child_provider_dump = json.dumps(runtime.provider_payloads[child_provider_start:], ensure_ascii=False)
        assert file_content in child_provider_dump
        assert first_summary in child_provider_dump
        assert new_child_message in child_provider_dump
        assert new_parent_secret not in child_provider_dump

        async with runtime.factory() as db:
            new_parent = await db.get(SessionReplyWorkItem, new_parent_work_id)
            new_round = await db.get(SubagentWork, new_child_work_id)
            old_round = await db.get(SubagentWork, old_child_work_id)
            assert new_parent is not None and new_round is not None and old_round is not None
            assert new_parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert new_round.result is not None
            assert new_round.result["result"]["content"] == second_summary
            assert old_round.result == old_round_result

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="second parent completed"))
        new_parent_done = await runtime.run_next()
        assert new_parent_done is not None
        assert new_parent_done.id == new_parent_work_id
        assert new_parent_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            first_parent = await db.get(SessionReplyWorkItem, first_parent_work_id)
            new_parent = await db.get(SessionReplyWorkItem, new_parent_work_id)
            old_round = await db.get(SubagentWork, old_child_work_id)
            new_round = await db.get(SubagentWork, new_child_work_id)
            assert first_parent is not None and new_parent is not None
            assert old_round is not None and new_round is not None
            assert first_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert new_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert old_round.result == old_round_result
            assert new_round.result is not None
            assert new_round.result["result"]["content"] == second_summary

            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
            assert [(call.arguments["operation"], call.child_work_id) for call in calls] == [
                ("create", old_child_work_id),
                ("send_message", new_child_work_id),
            ]
            assert calls[0].parent_work_id == first_parent_work_id
            assert calls[1].parent_work_id == new_parent_work_id
            assert calls[0].tool_call_id != calls[1].tool_call_id
            assert all(call.delivered is True for call in calls)

            subagent = await db.get(Subagent, subagent_id)
            assert subagent is not None
            assert subagent.active_child_work_id is None
            assert subagent.latest_child_work_id == new_child_work_id


@pytest.mark.asyncio
async def test_saved_child_text_with_late_message_stays_in_same_round(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[InternalToolCall(id="model-create-race", name=AGENT_WORK_TOOL_NAME, arguments={"operation": "create", "message": "完成一个独立子任务。"})]))
        accepted = await runtime.client.post("/api/v1/chat/completions", json={"session_id": runtime.session_id, "request_id": "saved-child-text-create", "message": "创建子任务并等待结果。"})
        assert accepted.status_code == 200
        first_parent_work_id = accepted.json()["data"]["work_id"]
        waiting = await runtime.run_next()
        assert waiting is not None and waiting.id == first_parent_work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(first_parent_work_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            child_work_id, subagent_id = child.id, child.subagent_id
            assert child_work_id is not None and isinstance(subagent_id, str) and subagent_id

        first_text, late_message, final_text = "首轮已保存但尚未交付", "首轮边界之后追加的消息", "同一轮处理追加消息后的最终文本"
        boundary, release = asyncio.Event(), asyncio.Event()
        original_prepare = executor_lifecycle_module.prepare_child_completion

        async def wrapped_prepare(db, *, work, worker_id, result_message_id, skip_reply):
            if work.id == child_work_id and not boundary.is_set():
                boundary.set()
                await release.wait()
            return await original_prepare(db, work=work, worker_id=worker_id, result_message_id=result_message_id, skip_reply=skip_reply)

        monkeypatch.setattr(executor_lifecycle_module, "prepare_child_completion", wrapped_prepare)
        child_task: asyncio.Task | None = None
        try:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_text))
            child_task = asyncio.create_task(runtime.run_next(independent_only=True))
            await asyncio.wait_for(boundary.wait(), timeout=10)
            async with runtime.factory() as db:
                saved = (
                    (
                        await db.execute(
                            select(Message).where(
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.ASSISTANT,
                                Message.type == MessageType.TEXT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                child, round_row = await db.get(SessionReplyWorkItem, child_work_id), await db.get(SubagentWork, child_work_id)
                assert [row.content for row in saved] == [first_text]
                assert child is not None and child.status == SessionReplyWorkStatus.RUNNING
                assert round_row is not None and round_row.result is None

            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="model-send-race",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={"operation": "send_message", "subagent_id": subagent_id, "message": late_message},
                        )
                    ],
                )
            )
            second_accepted = await runtime.client.post("/api/v1/chat/completions", json={"session_id": runtime.session_id, "request_id": "saved-child-text-send", "message": "另一个父任务追加消息。"})
            assert second_accepted.status_code == 200
            second_parent_work_id = second_accepted.json()["data"]["work_id"]
            second_waiting = await runtime.run_next(independent_only=False)
            assert second_waiting is not None and second_waiting.id == second_parent_work_id
            assert second_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async def load_state():
                async with runtime.factory() as db:
                    children = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.subagent_id == subagent_id))).scalars().all()
                    rounds = (await db.execute(select(SubagentWork).where(SubagentWork.subagent_id == subagent_id))).scalars().all()
                    calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
                    parents = [await db.get(SessionReplyWorkItem, work_id) for work_id in (first_parent_work_id, second_parent_work_id)]
                    late_rows = (await db.execute(select(Message).where(Message.work_id == child_work_id, Message.content == late_message))).scalars().all()
                    return children, rounds, calls, parents, late_rows

            children, rounds, calls, parents, late_rows = await load_state()
            assert len(children) == len(rounds) == 1
            assert [(call.arguments["operation"], call.child_work_id) for call in calls] == [("create", child_work_id), ("send_message", child_work_id)]
            assert calls[0].tool_call_id and all(call.delivered is False for call in calls)
            assert all(parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK for parent in parents)
            assert len(late_rows) == 1 and late_rows[0].is_processed is False

            release.set()
            child_done = await asyncio.wait_for(child_task, timeout=10)
            child_task = None
            assert child_done is not None and child_done.id == child_work_id
            assert child_done.status == SessionReplyWorkStatus.READY_FOR_LLM
            children, rounds, calls, parents, late_rows = await load_state()
            assert children[0].status == SessionReplyWorkStatus.READY_FOR_LLM and rounds[0].result is None
            assert all(parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK for parent in parents)
            assert len(late_rows) == 1 and late_rows[0].is_processed is False

            provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=final_text))
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None and child_done.id == child_work_id
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
            child_dump = json.dumps(runtime.provider_payloads[provider_start:], ensure_ascii=False)
            assert first_text in child_dump and child_dump.count(late_message) == 1

            children, rounds, calls, parents, late_rows = await load_state()
            assert len(children) == len(rounds) == 1 and rounds[0].result["result"]["content"] == final_text
            assert len(calls) == 2 and all(call.delivered is True for call in calls)
            assert len({call.tool_result_message_id for call in calls}) == 2
            async with runtime.factory() as db:
                for call in calls:
                    result_row = await db.get(Message, call.tool_result_message_id)
                    assert result_row is not None
                    result_message = InternalMessage.model_validate_json(result_row.content or "{}")
                    result_payload = json.loads(result_message.content or "{}")
                    assert result_message.tool_call_id == call.tool_call_id
                    assert result_payload["work"]["result"]["content"] == final_text != first_text
            assert len(late_rows) == 1 and late_rows[0].is_processed is True

            runtime.outputs.extend([InternalMessage(role=MessageRole.ASSISTANT, content="原父完成"), InternalMessage(role=MessageRole.ASSISTANT, content="追加父完成")])
            first_done, second_done = await runtime.run_next(independent_only=False), await runtime.run_next(independent_only=False)
            assert first_done is not None and second_done is not None
            assert {first_done.id, second_done.id} == {first_parent_work_id, second_parent_work_id}
            assert first_done.status == second_done.status == SessionReplyWorkStatus.SUCCEEDED
        finally:
            release.set()
            if child_task is not None and not child_task.done():
                child_task.cancel()
            if child_task is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(child_task, timeout=5)
