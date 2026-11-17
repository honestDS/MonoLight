import asyncio
import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.session_reply_queue.child_delivery import (
    SUBAGENT_RESULT_NOTIFICATION_KIND,
    build_subagent_notification_key,
    resume_child_work_for_parent,
)
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork


@pytest.mark.parametrize("interruption_point", ["before-result-save", "after-result-save", "after-unknown-result-save"])
@pytest.mark.parametrize("message_padding", ["", " \n"])
@pytest.mark.asyncio
async def test_agent_work_parent_commit_interrupt_recovers_submission_without_duplicate_submission(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    interruption_point: str,
    message_padding: str,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            enabled_tools = list(configs["tool"]["enabled_tools"])
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*enabled_tools, AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        child_instruction = f"{message_padding}Complete the independent child task and report the final text.{message_padding}"
        create_call = InternalToolCall(
            id="input-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "恢复子任务", "message": child_instruction},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]))

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "agent-work-recovery-request",
                "message": "Create the child task and then complete the parent task.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]

        original_save_tool_response = interactive_tools_module.save_tool_response
        saved_result_id: int | None = None

        async def interrupt_queued_tool_result(*args, **kwargs):
            nonlocal saved_result_id
            tool_result = kwargs.get("tool_res")
            if tool_result is None and len(args) > 4:
                tool_result = args[4]
            try:
                payload = json.loads(tool_result.content or "{}")
            except (AttributeError, TypeError, ValueError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("status") == "queued"
                and set(payload) == {"status", "work_id", "subagent_id"}
                and isinstance(payload.get("work_id"), int)
                and not isinstance(payload.get("work_id"), bool)
                and payload.get("work_id") > 0
                and isinstance(payload.get("subagent_id"), str)
                and payload.get("subagent_id")
            ):
                if interruption_point == "after-unknown-result-save":
                    tool_result.content = json.dumps(
                        {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED},
                        ensure_ascii=False,
                    )
                    tool_result.tool_execution_status = "execution_unknown"
                if interruption_point in {"after-result-save", "after-unknown-result-save"}:
                    saved_result = await original_save_tool_response(*args, **kwargs)
                    saved_result_id = saved_result.id
                raise asyncio.CancelledError("interrupted around queued child result save")
            return await original_save_tool_response(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", interrupt_queued_tool_result)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next()

        saved_result_content: str | None = None
        saved_result_suffix: str | None = None
        saved_result_execution_status: str | None = None
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.locked_by is not None
            assert parent.execution_state.get("has_child_work") is True

            children = (
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
                .all()
            )
            assert len(children) == 1
            child_work_id = children[0].id
            assert child_work_id is not None

            subagents = (
                (
                    await db.execute(
                        select(Subagent).where(
                            Subagent.uid == parent.uid,
                            Subagent.session_id == parent.session_id,
                            Subagent.profile_id == parent.profile_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(subagents) == 1
            child_rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child_work_id))).scalars().all()
            assert len(child_rounds) == 1
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1
            call = calls[0]
            assert call.id is not None
            assert isinstance(call.tool_call_id, str) and call.tool_call_id
            actual_tool_call_id = call.tool_call_id
            assert call.delivered is False

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            assert isinstance(checkpoint.get("messages"), list)
            checkpoint_messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]
            checkpoint_assistants = [message for message in checkpoint_messages if message.role == MessageRole.ASSISTANT]
            assert sum(any(tool_call.id == actual_tool_call_id for tool_call in message.tool_calls or []) for message in checkpoint_assistants) == 1
            initial_counts = (len(subagents), len(children), len(child_rounds), len(calls))
            if interruption_point in {"after-result-save", "after-unknown-result-save"}:
                assert saved_result_id is not None
                saved_result_row = await db.get(Message, saved_result_id)
                assert saved_result_row is not None
                saved_result_message = InternalMessage.model_validate_json(saved_result_row.content or "{}")
                assert saved_result_message.tool_call_id == actual_tool_call_id
                saved_result_content = saved_result_row.content
                saved_result_suffix = saved_result_row.model_context_suffix
                saved_result_execution_status = saved_result_message.tool_execution_status
                if interruption_point == "after-unknown-result-save":
                    assert saved_result_execution_status == "execution_unknown"
                    assert json.loads(saved_result_message.content or "{}") == {
                        "status": "execution_unknown",
                        "error": PROMPT_TOOL_INTERRUPTED,
                    }

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            parent.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        provider_payload_count = len(runtime.provider_payloads)

        recovered_done = await runtime.run_next()
        assert recovered_done is not None
        assert recovered_done.id == parent_work_id
        assert recovered_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == provider_payload_count

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1
            call = calls[0]
            assert call.tool_call_id == actual_tool_call_id
            assert call.tool_result_message_id is not None
            queued_result_id = call.tool_result_message_id
            if interruption_point in {"after-result-save", "after-unknown-result-save"}:
                assert saved_result_id is not None
                assert queued_result_id == saved_result_id

            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            matching_results = []
            for row in result_rows:
                result = InternalMessage.model_validate_json(row.content or "{}")
                if result.tool_call_id == actual_tool_call_id:
                    matching_results.append((row, result, json.loads(result.content or "{}")))
            assert len(matching_results) == 1
            queued_row, queued_result, queued_payload = matching_results[0]
            queued_result_content = queued_row.content
            queued_result_suffix = queued_row.model_context_suffix
            assert queued_row.id == queued_result_id
            assert queued_result.tool_call_id == actual_tool_call_id
            if interruption_point in {"after-result-save", "after-unknown-result-save"}:
                assert queued_row.content == saved_result_content
                assert queued_row.model_context_suffix == saved_result_suffix
                assert queued_result.tool_execution_status == saved_result_execution_status
            if interruption_point == "after-unknown-result-save":
                assert queued_payload == {
                    "status": "execution_unknown",
                    "error": PROMPT_TOOL_INTERRUPTED,
                }
            else:
                assert queued_payload == {
                    "status": "queued",
                    "work_id": child_work_id,
                    "subagent_id": calls[0].subagent_id,
                }

            child_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(child_checkpoint, dict)
            assert isinstance(child_checkpoint.get("messages"), list)
            checkpoint_results = [InternalMessage.model_validate(message) for message in child_checkpoint["messages"] if isinstance(message, dict) and message.get("role") == MessageRole.TOOL and message.get("tool_call_id") == actual_tool_call_id]
            assert len(checkpoint_results) == 1
            checkpoint_result = checkpoint_results[0]
            assert checkpoint_result.id == queued_result_id
            assert checkpoint_result.content == queued_result.content
            assert checkpoint_result.tool_execution_status == queued_result.tool_execution_status

            subagents = (
                (
                    await db.execute(
                        select(Subagent).where(
                            Subagent.uid == parent.uid,
                            Subagent.session_id == parent.session_id,
                            Subagent.profile_id == parent.profile_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            children = (
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
                .all()
            )
            child_rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child_work_id))).scalars().all()
            assert (len(subagents), len(children), len(child_rounds), len(calls)) == initial_counts

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="child final result"))
        child_done = await runtime.run_next()
        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="parent final result"))
        parent_done = await runtime.run_next()
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs
        assert any("child final result" in json.dumps(payload, ensure_ascii=False) for payload in runtime.provider_payloads[parent_provider_start:])
        if interruption_point == "after-unknown-result-save":
            assert PROMPT_TOOL_INTERRUPTED in json.dumps(runtime.provider_payloads[parent_provider_start:], ensure_ascii=False)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
            child = await db.get(SessionReplyWorkItem, child_work_id)
            assert child is not None
            assert child.status == SessionReplyWorkStatus.SUCCEEDED
            round_row = await db.get(SubagentWork, child_work_id)
            assert round_row is not None
            assert round_row.result is not None
            assert round_row.result["completed"] is True

            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 1
            call = calls[0]
            assert call.tool_call_id == actual_tool_call_id
            assert call.delivered is True
            assert call.tool_result_message_id == queued_result_id

            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            matching_results = []
            for row in result_rows:
                result = InternalMessage.model_validate_json(row.content or "{}")
                if result.tool_call_id == actual_tool_call_id:
                    matching_results.append((row, result, json.loads(result.content or "{}")))
            assert len(matching_results) == 1
            result_row, result, result_payload = matching_results[0]
            assert result_row.id == queued_result_id
            assert result_row.content == queued_result_content
            assert result_row.model_context_suffix == queued_result_suffix
            assert result.tool_call_id == actual_tool_call_id
            assert result.tool_execution_status == queued_result.tool_execution_status
            assert result_payload == queued_payload
            if interruption_point in {"after-result-save", "after-unknown-result-save"}:
                assert result_row.content == saved_result_content
                assert result_row.model_context_suffix == saved_result_suffix
                assert result.tool_execution_status == saved_result_execution_status

            markers = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.type == MessageType.SUBAGENT_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(markers) == 1
            assert markers[0].dedupe_key == build_subagent_notification_key(parent, SUBAGENT_RESULT_NOTIFICATION_KIND, f"{call.subagent_id}:{call.tool_call_id}")

            subagents = (
                (
                    await db.execute(
                        select(Subagent).where(
                            Subagent.uid == parent.uid,
                            Subagent.session_id == parent.session_id,
                            Subagent.profile_id == parent.profile_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            children = (
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
                .all()
            )
            child_rounds = (await db.execute(select(SubagentWork).where(SubagentWork.child_work_id == child_work_id))).scalars().all()
            assert (len(subagents), len(children), len(child_rounds), len(calls)) == initial_counts


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
@pytest.mark.asyncio
async def test_agent_work_child_finishes_before_parent_reply_is_delivered_once(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    import app.core.dispatchers.interactive_tools as interactive_tools_module

    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            enabled_tools = list(configs["tool"]["enabled_tools"])
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*enabled_tools, AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        child_content = "child completed before parent reply"
        child_instruction = "Complete the independent child task and report the final text."
        create_call = InternalToolCall(
            id="race-create-call",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "create", "name": "先完成子任务", "message": child_instruction},
        )
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[create_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=child_content),
            ]
        )

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "agent-work-early-child-request",
                "message": "Create the child task and then complete the parent task.",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        assert accepted_data["submission_status"] == "accepted"
        parent_work_id = accepted_data["work_id"]

        original_save_tool_response = interactive_tools_module.save_tool_response
        child_completed_during_save = False
        child_work_id: int | None = None
        subagent_id: str | None = None
        actual_tool_call_id: str | None = None
        queued_result_id: int | None = None
        state_snapshot = None
        message_snapshot = None
        marker_snapshot = None

        async def save_create_result_and_finish_child(*args, **kwargs):
            nonlocal child_completed_during_save, child_work_id, subagent_id, actual_tool_call_id, queued_result_id
            saved_result = await original_save_tool_response(*args, **kwargs)
            try:
                payload = json.loads(saved_result.content or "{}")
            except (AttributeError, TypeError, ValueError):
                payload = None
            if (
                not child_completed_during_save
                and isinstance(payload, dict)
                and payload.get("status") == "queued"
                and set(payload) == {"status", "work_id", "subagent_id"}
                and isinstance(payload.get("subagent_id"), str)
                and payload.get("subagent_id")
                and isinstance(payload.get("work_id"), int)
                and not isinstance(payload.get("work_id"), bool)
                and payload.get("work_id") > 0
            ):
                child_completed_during_save = True
                subagent_id = payload["subagent_id"]
                child_work_id = payload["work_id"]
                actual_tool_call_id = saved_result.tool_call_id
                queued_result_id = saved_result.id
                child_done = await runtime.run_next(independent_only=True)
                assert child_done is not None
                assert child_done.id == child_work_id
                assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
                assert not runtime.outputs

                async with runtime.factory() as child_db:
                    parent = await child_db.get(SessionReplyWorkItem, parent_work_id)
                    assert parent is not None
                    assert parent.status == SessionReplyWorkStatus.RUNNING
                    assert (parent.uid, parent.session_id, parent.profile_id) == (runtime.uid, runtime.session_id, runtime.profile.id)

                    child = await child_db.get(SessionReplyWorkItem, child_work_id)
                    assert child is not None
                    assert child.status == SessionReplyWorkStatus.SUCCEEDED
                    assert (child.uid, child.session_id, child.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
                    subagent = await child_db.get(Subagent, subagent_id)
                    assert subagent is not None
                    assert (subagent.uid, subagent.session_id, subagent.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
                    round_row = await child_db.get(SubagentWork, child_work_id)
                    assert round_row is not None
                    assert round_row.result is not None
                    assert round_row.result["completed"] is True
                    assert round_row.result["status"] == "succeeded"
                    assert round_row.result["result"]["content"] == child_content

                    calls = (await child_db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
                    assert len(calls) == 1
                    call = calls[0]
                    assert call.arguments == create_call.arguments
                    assert call.subagent_id == subagent_id
                    assert call.child_work_id == child_work_id
                    assert call.tool_call_id == actual_tool_call_id
                    assert call.delivered is False
                    queued_row = await child_db.get(Message, queued_result_id)
                    assert queued_row is not None
                    assert (queued_row.uid, queued_row.session_id, queued_row.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
                    assert queued_row.role == MessageRole.TOOL
                    assert queued_row.type == MessageType.TOOL_RESULT
                    queued_message = InternalMessage.model_validate_json(queued_row.content or "{}")
                    assert queued_message.tool_call_id == actual_tool_call_id
                    assert json.loads(queued_message.content or "{}") == {
                        "status": "queued",
                        "work_id": child_work_id,
                        "subagent_id": subagent_id,
                    }
            return saved_result

        with monkeypatch.context() as patch:
            patch.setattr(interactive_tools_module, "save_tool_response", save_create_result_and_finish_child)
            parent_ready = await runtime.run_next()

        assert child_completed_during_save is True
        assert child_work_id is not None
        assert subagent_id is not None
        assert actual_tool_call_id is not None
        assert queued_result_id is not None
        assert parent_ready is not None
        assert parent_ready.id == parent_work_id
        assert parent_ready.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == 2
        assert subagent_id not in json.dumps(runtime.provider_payloads[1], ensure_ascii=False)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            subagent = await db.get(Subagent, subagent_id)
            round_row = await db.get(SubagentWork, child_work_id)
            assert parent is not None and child is not None and subagent is not None and round_row is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert (parent.uid, parent.session_id, parent.profile_id) == (runtime.uid, runtime.session_id, runtime.profile.id)
            assert child.status == SessionReplyWorkStatus.SUCCEEDED
            assert (child.uid, child.session_id, child.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
            assert (subagent.uid, subagent.session_id, subagent.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
            assert round_row.result is not None
            assert round_row.result["result"]["content"] == child_content
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
            assert len(calls) == 1
            call = calls[0]
            assert call.arguments == create_call.arguments
            assert call.tool_call_id == actual_tool_call_id
            assert call.subagent_id == subagent_id
            assert call.child_work_id == child_work_id
            assert call.tool_result_message_id == queued_result_id
            assert call.delivered is True
            result_rows = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.role == MessageRole.TOOL,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(result_rows) == 1
            result_row = result_rows[0]
            result_message = InternalMessage.model_validate_json(result_row.content or "{}")
            result_payload = json.loads(result_message.content or "{}")
            assert result_row.id == call.tool_result_message_id
            assert result_message.tool_call_id == actual_tool_call_id
            assert result_message.tool_execution_status == "succeeded"
            assert result_payload == {
                "status": "queued",
                "subagent_id": subagent_id,
                "work_id": child_work_id,
            }
            assert not parent.execution_state.get("child_work_waits")
            markers = (
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == parent.session_id,
                            Message.uid == parent.uid,
                            Message.profile_id == parent.profile_id,
                            Message.type == MessageType.SUBAGENT_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(markers) == 1
            assert child_content in (markers[0].content or "")

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="parent final result"))
        parent_done = await runtime.run_next()
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads[parent_provider_start:]) == 1
        parent_resume_payload = runtime.provider_payloads[parent_provider_start]
        assert subagent_id in json.dumps(parent_resume_payload, ensure_ascii=False)
        assert child_content in json.dumps(parent_resume_payload, ensure_ascii=False)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            subagent = await db.get(Subagent, subagent_id)
            round_row = await db.get(SubagentWork, child_work_id)
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
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
            markers = [message for message in messages if message.type == MessageType.SUBAGENT_RESULT]
            assert parent is not None and child is not None and subagent is not None and round_row is not None
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert child.status == SessionReplyWorkStatus.SUCCEEDED
            assert (parent.uid, parent.session_id, parent.profile_id) == (runtime.uid, runtime.session_id, runtime.profile.id)
            assert (child.uid, child.session_id, child.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
            assert (subagent.uid, subagent.session_id, subagent.profile_id) == (parent.uid, parent.session_id, parent.profile_id)
            assert len(calls) == 1
            assert calls[0].delivered is True
            assert calls[0].tool_call_id == actual_tool_call_id
            assert calls[0].tool_result_message_id == queued_result_id
            assert round_row.result is not None
            assert round_row.result["result"]["content"] == child_content
            assert len(markers) == 1
            assert child_content in (markers[0].content or "")
            state_snapshot = {
                "parent": parent.model_dump(mode="json"),
                "child": child.model_dump(mode="json"),
                "subagent": subagent.model_dump(mode="json"),
                "round": round_row.model_dump(mode="json"),
                "calls": [call.model_dump(mode="json") for call in calls],
            }
            message_snapshot = [message.model_dump(mode="json") for message in messages]
            marker_snapshot = [(marker.id, marker.dedupe_key, marker.content) for marker in markers]

        assert state_snapshot is not None
        assert message_snapshot is not None
        assert marker_snapshot is not None
        for _ in range(2):
            async with runtime.factory() as db:
                assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
                assert await reconcile_child_work(db) == 0

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            subagent = await db.get(Subagent, subagent_id)
            round_row = await db.get(SubagentWork, child_work_id)
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id).order_by(SubagentToolCall.id))).scalars().all()
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
            markers = [message for message in messages if message.type == MessageType.SUBAGENT_RESULT]
            assert parent is not None and child is not None and subagent is not None and round_row is not None
            assert state_snapshot == {
                "parent": parent.model_dump(mode="json"),
                "child": child.model_dump(mode="json"),
                "subagent": subagent.model_dump(mode="json"),
                "round": round_row.model_dump(mode="json"),
                "calls": [call.model_dump(mode="json") for call in calls],
            }
            assert message_snapshot == [message.model_dump(mode="json") for message in messages]
            assert marker_snapshot == [(marker.id, marker.dedupe_key, marker.content) for marker in markers]
