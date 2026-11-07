import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from sqlalchemy import select

import app.core.session_reply_queue.child_work as child_work_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
from app.core.constants import AGENT_WORK_TOOL_NAME, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.reply_work_item import session_reply_work_item_crud as reply_work_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.session_reply_queue.child_delivery import (
    deliver_child_work_results,
    resume_child_work_for_parent,
)
from app.core.session_reply_queue.child_recovery import reconcile_child_work
from app.core.session_reply_queue.executor_resume import restore_interactive_tool_replies
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.subagent import SubagentToolCall


def _message_payload(message: Message) -> dict:
    stored = InternalMessage.model_validate_json(message.content or "{}")
    payload = json.loads(stored.content or "{}")
    assert isinstance(payload, dict)
    return payload


def _checkpoint_messages(checkpoint: dict, key: str) -> list[InternalMessage]:
    raw_messages = checkpoint.get(key)
    assert isinstance(raw_messages, list)
    return [InternalMessage.model_validate(message) for message in raw_messages]


def _checkpoint_query_result(checkpoint: dict, query_call_id: str) -> InternalMessage:
    results = [message for key in ("messages", "turn_messages") for message in _checkpoint_messages(checkpoint, key) if message.role == MessageRole.TOOL and message.tool_call_id == query_call_id]
    assert len(results) == 2
    assert results[0].model_dump(mode="json") == results[1].model_dump(mode="json")
    return results[0]


async def _notification_snapshot(db, session_id: str) -> tuple[tuple, tuple]:
    markers = (await db.execute(select(Message).where(Message.session_id == session_id, Message.type == MessageType.SUBAGENT_RESULT).order_by(Message.id))).scalars().all()
    events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.id))).scalars().all()
    return (
        tuple((marker.id, marker.dedupe_key, marker.content) for marker in markers),
        tuple((event.id, event.dedupe_key, deepcopy(event.event)) for event in events),
    )


