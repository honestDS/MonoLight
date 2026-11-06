import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, FILE_TOOL_NAME, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.child_delivery import (
    deliver_child_work_results,
    resume_child_work_for_parent,
)
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentToolCall


def _checkpoint_messages(checkpoint: dict) -> list[InternalMessage]:
    messages: list[InternalMessage] = []
    for key in ("messages", "turn_messages"):
        raw_messages = checkpoint.get(key)
        if isinstance(raw_messages, list):
            messages.extend(InternalMessage.model_validate(message) for message in raw_messages)
    return messages


def _message_payload(message: Message) -> tuple[InternalMessage, dict]:
    stored = InternalMessage.model_validate_json(message.content or "{}")
    payload = json.loads(stored.content or "{}")
    assert isinstance(payload, dict)
    return stored, payload


async def _notification_snapshot(db, session_id: str) -> tuple[tuple, tuple]:
    markers = (await db.execute(select(Message).where(Message.session_id == session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
    events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all()
    return (
        tuple((marker.id, marker.dedupe_key, marker.content) for marker in markers),
        tuple((event.id, event.dedupe_key, deepcopy(event.event)) for event in events),
    )


@asynccontextmanager
async def _query_wait_case(image_chat_runtime, *, protocol="OPENAI_RESPONSES", pending_child_count=1):
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_calls = [
            InternalToolCall(
                id=f"query-wait-create-{index}",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "create", "message": f"query-wait-child-instruction-{index}"},
            )
            for index in range(pending_child_count + 1)
        ]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "query-wait-parent-request",
                "message": "Create child tasks and wait for all results.",
            },
        )
        assert accepted.status_code == 200
        parent_work_id = accepted.json()["data"]["work_id"]

        waiting = await runtime.run_next(independent_only=False)
        assert waiting is not None
        assert waiting.id == parent_work_id
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

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
                        .order_by(SessionReplyWorkItem.created_at, SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(children) == pending_child_count + 1
            assert len(calls) == len(children)
            calls_by_child_id = {call.child_work_id: call for call in calls}
            assert all(child.id in calls_by_child_id for child in children)
            first_child = children[0]
            assert first_child.id is not None
            completed_call = calls_by_child_id[first_child.id].model_copy(deep=True)
            pending_calls = [calls_by_child_id[child.id].model_copy(deep=True) for child in children[1:]]

        first_summary = f"query-wait-child-result-{completed_call.child_work_id}"
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_summary))
        completed = await runtime.run_next(independent_only=True)
        assert completed is not None
        assert completed.id == completed_call.child_work_id
        assert completed.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            child_work_waits = parent.execution_state.get("child_work_waits")
            assert isinstance(child_work_waits, list)
            assert {slot.get("child_work_id") for slot in child_work_waits if isinstance(slot, dict)} == {call.child_work_id for call in pending_calls}

        yield SimpleNamespace(
            runtime=runtime,
            parent_work_id=parent_work_id,
            completed_call=completed_call,
            pending_calls=pending_calls,
            child_work_waits=deepcopy(child_work_waits),
        )


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("query_with_work_id", [False, True], ids=["query-latest", "query-explicit-work"])
@pytest.mark.asyncio
async def test_pending_subagent_query_waits_and_resumes(image_chat_runtime, protocol: str, query_with_work_id: bool) -> None:
    async with _query_wait_case(image_chat_runtime, protocol=protocol, pending_child_count=2) as case:
        runtime = case.runtime
        parent_work_id = case.parent_work_id
        pending_calls = case.pending_calls
        query_calls = []
        for index, call in enumerate(pending_calls):
            arguments = {"operation": "query", "subagent_id": call.subagent_id}
            if query_with_work_id:
                arguments["work_id"] = call.child_work_id
            query_calls.append(
                InternalToolCall(
                    id=f"query-wait-query-{index}-{query_with_work_id}",
                    name=AGENT_WORK_TOOL_NAME,
                    arguments=arguments,
                )
            )

        fallback = "query-wait-parent-fallback-must-not-be-consumed"
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=query_calls),
                InternalMessage(role=MessageRole.ASSISTANT, content=fallback),
            ]
        )
        query_provider_start = len(runtime.provider_payloads)
        query_waiting = await runtime.run_next(independent_only=False)
        assert query_waiting is not None
        assert query_waiting.id == parent_work_id
        assert query_waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == query_provider_start + 1
        assert len(runtime.outputs) == 1
        assert runtime.outputs[0].content == fallback

        pending_result_ids = {call.child_work_id: call.tool_result_message_id for call in pending_calls}
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert parent.execution_state.get("child_work_waits") == case.child_work_waits

            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == len(pending_calls) + 1
            assert {call.arguments.get("operation") for call in calls} == {"create"}
            assert {call.tool_call_id for call in calls} >= {case.completed_call.tool_call_id, *(call.tool_call_id for call in pending_calls)}
            for call in pending_calls:
                stored_result = await db.get(Message, pending_result_ids[call.child_work_id])
                assert stored_result is not None
                stored, payload = _message_payload(stored_result)
                assert stored.tool_call_id == call.tool_call_id
                assert payload["status"] == "queued"
                persisted_call = next(row for row in calls if row.tool_call_id == call.tool_call_id)
                assert persisted_call.delivered is False

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            checkpoint_messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]
            real_query_calls = []
            for query_call in query_calls:
                matches = [tool_call for message in checkpoint_messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments == query_call.arguments]
                assert len(matches) == 1
                real_query_calls.append(matches[0])
            assert len({call.id for call in real_query_calls}) == len(query_calls)
            query_call_ids = {call.id for call in real_query_calls}
            checkpoint_query_results = [message for message in _checkpoint_messages(checkpoint) if message.role == MessageRole.TOOL and message.tool_call_id in query_call_ids]
            assert {message.tool_call_id for message in checkpoint_query_results} == query_call_ids
            for message in checkpoint_query_results:
                payload = json.loads(message.content or "{}")
                assert payload["status"] == "success"
                assert payload["operation"] == "query"
                assert payload["work"]["completed"] is False

        provider_count = len(runtime.provider_payloads)
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count

        runtime.outputs.popleft()
        child_summaries = {}
        for call in pending_calls:
            summary = f"query-wait-child-result-{call.child_work_id}"
            child_summaries[call.child_work_id] = summary
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=summary))
            child_done = await runtime.run_next(independent_only=True)
            assert child_done is not None
            assert child_done.id == call.child_work_id
            assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert not parent.execution_state.get("child_work_waits")
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert all(call.arguments.get("operation") == "create" and call.delivered is True for call in calls)
            for call in pending_calls:
                persisted_call = next(row for row in calls if row.tool_call_id == call.tool_call_id)
                assert persisted_call.tool_result_message_id == pending_result_ids[call.child_work_id]
                stored_result = await db.get(Message, pending_result_ids[call.child_work_id])
                assert stored_result is not None
                _stored, payload = _message_payload(stored_result)
                assert payload["status"] == "success"
                assert payload["work"]["completed"] is True
                assert payload["work"]["result"]["content"] == child_summaries[call.child_work_id]

        final_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="query-wait-parent-final"))
        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == final_provider_start + 1
        final_request = json.dumps(runtime.provider_payloads[final_provider_start], ensure_ascii=False)
        assert f"query-wait-child-result-{case.completed_call.child_work_id}" in final_request
        assert all(summary in final_request for summary in child_summaries.values())

        async with runtime.factory() as db:
            notification_snapshot = await _notification_snapshot(db, runtime.session_id)

        provider_count = len(runtime.provider_payloads)
        async with runtime.factory() as db:
            for call in [case.completed_call, *pending_calls]:
                assert await deliver_child_work_results(db, child_work_id=call.child_work_id) == 0
            assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
            assert await reconcile_child_work(db) == 0

        async with runtime.factory() as db:
            assert await _notification_snapshot(db, runtime.session_id) == notification_snapshot
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
        assert await runtime.run_next(independent_only=False) is None
        assert len(runtime.provider_payloads) == provider_count


