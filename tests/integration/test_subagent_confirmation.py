import asyncio
import contextlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, ERR_SUBAGENT_CANCELLED, FILE_TOOL_NAME
from app.core.i18n import t
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from app.providers.llm.client import LLMClient


@pytest.mark.parametrize("decision", ["同意", "拒绝"], ids=["approve", "reject"])
@pytest.mark.asyncio
async def test_subagent_file_write_confirmation_round_returns_to_parent(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    decision: str,
) -> None:
    async with image_chat_runtime(goal_mode=False) as runtime:
        target = (tmp_path / "subagent-confirmation.txt").resolve()
        child_instruction = f"独立将 {target} 写入 exactly 'confirmed\n'，完成后报告子轮总结。"

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            configs["security"].update(
                {
                    "audit_channel_id": 1,
                    "audit_model_id": runtime.chat_model_id,
                    "audit_threshold": 5,
                    "protect_system_directories": False,
                }
            )
            profile.configs = ProfileConfig.model_validate(configs).model_dump(mode="json")
            await db.commit()

        original_generate = LLMClient.generate

        async def generate(**kwargs: Any) -> InternalResponse:
            for message in kwargs["messages"]:
                if message.role != MessageRole.USER or not isinstance(message.content, str):
                    continue
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if "audit_results" in payload:
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            content="子代理文件写入需要人工确认。",
                        ),
                        model=runtime.chat_model_id,
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        finish_reason="stop",
                    )
                if "confirmation_threshold" not in payload or not isinstance(payload.get("tool_calls"), list):
                    continue
                tool_call_ids = [item.get("tool_call_id") for item in payload["tool_calls"] if isinstance(item, dict) and isinstance(item.get("tool_call_id"), str)]
                assert tool_call_ids
                return InternalResponse(
                    message=InternalMessage(
                        role=MessageRole.ASSISTANT,
                        content=json.dumps(
                            {
                                "results": [
                                    {
                                        "tool_call_id": tool_call_id,
                                        "score": 5,
                                        "reason": "需要人工确认",
                                        "file_checks": [],
                                        "deny": False,
                                    }
                                    for tool_call_id in tool_call_ids
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    model=runtime.chat_model_id,
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    finish_reason="stop",
                )
            return await original_generate(**kwargs)

        monkeypatch.setattr(LLMClient, "generate", generate)

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="model-parent-create-id",
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
                "request_id": "subagent-confirmation-parent-request",
                "message": "请将文件任务交给子代理并在结果返回后完成父任务。",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]

        parent_waiting = await runtime.run_next()
        assert parent_waiting is not None
        assert parent_waiting.id == parent_work_id
        assert parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
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
            subagent = await db.get(Subagent, child.subagent_id)
            assert subagent is not None
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            original_parent_tool_call_id = call.tool_call_id
            round_row = await db.get(SubagentWork, child_work_id)
            assert round_row is not None
            assert round_row.current_work_id == child_work_id

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="model-child-write-id",
                        name=FILE_TOOL_NAME,
                        arguments={
                            "operation": "write",
                            "path": str(target),
                            "content": "confirmed\n",
                        },
                    )
                ],
            )
        )
        child_waiting = await runtime.run_next()
        assert child_waiting is not None
        assert child_waiting.id == child_work_id
        assert child_waiting.status == SessionReplyWorkStatus.SUCCEEDED
        assert not target.exists()

        confirmation_history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert confirmation_history_response.status_code == 200
        confirmation_history = confirmation_history_response.json()["data"]
        assert any(item.get("type") == MessageType.AUDIT_CONFIRMATION.value for item in confirmation_history)
        assert all(item.get("content") != child_instruction for item in confirmation_history)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            round_row = await db.get(SubagentWork, child_work_id)
            audit_record = (await db.execute(select(AuditRecord).where(AuditRecord.source_work_id == child_work_id))).scalars().one()
            assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert child is not None and child.status == SessionReplyWorkStatus.SUCCEEDED
            assert round_row is not None
            assert round_row.result is None
            assert round_row.waiting_confirmation is True
            assert audit_record.status == AuditRecordStatus.PENDING
            audit_record_id = audit_record.id

        decision_response = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"subagent-confirmation-{decision}",
                "message": decision,
            },
        )
        assert decision_response.status_code == 200
        decision_data = decision_response.json()["data"]
        assert decision_data["submission_status"] == ("approved" if decision == "同意" else "rejected")
        decision_work_id = decision_data["work_id"]

        if decision == "同意":
            async with runtime.factory() as db:
                confirmed_work = await db.get(SessionReplyWorkItem, decision_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
                assert confirmed_work is not None
                assert confirmed_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
                assert confirmed_work.source_type == SessionReplySourceType.AUDIT_RECORD
                assert confirmed_work.subagent_id == subagent.id
                assert confirmed_work.execution_state["child_work_id"] == child_work_id
                assert confirmed_work.work_category == SessionReplyWorkCategory.SUBAGENT
                assert round_row is not None
                assert round_row.current_work_id == decision_work_id
                assert round_row.waiting_confirmation is False

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="子轮总结"))
            confirmed_done = await runtime.run_next()
            assert confirmed_done is not None
            assert confirmed_done.id == decision_work_id
            assert confirmed_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert target.read_text(encoding="utf-8") == "confirmed\n"

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                confirmed_work = await db.get(SessionReplyWorkItem, decision_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
                call = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == parent_work_id,
                                SubagentToolCall.tool_call_id == original_parent_tool_call_id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                result_row = await db.get(Message, call.tool_result_message_id)
                record = await db.get(AuditRecord, audit_record_id)
                assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert confirmed_work is not None and confirmed_work.status == SessionReplyWorkStatus.SUCCEEDED
                assert round_row is not None
                assert round_row.current_work_id == decision_work_id
                assert round_row.result is not None
                assert round_row.result["status"] == "succeeded"
                assert round_row.result["result"]["content"] == "子轮总结"
                assert call.delivered is True
                assert result_row is not None
                result_message = InternalMessage.model_validate_json(result_row.content or "{}")
                result_payload = json.loads(result_message.content or "{}")
                assert result_message.tool_call_id == original_parent_tool_call_id
                assert result_message.tool_execution_status == "succeeded"
                assert result_payload["status"] == "success"
                assert result_payload["subagent_id"] == subagent.id
                assert result_payload["child_work_id"] == child_work_id
                assert result_payload["work"]["result"]["content"] == "子轮总结"
                assert record is not None and record.status == AuditRecordStatus.SUCCEEDED

            parent_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="父任务已完成"))
            parent_done = await runtime.run_next()
            assert parent_done is not None
            assert parent_done.id == parent_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert any("子轮总结" in json.dumps(payload, ensure_ascii=False) for payload in runtime.provider_payloads[parent_provider_start:])

            final_history_response = await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            )
            assert final_history_response.status_code == 200
            final_history = final_history_response.json()["data"]
            assert any(item.get("content") == "父任务已完成" for item in final_history)
            assert all(item.get("content") != child_instruction for item in final_history)
        else:
            async with runtime.factory() as db:
                rejected_work = await db.get(SessionReplyWorkItem, decision_work_id)
                record = await db.get(AuditRecord, audit_record_id)
                assert rejected_work is not None
                assert rejected_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
                assert record is not None and record.status == AuditRecordStatus.REJECTED

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="已拒绝该文件写入"))
            rejected_done = await runtime.run_next()
            assert rejected_done is not None
            assert rejected_done.id == decision_work_id
            assert rejected_done.status == SessionReplyWorkStatus.SUCCEEDED

            await SessionReplyConsumer()._recover_expired()
            assert not target.exists()

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
                call = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == parent_work_id,
                                SubagentToolCall.tool_call_id == original_parent_tool_call_id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                result_row = await db.get(Message, call.tool_result_message_id)
                record = await db.get(AuditRecord, audit_record_id)
                assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert round_row is not None
                assert round_row.current_work_id == child_work_id
                assert round_row.waiting_confirmation is False
                assert round_row.result is not None
                assert round_row.result["status"] == "cancelled"
                assert call.delivered is True
                assert result_row is not None
                result_message = InternalMessage.model_validate_json(result_row.content or "{}")
                result_payload = json.loads(result_message.content or "{}")
                assert result_message.tool_call_id == original_parent_tool_call_id
                assert result_message.tool_execution_status == "failed"
                assert result_payload["status"] == "failed"
                assert result_payload["error"] == t(ERR_SUBAGENT_CANCELLED)
                assert result_payload["work"]["status"] == "cancelled"
                assert result_payload["work"]["error"] == t(ERR_SUBAGENT_CANCELLED)
                assert record is not None and record.status == AuditRecordStatus.REJECTED

            parent_provider_start = len(runtime.provider_payloads)
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="父任务已收到取消结果"))
            parent_done = await runtime.run_next()
            assert parent_done is not None
            assert parent_done.id == parent_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            assert any(t(ERR_SUBAGENT_CANCELLED) in json.dumps(payload, ensure_ascii=False) for payload in runtime.provider_payloads[parent_provider_start:])

            final_history_response = await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            )
            assert final_history_response.status_code == 200
            final_history = final_history_response.json()["data"]
            assert any(item.get("content") == "父任务已收到取消结果" for item in final_history)
            assert all(item.get("content") != child_instruction for item in final_history)