@asynccontextmanager
async def _query_transaction_case(image_chat_runtime):
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        create_messages = (
            "query-transaction-child-one",
            "query-transaction-child-two",
        )
        summary_a = "query-transaction-child-A-summary"
        summary_b = "query-transaction-child-B-summary"
        create_calls = [
            InternalToolCall(
                id="query-transaction-create-a",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "create", "message": create_messages[0], "name": "构建检查"},
            ),
            InternalToolCall(
                id="query-transaction-create-b",
                name=AGENT_WORK_TOOL_NAME,
                arguments={"operation": "create", "message": create_messages[1], "name": "依赖分析"},
            ),
        ]
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=create_calls))
        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "query-transaction-parent-request",
                "message": "Create two child tasks and wait for both results.",
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
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                        .order_by(SessionReplyWorkItem.created_at, SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(children) == 2
            calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == parent_work_id))).scalars().all()
            assert len(calls) == 2
            calls_by_child_id = {call.child_work_id: call for call in calls}
            assert all(child.id in calls_by_child_id for child in children)
            first_child, second_child = children
            assert first_child.id is not None and second_child.id is not None
            call_a = calls_by_child_id[first_child.id]
            call_b = calls_by_child_id[second_child.id]
            assert call_a.id is not None and call_b.id is not None
            assert call_a.tool_result_message_id is not None and call_b.tool_result_message_id is not None
            child_a_work_id = first_child.id
            child_b_work_id = second_child.id
            b_call_id = call_b.id
            b_tool_call_id = call_b.tool_call_id
            b_subagent_id = call_b.subagent_id
            b_result_message_id = call_b.tool_result_message_id

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=summary_a))
        child_a_done = await runtime.run_next(independent_only=True)
        assert child_a_done is not None
        assert child_a_done.id == child_a_work_id
        assert child_a_done.status == SessionReplyWorkStatus.SUCCEEDED

        worker_id = "query-transaction-worker"
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            claimed = await reply_work_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
                independent_only=False,
            )
            assert claimed is not None
            assert claimed.id == parent_work_id
            assert claimed.status == SessionReplyWorkStatus.RUNNING

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=summary_b))
        child_b_done = await runtime.run_next(independent_only=True)
        assert child_b_done is not None
        assert child_b_done.id == child_b_work_id
        assert child_b_done.status == SessionReplyWorkStatus.SUCCEEDED

        query_call = InternalToolCall(
            id="query-transaction-query-b",
            name=AGENT_WORK_TOOL_NAME,
            arguments={
                "operation": "query",
                "subagent_id": b_subagent_id,
                "work_id": child_b_work_id,
            },
        )
        query_result_payload: dict
        pending_checkpoint: dict
        query_result_message_id: int
        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.locked_by == worker_id
            checkpoint = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(checkpoint, dict)
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert any(isinstance(slot, dict) and slot.get("child_work_id") == child_b_work_id and slot.get("tool_call_id") == b_tool_call_id for slot in waits)

            query_assistant = InternalMessage(
                role=MessageRole.ASSISTANT,
                type=MessageType.TOOL_CALL,
                tool_calls=[query_call],
            )
            saved_query_assistant = await save_message(
                db,
                runtime.session_id,
                runtime.uid,
                MessageRole.ASSISTANT,
                MessageType.TOOL_CALL,
                query_assistant,
                runtime.profile.id,
                commit=False,
                work_id=parent_work_id,
            )
            assert saved_query_assistant.id is not None
            query_assistant.id = saved_query_assistant.id
            query_assistant.created_at = saved_query_assistant.created_at

            intent_messages = _checkpoint_messages(checkpoint, "messages")
            intent_turn_messages = _checkpoint_messages(checkpoint, "turn_messages")
            intent_messages.append(query_assistant)
            intent_turn_messages.append(query_assistant.model_copy(deep=True))
            intent_checkpoint = deepcopy(checkpoint)
            intent_checkpoint["messages"] = [message.model_dump(mode="json", exclude_none=True) for message in intent_messages]
            intent_checkpoint["turn_messages"] = [message.model_dump(mode="json", exclude_none=True) for message in intent_turn_messages]
            await executor_interactive_module._save_interactive_work_execution_checkpoint(
                db,
                intent_checkpoint,
                work=parent,
                worker_id=worker_id,
            )

            query_result_payload = await child_work_module.query_subagent(
                db,
                uid=runtime.uid,
                session_id=runtime.session_id,
                profile_id=runtime.profile.id,
                subagent_id=b_subagent_id,
                work_id=child_b_work_id,
            )
            assert query_result_payload["status"] == "success"
            assert query_result_payload["operation"] == "query"
            query_result = InternalMessage(
                role=MessageRole.TOOL,
                type=MessageType.TOOL_RESULT,
                tool_call_id=query_call.id,
                tool_execution_status="succeeded",
                content=json.dumps(query_result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            stored_query_result = await save_tool_response(
                db,
                runtime.session_id,
                runtime.uid,
                runtime.profile.id,
                query_result,
                intent_messages,
                intent_turn_messages,
            )
            assert stored_query_result.id is not None
            query_result_message_id = stored_query_result.id
            pending_checkpoint = deepcopy(intent_checkpoint)
            pending_checkpoint["messages"] = [message.model_dump(mode="json", exclude_none=True) for message in intent_messages]
            pending_checkpoint["turn_messages"] = [message.model_dump(mode="json", exclude_none=True) for message in intent_turn_messages]

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, b_call_id)
            b_result = await db.get(Message, b_result_message_id)
            assert parent is not None and b_call is not None and b_result is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert b_call.delivered is False
            assert _message_payload(b_result)["status"] == "queued"
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert any(slot.get("child_work_id") == child_b_work_id for slot in waits if isinstance(slot, dict))
            notification_snapshot = await _notification_snapshot(db, runtime.session_id)

        assert _checkpoint_query_result(pending_checkpoint, query_call.id).id == query_result_message_id
        yield {
            "runtime": runtime,
            "parent_work_id": parent_work_id,
            "child_b_work_id": child_b_work_id,
            "b_call_id": b_call_id,
            "b_tool_call_id": b_tool_call_id,
            "b_result_message_id": b_result_message_id,
            "query_call_id": query_call.id,
            "query_result_message_id": query_result_message_id,
            "query_result_payload": query_result_payload,
            "summary_b": summary_b,
            "worker_id": worker_id,
            "checkpoint": pending_checkpoint,
            "notification_snapshot": notification_snapshot,
        }


@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, asyncio.CancelledError],
    ids=["runtime-error", "cancelled"],
)
@pytest.mark.asyncio
async def test_query_delivery_checkpoint_save_failure_rolls_back(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    async with _query_transaction_case(image_chat_runtime) as case:
        runtime = case["runtime"]
        parent_work_id = case["parent_work_id"]
        worker_id = case["worker_id"]
        real_save_checkpoint = reply_work_crud.save_checkpoint

        async def save_then_fail(db, work_id, checkpoint):
            await real_save_checkpoint(db, work_id, checkpoint)
            if work_id == parent_work_id:
                raise failure_type("injected checkpoint failure")

        monkeypatch.setattr(reply_work_crud, "save_checkpoint", save_then_fail)
        with pytest.raises(failure_type):
            async with runtime.factory() as db:
                db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
                work = await db.get(SessionReplyWorkItem, parent_work_id)
                assert work is not None
                await executor_interactive_module._save_interactive_work_execution_checkpoint(
                    db,
                    deepcopy(case["checkpoint"]),
                    work=work,
                    worker_id=worker_id,
                )

        monkeypatch.setattr(reply_work_crud, "save_checkpoint", real_save_checkpoint)
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            query_result = await db.get(Message, case["query_result_message_id"])
            assert parent is not None and b_call is not None and b_result is not None and query_result is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.locked_by == worker_id
            assert b_call.delivered is False
            assert _message_payload(b_result)["status"] == "queued"
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert any(slot.get("child_work_id") == case["child_b_work_id"] for slot in waits if isinstance(slot, dict))
            stored_checkpoint = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(stored_checkpoint, dict)
            for key in ("messages", "turn_messages"):
                messages = _checkpoint_messages(stored_checkpoint, key)
                assert any(message.role == MessageRole.ASSISTANT and any(tool_call.id == case["query_call_id"] for tool_call in message.tool_calls or []) for message in messages)
                assert not any(message.role == MessageRole.TOOL and message.tool_call_id == case["query_call_id"] for message in messages)
            assert query_result.work_id is None
            assert _message_payload(query_result) == case["query_result_payload"]
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]
            assert all(case["summary_b"] not in (content or "") for _, _, content in case["notification_snapshot"][0])

        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
            work = await db.get(SessionReplyWorkItem, parent_work_id)
            stored_checkpoint = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert work is not None and isinstance(stored_checkpoint, dict)
            restored = await restore_interactive_tool_replies(
                db,
                work=work,
                worker_id=worker_id,
                checkpoint=stored_checkpoint,
            )
            restored_result = _checkpoint_query_result(restored, case["query_call_id"])
            assert restored_result.id == case["query_result_message_id"]
            assert json.loads(restored_result.content or "{}") == case["query_result_payload"]
            parent = await reply_work_crud.get(db, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            assert parent is not None and b_call is not None and b_result is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert b_call.delivered is True
            assert "child_work_waits" not in parent.execution_state
            assert _message_payload(b_result)["status"] == "success"
            persisted = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert isinstance(persisted, dict)
            assert _checkpoint_query_result(persisted, case["query_call_id"]).id == case["query_result_message_id"]
            assert await deliver_child_work_results(db, child_work_id=case["child_b_work_id"]) == 0
            assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
            assert await reconcile_child_work(db) == 0
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]