@pytest.mark.asyncio
async def test_pending_query_result_recovers_without_another_model_request(image_chat_runtime, monkeypatch) -> None:
    import app.core.dispatchers.interactive_tools as tools_module

    async with _query_wait_case(image_chat_runtime) as case:
        runtime = case.runtime
        parent_work_id = case.parent_work_id
        pending_call = case.pending_calls[0]
        query_call = InternalToolCall(
            id="query-wait-recovery-query",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "query",
                "subagent_id": pending_call.subagent_id,
                "work_id": pending_call.child_work_id,
            },
        )
        fallback = "query-wait-recovery-parent-fallback"
        runtime.outputs.extend(
            [
                InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_call]),
                InternalMessage(role=MessageRole.ASSISTANT, content=fallback),
            ]
        )

        real_save_tool_response = tools_module.save_tool_response

        async def interrupt_after_query_saved(*args, **kwargs):
            stored = await real_save_tool_response(*args, **kwargs)
            claim_info = args[0].info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
            if claim_info is not None and claim_info[0] == parent_work_id:
                payload = json.loads(stored.content or "{}")
                if payload.get("operation") == "query":
                    raise asyncio.CancelledError
            return stored

        with monkeypatch.context() as patch:
            patch.setattr(tools_module, "save_tool_response", interrupt_after_query_saved)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.execution_state.get("child_work_waits") == case.child_work_waits
            parent.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        provider_count = len(runtime.provider_payloads)
        resumed = await runtime.run_next(independent_only=False)
        assert resumed is not None
        assert resumed.id == parent_work_id
        assert resumed.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == provider_count
        assert len(runtime.outputs) == 1
        assert runtime.outputs[0].content == fallback

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.execution_state.get("child_work_waits") == case.child_work_waits

            persisted_call = next(
                call
                for call in (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.child_work_id == pending_call.child_work_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert persisted_call.delivered is False
            assert persisted_call.tool_result_message_id == pending_call.tool_result_message_id

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            checkpoint_messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]
            query_matches = [tool_call for message in checkpoint_messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.name == AGENT_WORK_TOOL_NAME and tool_call.arguments == query_call.arguments]
            assert len(query_matches) == 1
            query_results = [message for message in checkpoint_messages if message.role == MessageRole.TOOL and message.tool_call_id == query_matches[0].id]
            assert len(query_results) == 1
            query_result = json.loads(query_results[0].content or "{}")
            assert query_result["operation"] == "query"
            assert query_result["work"]["completed"] is False


@pytest.mark.parametrize(
    "variant",
    ["mixed-tool", "mixed-completed-query", "failed-query", "unrelated-parent"],
)
@pytest.mark.asyncio
async def test_pending_query_in_mixed_or_unrelated_work_does_not_pause(image_chat_runtime, tmp_path, variant: str) -> None:
    async with _query_wait_case(image_chat_runtime) as case:
        runtime = case.runtime
        original_parent_work_id = case.parent_work_id
        pending_call = case.pending_calls[0]
        parent_work_id = original_parent_work_id
        if variant == "unrelated-parent":
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="query-wait-original-parent-done"))
            original_parent = await runtime.run_next(independent_only=False)
            assert original_parent is not None
            assert original_parent.id == original_parent_work_id
            assert original_parent.status == SessionReplyWorkStatus.SUCCEEDED
            accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "query-wait-unrelated-parent-request",
                    "message": "查询进度并回答",
                },
            )
            assert accepted.status_code == 200
            parent_work_id = accepted.json()["data"]["work_id"]
            assert parent_work_id != original_parent_work_id

        def query(subagent_id: str, work_id: int | None = None) -> InternalToolCall:
            arguments = {"operation": "query", "subagent_id": subagent_id}
            if work_id is not None:
                arguments["work_id"] = work_id
            return InternalToolCall(id=f"query-wait-boundary-{subagent_id}", name=AGENT_WORK_TOOL_NAME, arguments=arguments)

        tool_calls = [query(pending_call.subagent_id, pending_call.child_work_id)]
        file_marker = None
        if variant == "mixed-tool":
            file_marker = "query-wait-boundary-file-marker"
            fixture = tmp_path / "query-wait-boundary.txt"
            fixture.write_text(file_marker, encoding="utf-8")
            tool_calls.append(InternalToolCall(id="query-wait-boundary-file-read", name=FILE_TOOL_NAME, arguments={"operation": "read", "path": str(fixture)}))
        elif variant == "mixed-completed-query":
            tool_calls.append(query(case.completed_call.subagent_id, case.completed_call.child_work_id))
        elif variant == "failed-query":
            tool_calls.append(query("missing-subagent", None))

        final_summary = "query-wait-boundary-final-summary"
        runtime.outputs.extend([InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls), InternalMessage(role=MessageRole.ASSISTANT, content=final_summary)])
        provider_start = len(runtime.provider_payloads)
        completed = await runtime.run_next(independent_only=False)
        assert completed is not None
        assert completed.id == parent_work_id
        assert completed.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == provider_start + 2
        assert not runtime.outputs

        next_model_input = runtime.provider_payloads[provider_start + 1]["input"]
        request_text = json.dumps(next_model_input, ensure_ascii=False)
        tool_results = [json.loads(item["output"]) for item in next_model_input if item.get("type") == "function_call_output"]
        pending_query_results = [result for result in tool_results if result.get("operation") == "query" and result.get("subagent_id") == pending_call.subagent_id]
        assert len(pending_query_results) == 1
        pending_query_result = pending_query_results[0]
        assert pending_query_result["status"] == "success"
        assert pending_query_result["work"]["completed"] is False
        assert pending_query_result["work"]["work_id"] == pending_call.child_work_id
        if variant == "mixed-tool":
            assert file_marker in request_text
        elif variant == "mixed-completed-query":
            assert f"query-wait-child-result-{case.completed_call.child_work_id}" in request_text
        elif variant == "failed-query":
            assert any(result.get("operation") == "query" and result.get("status") == "failed" for result in tool_results)

        async with runtime.factory() as db:
            original_parent = await db.get(SessionReplyWorkItem, original_parent_work_id)
            assert original_parent is not None
            assert original_parent.execution_state.get("child_work_waits") == case.child_work_waits
            persisted_call = (
                await db.execute(
                    select(SubagentToolCall).where(
                        SubagentToolCall.parent_work_id == original_parent_work_id,
                        SubagentToolCall.child_work_id == pending_call.child_work_id,
                    )
                )
            ).scalar_one()
            assert persisted_call.delivered is False
            assert persisted_call.tool_result_message_id == pending_call.tool_result_message_id
            if variant == "unrelated-parent":
                unrelated_parent = await db.get(SessionReplyWorkItem, parent_work_id)
                assert unrelated_parent is not None
                assert not unrelated_parent.execution_state.get("child_work_waits")