@pytest.mark.parametrize("work_state", ["queued", "running"], ids=["queued", "running"])
@pytest.mark.asyncio
async def test_subagent_cancel_round_returns_cancelled_result_to_parent(
    image_chat_runtime,
    tmp_path: Path,
    work_state: str,
) -> None:
    from app.core.crud.session.reply_work_item import session_reply_work_item_crud
    from app.core.session_reply_queue.executor_lifecycle import execute_session_reply_work

    async with image_chat_runtime(goal_mode=False) as runtime:
        target = (tmp_path / f"subagent-cancel-{work_state}.txt").resolve()
        child_instruction = f"Independently write exactly 'cancelled-child\\n' to {target} using the file tool, then report completion."

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = ProfileConfig.model_validate(configs).model_dump(mode="json")
            await db.commit()

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="model-create-cancel-id",
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
                "request_id": f"subagent-cancel-create-{work_state}",
                "message": "创建一个独立子代理任务，然后等待其结果。",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        parent_waiting = await runtime.run_next()
        assert parent_waiting is not None
        assert parent_waiting.id == parent_work_id
        assert parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
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
            assert child.status == SessionReplyWorkStatus.READY_FOR_LLM
            child_work_id = child.id
            assert isinstance(child.subagent_id, str) and child.subagent_id
            subagent_id = child.subagent_id
            subagent = await db.get(Subagent, subagent_id)
            assert subagent is not None
            assert subagent.active_child_work_id == child_work_id
            round_row = await db.get(SubagentWork, child_work_id)
            assert round_row is not None
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            original_tool_call_id = call.tool_call_id
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
            assert instruction_rows[0].is_processed is False

        child_worker_id: str | None = None
        if work_state == "running":
            child_worker_id = "subagent-cancel-old-worker"
            async with runtime.factory() as db:
                claimed_child = await session_reply_work_item_crud.claim_next(
                    db,
                    worker_id=child_worker_id,
                    lease_seconds=300,
                )
            assert claimed_child is not None
            assert claimed_child.id == child_work_id
            assert claimed_child.status == SessionReplyWorkStatus.RUNNING

        cancel_call = InternalToolCall(
            id="model-cancel-id",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "cancel",
                "subagent_id": subagent_id,
                "work_id": child_work_id,
            },
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[cancel_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content="取消请求已完成"),
            ]
        )
        cancel_accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": f"subagent-cancel-request-{work_state}",
                "message": "取消指定的子代理工作。",
            },
        )
        assert cancel_accepted.status_code == 200
        cancel_work_id = cancel_accepted.json()["data"]["work_id"]
        cancel_provider_start = len(runtime.provider_payloads)
        cancel_done = await runtime.run_next(independent_only=False)
        assert cancel_done is not None
        assert cancel_done.id == cancel_work_id
        assert cancel_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert any(t(ERR_SUBAGENT_CANCELLED) in json.dumps(payload, ensure_ascii=False) for payload in runtime.provider_payloads[cancel_provider_start:])

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            subagent = await db.get(Subagent, subagent_id)
            round_row = await db.get(SubagentWork, child_work_id)
            call = (
                (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.tool_call_id == original_tool_call_id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert child is not None and child.status == SessionReplyWorkStatus.CANCELLED
            assert child.locked_by is None and child.lock_until is None
            assert subagent is not None and subagent.active_child_work_id is None
            assert round_row is not None
            assert round_row.result is not None
            assert round_row.result["completed"] is True
            assert round_row.result["status"] == "cancelled"
            assert round_row.result["error"] == t(ERR_SUBAGENT_CANCELLED)
            assert call.delivered is True
            assert call.tool_result_message_id is not None
            result_row = await db.get(Message, call.tool_result_message_id)
            assert result_row is not None
            result_message = InternalMessage.model_validate_json(result_row.content or "{}")
            result_payload = json.loads(result_message.content or "{}")
            assert result_message.tool_call_id == original_tool_call_id
            assert result_message.tool_execution_status == "failed"
            assert result_payload["status"] == "failed"
            assert result_payload["operation"] == "create"
            assert result_payload["subagent_id"] == subagent_id
            assert result_payload["child_work_id"] == child_work_id
            assert result_payload["error"] == t(ERR_SUBAGENT_CANCELLED)
            assert result_payload["work"]["status"] == "cancelled"
            assert result_payload["work"]["error"] == t(ERR_SUBAGENT_CANCELLED)
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
            assert instruction_rows[0].is_processed is False
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
            assert child_tool_results == []

        assert not target.exists()

        if work_state == "running":
            assert child_worker_id is not None
            await execute_session_reply_work(child_work_id, child_worker_id)
            assert not target.exists()
            async with runtime.factory() as db:
                child = await db.get(SessionReplyWorkItem, child_work_id)
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
                assert child is not None and child.status == SessionReplyWorkStatus.CANCELLED
                assert child_tool_results == []

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="原父已收到取消原因并完成"))
        parent_done = await runtime.run_next()
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert any(t(ERR_SUBAGENT_CANCELLED) in json.dumps(payload, ensure_ascii=False) for payload in runtime.provider_payloads[parent_provider_start:])

        final_history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id, "size": 100},
        )
        assert final_history_response.status_code == 200
        final_history = final_history_response.json()["data"]
        assert any(item.get("content") == "原父已收到取消原因并完成" for item in final_history)
        assert all(item.get("content") != child_instruction for item in final_history)