@pytest.mark.asyncio
async def test_query_delivery_rejects_stale_executor_and_recovers_with_new_worker(image_chat_runtime) -> None:
    async with _query_transaction_case(image_chat_runtime) as case:
        runtime = case["runtime"]
        parent_work_id = case["parent_work_id"]
        old_worker_id = case["worker_id"]
        new_worker_id = "query-transaction-replacement-worker"
        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            parent.locked_by = new_worker_id
            db.add(parent)
            await db.commit()
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, old_worker_id)
            with pytest.raises(asyncio.CancelledError):
                await executor_interactive_module._save_interactive_work_execution_checkpoint(
                    db,
                    deepcopy(case["checkpoint"]),
                    work=parent,
                    worker_id=old_worker_id,
                )

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            assert parent is not None and b_call is not None and b_result is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.locked_by == new_worker_id
            assert b_call.delivered is False
            assert _message_payload(b_result)["status"] == "queued"
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert any(slot.get("child_work_id") == case["child_b_work_id"] for slot in waits if isinstance(slot, dict))
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]
            assert all(case["summary_b"] not in (content or "") for _, _, content in case["notification_snapshot"][0])

        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, new_worker_id)
            work = await db.get(SessionReplyWorkItem, parent_work_id)
            stored_checkpoint = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert work is not None and isinstance(stored_checkpoint, dict)
            restored = await restore_interactive_tool_replies(
                db,
                work=work,
                worker_id=new_worker_id,
                checkpoint=stored_checkpoint,
            )
            restored_result = _checkpoint_query_result(restored, case["query_call_id"])
            assert restored_result.id == case["query_result_message_id"]
            assert json.loads(restored_result.content or "{}") == case["query_result_payload"]
            parent = await reply_work_crud.get(db, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            assert parent is not None and b_call is not None
            assert parent.locked_by == new_worker_id
            assert b_call.delivered is True
            assert "child_work_waits" not in parent.execution_state
            assert await deliver_child_work_results(db, child_work_id=case["child_b_work_id"]) == 0
            assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
            assert await reconcile_child_work(db) == 0
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]