@pytest.mark.parametrize("event", ["child-completed", "new-user-message"])
@pytest.mark.asyncio
async def test_pending_query_does_not_lose_work_when_event_arrives(image_chat_runtime, monkeypatch, event: str) -> None:
    import app.core.session_reply_queue.child_work as child_work_module

    async with _query_wait_case(image_chat_runtime) as case:
        runtime = case.runtime
        parent_work_id = case.parent_work_id
        pending_call = case.pending_calls[0]
        query = InternalToolCall(
            id="query-wait-late-event-query",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "query", "subagent_id": pending_call.subagent_id, "work_id": pending_call.child_work_id},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query]))
        query_summary = "query-wait-late-event-parent-summary"
        child_summary = f"query-wait-late-child-result-{pending_call.child_work_id}"
        real_query = child_work_module.query_subagent
        new_user_work_id = None

        async def query_then_event(*args, **kwargs):
            nonlocal new_user_work_id
            result = await real_query(*args, **kwargs)
            assert result["work"]["completed"] is False
            if event == "child-completed":
                runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_summary))
                completed_child = await runtime.run_next(independent_only=True)
                assert completed_child is not None and completed_child.id == pending_call.child_work_id
                assert completed_child.status == SessionReplyWorkStatus.SUCCEEDED
            else:
                accepted = await runtime.client.post(
                    "/api/v1/chat/completions",
                    json={
                        "session_id": runtime.session_id,
                        "request_id": "query-wait-user-new-instruction-request",
                        "message": "query-wait-user-new-instruction",
                    },
                )
                assert accepted.status_code == 200
                new_user_work_id = accepted.json()["data"]["work_id"]
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=query_summary))
            return result

        provider_start = len(runtime.provider_payloads)
        with monkeypatch.context() as patch:
            patch.setattr(child_work_module, "query_subagent", query_then_event)
            parent = await runtime.run_next(independent_only=False)
        assert parent is not None and parent.id == parent_work_id

        if event == "child-completed":
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert len(runtime.provider_payloads) == provider_start + 2
            assert len(runtime.outputs) == 1 and runtime.outputs[0].content == query_summary
            async with runtime.factory() as db:
                parent_row = await db.get(SessionReplyWorkItem, parent_work_id)
                persisted_call = (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.child_work_id == pending_call.child_work_id,
                        )
                    )
                ).scalar_one()
                assert parent_row is not None and not parent_row.execution_state.get("child_work_waits")
                assert persisted_call.delivered is True
                stored_result = await db.get(Message, persisted_call.tool_result_message_id)
                assert stored_result is not None
                _stored, payload = _message_payload(stored_result)
                assert payload["work"]["result"]["content"] == child_summary

            final_provider_start = len(runtime.provider_payloads)
            completed_parent = await runtime.run_next(independent_only=False)
            assert completed_parent is not None and completed_parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(runtime.provider_payloads) == final_provider_start + 1
            assert child_summary in json.dumps(runtime.provider_payloads[final_provider_start]["input"], ensure_ascii=False)
            async with runtime.factory() as db:
                assert await deliver_child_work_results(db, child_work_id=pending_call.child_work_id) == 0
            assert await runtime.run_next(independent_only=False) is None
        else:
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert new_user_work_id is not None
            assert len(runtime.provider_payloads) == provider_start + 2
            next_input = json.dumps(runtime.provider_payloads[provider_start + 1]["input"], ensure_ascii=False)
            assert "query-wait-user-new-instruction" in next_input
            async with runtime.factory() as db:
                parent_row = await db.get(SessionReplyWorkItem, parent_work_id)
                new_user_work = await db.get(SessionReplyWorkItem, new_user_work_id)
                pending_child = await db.get(SessionReplyWorkItem, pending_call.child_work_id)
                persisted_call = (
                    await db.execute(
                        select(SubagentToolCall).where(
                            SubagentToolCall.parent_work_id == parent_work_id,
                            SubagentToolCall.child_work_id == pending_call.child_work_id,
                        )
                    )
                ).scalar_one()
                assert parent_row is not None and new_user_work is not None and pending_child is not None
                assert new_user_work_id != parent_work_id
                assert new_user_work.status == SessionReplyWorkStatus.MERGED
                assert new_user_work.merged_into_id == parent_work_id
                assert int(new_user_work.source_id) in parent_row.input_message_ids
                assert pending_child.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert pending_call.child_work_id in {slot.get("child_work_id") for slot in parent_row.execution_state.get("child_work_waits") or []}
                assert persisted_call.delivered is False