@pytest.mark.asyncio
async def test_running_subagent_file_tool_cancel_keeps_late_result_private(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.tools.file_tool import FileToolExecutor

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        target = (tmp_path / "subagent-late-file-tool.txt").resolve()
        private_marker = "subagent-late-file-tool-result-marker-7f4c"
        child_instruction = f"Independently use file_tool to write a file at {target}, then report completion."

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = ProfileConfig.model_validate(configs).model_dump(mode="json")
            await db.commit()

        original_execute = FileToolExecutor.execute
        file_tool_started = asyncio.Event()
        release_file_tool = asyncio.Event()
        file_tool_finished = asyncio.Event()

        async def gated_execute(executor: FileToolExecutor, *args: Any, **kwargs: Any) -> str:
            file_tool_started.set()
            await release_file_tool.wait()
            try:
                return await original_execute(executor, *args, **kwargs)
            finally:
                file_tool_finished.set()

        monkeypatch.setattr(FileToolExecutor, "execute", gated_execute)

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="model-create-late-file-tool-id",
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
                "request_id": "subagent-late-file-tool-create",
                "message": "Create an independent child task and wait for it.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]
        parent_waiting = await runtime.run_next()
        assert parent_waiting is not None
        assert parent_waiting.id == parent_work_id
        assert parent_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
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
            assert isinstance(child.subagent_id, str) and child.subagent_id
            subagent_id = child.subagent_id
            call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().one()
            original_parent_tool_call_id = call.tool_call_id

        child_file_call_id = "model-child-late-file-write-id"
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id=child_file_call_id,
                        name=FILE_TOOL_NAME,
                        arguments={
                            "operation": "write",
                            "path": str(target),
                            "content": private_marker,
                        },
                    )
                ],
            )
        )

        child_task: asyncio.Task | None = None
        try:
            child_task = asyncio.create_task(runtime.run_next(independent_only=True))
            await asyncio.wait_for(file_tool_started.wait(), timeout=10)

            async with runtime.factory() as db:
                assistant_tool_call_rows = (
                    (
                        await db.execute(
                            select(Message)
                            .where(
                                Message.work_id == child_work_id,
                                Message.role == MessageRole.ASSISTANT,
                                Message.type == MessageType.TOOL_CALL,
                            )
                            .order_by(Message.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                matching_child_tool_calls: list[InternalToolCall] = []
                for row in assistant_tool_call_rows:
                    stored_assistant = InternalMessage.model_validate_json(row.content or "{}")
                    for tool_call in stored_assistant.tool_calls or []:
                        if tool_call.name == FILE_TOOL_NAME and tool_call.arguments.get("operation") == "write" and tool_call.arguments.get("path") == str(target):
                            matching_child_tool_calls.append(tool_call)
                assert len(matching_child_tool_calls) == 1
                actual_child_file_call_id = matching_child_tool_calls[0].id

            cancel_call = InternalToolCall(
                id="model-cancel-late-file-tool-id",
                name=AGENT_WORK_TOOL_NAME,
                arguments={
                    "operation": "cancel",
                    "subagent_id": subagent_id,
                    "work_id": child_work_id,
                },
            )
            runtime.outputs.extend(
                [
                    InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[cancel_call]),
                    InternalMessage(role=MessageRole.ASSISTANT, content="取消前台工作已完成"),
                ]
            )
            cancel_accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "subagent-late-file-tool-cancel",
                    "message": "Cancel the running child task and finish this foreground request.",
                },
            )
            assert cancel_accepted.status_code == 200
            cancel_work_id = cancel_accepted.json()["data"]["work_id"]
            cancel_done = await runtime.run_next(independent_only=False)
            assert cancel_done is not None
            assert cancel_done.id == cancel_work_id
            assert cancel_done.status == SessionReplyWorkStatus.SUCCEEDED

            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                child = await db.get(SessionReplyWorkItem, child_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
                call = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == parent_work_id,
                                SubagentToolCall.tool_call_id == original_parent_tool_call_id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child is not None and child.status == SessionReplyWorkStatus.CANCELLED
                assert child.locked_by is None and child.lock_until is None
                assert round_row is not None and round_row.result is not None
                assert round_row.result["completed"] is True
                assert round_row.result["status"] == "cancelled"
                assert round_row.result["error"] == t(ERR_SUBAGENT_CANCELLED)
                assert call.delivered is True
                assert call.tool_result_message_id is not None
                cancellation_result_row = await db.get(Message, call.tool_result_message_id)
                assert cancellation_result_row is not None
                cancellation_result_content = cancellation_result_row.content
                cancellation_result_id = cancellation_result_row.id
                cancellation_result = InternalMessage.model_validate_json(cancellation_result_row.content or "{}")
                cancellation_payload = json.loads(cancellation_result.content or "{}")
                assert cancellation_result.tool_call_id == original_parent_tool_call_id
                assert cancellation_result.tool_execution_status == "failed"
                assert cancellation_payload["status"] == "failed"
                assert cancellation_payload["operation"] == "create"
                assert cancellation_payload["child_work_id"] == child_work_id
                assert cancellation_payload["error"] == t(ERR_SUBAGENT_CANCELLED)
                assert cancellation_payload["work"]["status"] == "cancelled"

            release_file_tool.set()
            await asyncio.wait_for(file_tool_finished.wait(), timeout=10)
            assert target.read_text(encoding="utf-8") == private_marker
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(child_task, timeout=10)
            child_task = None

            late_row_ids: set[int] = set()
            async with runtime.factory() as db:
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                child = await db.get(SessionReplyWorkItem, child_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
                call = (
                    (
                        await db.execute(
                            select(SubagentToolCall).where(
                                SubagentToolCall.parent_work_id == parent_work_id,
                                SubagentToolCall.tool_call_id == original_parent_tool_call_id,
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child is not None and child.status == SessionReplyWorkStatus.CANCELLED
                assert round_row is not None and round_row.result is not None
                assert round_row.result["status"] == "cancelled"
                assert call.delivered is True
                assert call.tool_result_message_id == cancellation_result_id
                result_row = await db.get(Message, call.tool_result_message_id)
                assert result_row is not None
                assert result_row.content == cancellation_result_content
                tool_result_rows = (await db.execute(select(Message).where(Message.role == MessageRole.TOOL, Message.type == MessageType.TOOL_RESULT))).scalars().all()
                late_rows = []
                for row in tool_result_rows:
                    stored = InternalMessage.model_validate_json(row.content or "{}")
                    if stored.tool_call_id == actual_child_file_call_id:
                        late_rows.append(row)
                if late_rows:
                    assert all(row.work_id == child_work_id for row in late_rows)
                    for row in late_rows:
                        assert row.id is not None
                        late_row_ids.add(row.id)

            parent_provider_start = len(runtime.provider_payloads)
            parent_final = "原父在取消结果后恢复完成"
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=parent_final))
            parent_done = await runtime.run_next(independent_only=False)
            assert parent_done is not None
            assert parent_done.id == parent_work_id
            assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
            parent_provider_dump = json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)
            assert private_marker not in parent_provider_dump
            assert actual_child_file_call_id not in parent_provider_dump

            history_response = await runtime.client.get(
                "/api/v1/chat/sessions/history",
                params={"session_id": runtime.session_id, "size": 100},
            )
            assert history_response.status_code == 200
            history = history_response.json()["data"]
            assert any(item.get("content") == parent_final for item in history)
            assert private_marker not in json.dumps(history, ensure_ascii=False)
            assert all(item.get("id") not in late_row_ids for item in history)
        finally:
            release_file_tool.set()
            if child_task is not None and not child_task.done():
                child_task.cancel()
            if child_task is not None:
                with contextlib.suppress(BaseException):
                    await child_task