@pytest.mark.asyncio
async def test_query_delivery_checkpoint_survives_database_reopen(image_chat_runtime) -> None:
    async with _query_transaction_case(image_chat_runtime) as case:
        runtime = case["runtime"]
        parent_work_id = case["parent_work_id"]
        worker_id = case["worker_id"]
        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
            work = await db.get(SessionReplyWorkItem, parent_work_id)
            assert work is not None
            await executor_interactive_module._save_interactive_work_execution_checkpoint(
                db,
                deepcopy(case["checkpoint"]),
                work=work,
                worker_id=worker_id,
            )

        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
            work = await db.get(SessionReplyWorkItem, parent_work_id)
            persisted = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert work is not None and isinstance(persisted, dict)
            assert work.status == SessionReplyWorkStatus.RUNNING
            assert work.locked_by == worker_id
            assert "child_work_waits" not in work.execution_state
            assert _checkpoint_query_result(persisted, case["query_call_id"]).id == case["query_result_message_id"]
            await executor_interactive_module._save_interactive_work_execution_checkpoint(
                db,
                deepcopy(persisted),
                work=work,
                worker_id=worker_id,
            )
            assert await deliver_child_work_results(db, child_work_id=case["child_b_work_id"]) == 0
            assert await resume_child_work_for_parent(db, parent_work_id=parent_work_id) is False
            assert await reconcile_child_work(db) == 0

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            query_result = await db.get(Message, case["query_result_message_id"])
            persisted = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert parent is not None and b_call is not None and b_result is not None and query_result is not None
            assert isinstance(persisted, dict)
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert parent.locked_by == worker_id
            assert b_call.delivered is True
            assert "child_work_waits" not in parent.execution_state
            assert _message_payload(b_result)["status"] == "success"
            assert _message_payload(query_result) == case["query_result_payload"]
            assert _checkpoint_query_result(persisted, case["query_call_id"]).id == case["query_result_message_id"]
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]