@pytest.mark.parametrize("staged", [False, True], ids=["before-suspend", "staged-suspend"])
@pytest.mark.asyncio
async def test_pending_query_suspend_interrupt_recovers_from_checkpoint(image_chat_runtime, monkeypatch, staged: bool) -> None:
    from app.core.crud.session.external_work import external_work_crud
    from app.models.session_reply_stream_event import SessionReplyStreamEvent

    async with _query_wait_case(image_chat_runtime) as case:
        runtime = case.runtime
        parent_work_id = case.parent_work_id
        pending_call = case.pending_calls[0]
        query_call = InternalToolCall(
            id="query-wait-suspend-interrupt-query",
            name=AGENT_WORK_TOOL_NAME,
            arguments={"operation": "query", "subagent_id": pending_call.subagent_id, "work_id": pending_call.child_work_id},
        )
        fallback = "query-wait-suspend-interrupt-fallback"
        runtime.outputs.extend([InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[query_call]), InternalMessage(role=MessageRole.ASSISTANT, content=fallback)])

        async with runtime.factory() as db:
            baseline = sum(event.event.get("type") == "work_suspended" for event in (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == parent_work_id))).scalars().all())

        real_suspend_claimed = external_work_crud.suspend_claimed

        async def interrupt_suspend(db, *args, **kwargs):
            work = kwargs.get("work")
            if work is not None and work.id == parent_work_id:
                if staged:
                    staged_kwargs = dict(kwargs)
                    staged_kwargs["commit"] = False
                    await real_suspend_claimed(db, *args, **staged_kwargs)
                raise asyncio.CancelledError
            return await real_suspend_claimed(db, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(external_work_crud, "suspend_claimed", interrupt_suspend)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run_next(independent_only=False)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None and parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.execution_state.get("child_work_waits") == case.child_work_waits
            events = (await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == parent_work_id))).scalars().all()
            assert sum(event.event.get("type") == "work_suspended" for event in events) == baseline

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            checkpoint_messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]
            query_matches = [tool_call for message in checkpoint_messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.arguments == query_call.arguments]
            assert len(query_matches) == 1
            query_results = [message for message in checkpoint_messages if message.role == MessageRole.TOOL and message.tool_call_id == query_matches[0].id]
            assert len(query_results) == 1
            assert json.loads(query_results[0].content or "{}")["work"]["completed"] is False

            persisted_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id, SubagentToolCall.child_work_id == pending_call.child_work_id))).scalar_one()
            assert persisted_call.delivered is False
            assert persisted_call.tool_result_message_id == pending_call.tool_result_message_id
            parent.lock_until = 0
            await db.commit()

        await SessionReplyConsumer()._recover_expired()
        provider_count = len(runtime.provider_payloads)
        resumed = await runtime.run_next(independent_only=False)
        assert resumed is not None and resumed.id == parent_work_id
        assert resumed.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(runtime.provider_payloads) == provider_count
        assert len(runtime.outputs) == 1 and runtime.outputs[0].content == fallback

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            persisted_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id, SubagentToolCall.child_work_id == pending_call.child_work_id))).scalar_one()
            assert parent is not None and parent.execution_state.get("child_work_waits") == case.child_work_waits
            assert persisted_call.delivered is False
            assert persisted_call.tool_result_message_id == pending_call.tool_result_message_id