@pytest.mark.parametrize(
    "query_variant",
    ["truncated", "running", "failed", "not-persisted", "wrong-child", "user-json"],
)
@pytest.mark.asyncio
async def test_query_delivery_requires_a_complete_current_persisted_result(image_chat_runtime, query_variant: str) -> None:
    async with _query_transaction_case(image_chat_runtime) as case:
        runtime = case["runtime"]
        parent_work_id = case["parent_work_id"]
        worker_id = case["worker_id"]
        checkpoint = deepcopy(case["checkpoint"])
        sync_query_message = query_variant in {"truncated", "running", "failed", "wrong-child"}
        modified_query_message = None

        async with runtime.factory() as db:
            query_messages_by_key = {}
            for key in ("messages", "turn_messages"):
                messages = _checkpoint_messages(checkpoint, key)
                matches = [message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id == case["query_call_id"]]
                assert len(matches) == 1
                query_messages_by_key[key] = (messages, matches[0])

            source_query_message = query_messages_by_key["messages"][1]
            if query_variant == "truncated":
                query_payload = json.loads(source_query_message.content or "{}")
                truncated_work = deepcopy(query_payload["work"])
                truncated_result = deepcopy(truncated_work["result"])
                assert isinstance(truncated_result.get("content"), str)
                truncated_result["content"] = truncated_result["content"][:-1]
                truncated_work["result"] = truncated_result
                query_payload["work"] = truncated_work
                modified_query_message = source_query_message.model_copy(update={"content": json.dumps(query_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})
            elif query_variant == "running":
                query_payload = json.loads(source_query_message.content or "{}")
                running_work = deepcopy(query_payload["work"])
                running_work["completed"] = False
                query_payload["work"] = running_work
                modified_query_message = source_query_message.model_copy(update={"content": json.dumps(query_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})
            elif query_variant == "failed":
                query_payload = json.loads(source_query_message.content or "{}")
                query_payload["status"] = "failed"
                modified_query_message = source_query_message.model_copy(update={"content": json.dumps(query_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})
            elif query_variant == "not-persisted":
                modified_query_message = source_query_message.model_copy(update={"id": None})
            elif query_variant == "wrong-child":
                query_payload = json.loads(source_query_message.content or "{}")
                wrong_work = deepcopy(query_payload["work"])
                wrong_work["work_id"] = case["child_b_work_id"] + 10_000_000
                query_payload["work"] = wrong_work
                modified_query_message = source_query_message.model_copy(update={"content": json.dumps(query_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))})
            else:
                modified_query_message = source_query_message.model_copy(
                    update={
                        "role": MessageRole.USER,
                        "type": MessageType.TEXT,
                        "content": "query result was returned as user text",
                        "tool_call_id": None,
                        "tool_execution_status": None,
                    }
                )

            assert modified_query_message is not None
            if sync_query_message:
                query_row = await db.get(Message, case["query_result_message_id"])
                assert query_row is not None
                query_row.content = modified_query_message.model_dump_json(exclude_none=True)
                db.add(query_row)
                await db.commit()

            for key, (messages, _source) in query_messages_by_key.items():
                replacement = modified_query_message.model_copy(deep=True)
                query_index = next(index for index, message in enumerate(messages) if message.role == MessageRole.TOOL and message.tool_call_id == case["query_call_id"])
                messages[query_index] = replacement
                checkpoint[key] = [message.model_dump(mode="json", exclude_none=True) for message in messages]

            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
            work = await db.get(SessionReplyWorkItem, parent_work_id)
            assert work is not None
            await executor_interactive_module._save_interactive_work_execution_checkpoint(
                db,
                checkpoint,
                work=work,
                worker_id=worker_id,
            )

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            assert parent is not None and b_call is not None and b_result is not None
            assert parent.status == SessionReplyWorkStatus.RUNNING
            assert b_call.delivered is False
            assert _message_payload(b_result)["status"] == "queued"
            waits = parent.execution_state.get("child_work_waits")
            assert isinstance(waits, list)
            assert any(slot.get("child_work_id") == case["child_b_work_id"] for slot in waits if isinstance(slot, dict))
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]

        async with runtime.factory() as db:
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
            assert await reply_work_crud.mark_terminal(
                db,
                work_id=parent_work_id,
                worker_id=worker_id,
                status=SessionReplyWorkStatus.SUCCEEDED,
                commit=True,
            )

        async with runtime.factory() as db:
            assert await deliver_child_work_results(db, child_work_id=case["child_b_work_id"]) == 1
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            assert parent is not None and b_call is not None and b_result is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert b_call.delivered is True
            assert "child_work_waits" not in parent.execution_state
            b_payload = _message_payload(b_result)
            assert b_payload["status"] == "success"
            assert b_payload["work"]["result"]["content"] == case["summary_b"]
            notification_snapshot = await _notification_snapshot(db, runtime.session_id)
            baseline_marker_ids = {marker[0] for marker in case["notification_snapshot"][0]}
            new_markers = [marker for marker in notification_snapshot[0] if marker[0] not in baseline_marker_ids]
            assert len(new_markers) == 1
            assert case["summary_b"] in (new_markers[0][2] or "")
            baseline_ready_events = [event for event in case["notification_snapshot"][1] if event[2].get("type") == "work_ready" and event[2].get("work_id") == parent_work_id]
            ready_events = [event for event in notification_snapshot[1] if event[2].get("type") == "work_ready" and event[2].get("work_id") == parent_work_id]
            assert len(ready_events) == len(baseline_ready_events) + 1

            assert await deliver_child_work_results(db, child_work_id=case["child_b_work_id"]) == 0
            assert await reconcile_child_work(db) == 0
            assert await _notification_snapshot(db, runtime.session_id) == notification_snapshot


@pytest.mark.asyncio
async def test_query_delivery_save_and_automatic_delivery_race_without_duplicates(image_chat_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _query_transaction_case(image_chat_runtime) as case:
        runtime = case["runtime"]
        parent_work_id = case["parent_work_id"]
        worker_id = case["worker_id"]
        checkpoint_staged = asyncio.Event()
        delivery_entered = asyncio.Event()
        release_commit = asyncio.Event()
        release_delivery_lock = asyncio.Event()
        save_gate_used = False
        tasks: list[asyncio.Task] = []
        real_save_checkpoint = reply_work_crud.save_checkpoint
        real_lock_parent_for_delivery = subagent_crud.lock_parent_for_delivery

        async def save_checkpoint_gate(db, work_id, checkpoint):
            nonlocal save_gate_used
            await real_save_checkpoint(db, work_id, checkpoint)
            if work_id != parent_work_id or save_gate_used:
                return
            save_gate_used = True
            checkpoint_staged.set()
            await release_commit.wait()

        async def lock_parent_for_delivery_gate(db, **kwargs):
            if kwargs.get("work_id") == parent_work_id:
                delivery_entered.set()
                await release_delivery_lock.wait()
            return await real_lock_parent_for_delivery(db, **kwargs)

        async def resume_in_new_session():
            async with runtime.factory() as db:
                return await resume_child_work_for_parent(db, parent_work_id=parent_work_id)

        monkeypatch.setattr(reply_work_crud, "save_checkpoint", save_checkpoint_gate)
        monkeypatch.setattr(subagent_crud, "lock_parent_for_delivery", lock_parent_for_delivery_gate)
        save_task = None
        delivery_task = None
        try:
            async with runtime.factory() as worker_db:
                worker_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
                work = await worker_db.get(SessionReplyWorkItem, parent_work_id)
                assert work is not None
                save_task = asyncio.create_task(
                    executor_interactive_module._save_interactive_work_execution_checkpoint(
                        worker_db,
                        deepcopy(case["checkpoint"]),
                        work=work,
                        worker_id=worker_id,
                    )
                )
                tasks.append(save_task)
                await asyncio.wait_for(checkpoint_staged.wait(), timeout=10)
                delivery_task = asyncio.create_task(resume_in_new_session())
                tasks.append(delivery_task)
                await asyncio.wait_for(delivery_entered.wait(), timeout=10)
                release_commit.set()
                await asyncio.wait_for(asyncio.shield(save_task), timeout=10)

                async with runtime.factory() as db:
                    db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (parent_work_id, worker_id)
                    assert await reply_work_crud.mark_terminal(
                        db,
                        work_id=parent_work_id,
                        worker_id=worker_id,
                        status=SessionReplyWorkStatus.SUCCEEDED,
                        commit=True,
                    )
                release_delivery_lock.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
                assert results[0] is None
                assert results[1] is False
        finally:
            release_commit.set()
            release_delivery_lock.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            b_call = await db.get(SubagentToolCall, case["b_call_id"])
            b_result = await db.get(Message, case["b_result_message_id"])
            query_result = await db.get(Message, case["query_result_message_id"])
            persisted = await reply_work_crud.get_checkpoint(db, parent_work_id)
            assert parent is not None and b_call is not None and b_result is not None and query_result is not None
            assert isinstance(persisted, dict)
            assert parent.status == SessionReplyWorkStatus.SUCCEEDED
            assert b_call.delivered is True
            assert "child_work_waits" not in parent.execution_state
            assert _message_payload(b_result)["work"]["result"]["content"] == case["summary_b"]
            assert _message_payload(query_result) == case["query_result_payload"]
            assert _checkpoint_query_result(persisted, case["query_call_id"]).id == case["query_result_message_id"]
            checkpoint_b_results = [message for key in ("messages", "turn_messages") for message in _checkpoint_messages(persisted, key) if message.role == MessageRole.TOOL and message.tool_call_id == case["b_tool_call_id"]]
            assert len(checkpoint_b_results) == 2
            assert json.loads(checkpoint_b_results[0].content or "{}")["work"]["result"]["content"] == case["summary_b"]
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]
            assert await reconcile_child_work(db) == 0
            assert await _notification_snapshot(db, runtime.session_id) == case["notification_snapshot"]
