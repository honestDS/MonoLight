import asyncio
import json

import pytest

from app.core.background_tasks import external_work as external_work_module
from app.core.background_tasks.result_delivery import receive_pending_external_results
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue.manager_result import SessionReplyResult
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.background_tasks import test_external_work_lifecycle as lifecycle_module
from tests.integration.background_tasks.test_external_work_lifecycle import (
    _assert_background_result_marker,
    _assert_queued_tool_result,
    _build_received_results_checkpoint,
    _claim_next,
    _complete_task,
    _create_case,
    _create_target_work,
    _make_work_ready_for_claim,
    _messages_of_type,
    _prepare_claimed_external_resume,
    _result_payloads,
    _save_received_results_checkpoint,
    _snapshot,
)

external_work_database = lifecycle_module.external_work_database


@pytest.mark.asyncio
async def test_parallel_external_resume_is_idempotent_and_persists_protocol_events(external_work_database):
    case = await _create_case(external_work_database)
    await _complete_task(case, "call-1", "completed-once")

    resume_results = await asyncio.gather(
        external_work_module.resume_external_work_for_task(case.task_ids["call-1"]),
        external_work_module.resume_external_work_for_task(case.task_ids["call-1"]),
    )
    assert sorted(resume_results) == [False, True]

    snapshot = await _snapshot(case)
    work = snapshot["work"]
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert work.sequence_no > case.initial_sequence_no
    assert work.execution_state["external_resume_pending"] is True
    assert "pending_tools" not in work.execution_state

    _assert_queued_tool_result(snapshot, case, "call-1")
    result_row = next(message for message in snapshot["messages"] if message.id == case.result_message_ids["call-1"])
    assert result_row.id == case.result_message_ids["call-1"]

    markers = _messages_of_type(snapshot, MessageType.BACKGROUND_TASK_RESULT)
    assert not markers
    resume_context = snapshot["work"].execution_state["external_resume_context"]
    assert len(resume_context) == 1
    assert resume_context[0]["task"] == "completed-once"
    assert resume_context[0]["execution_status"] == "succeeded"

    tool_end_events = [event for event in snapshot["stream_events"] if event.event.get("type") == "tool_end"]
    assert len(tool_end_events) == 1
    assert tool_end_events[0].event["tool_call_id"] == "call-1"
    assert tool_end_events[0].event["result"] == "completed-once"
    assert tool_end_events[0].event["work_sequence_no"] == case.initial_sequence_no

    ready_events = [event for event in snapshot["session_events"] if event.event.get("type") == "work_ready"]
    assert len(ready_events) == 1
    assert ready_events[0].event["work_id"] == case.work_id
    assert snapshot["tasks"][0].status == BackgroundTaskStatus.SUCCEEDED

    await _prepare_claimed_external_resume(case)
    materialized = await _snapshot(case)
    assert materialized["work"].status == SessionReplyWorkStatus.RUNNING
    _assert_queued_tool_result(materialized, case, "call-1")
    markers = _messages_of_type(materialized, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    marker_payload = _assert_background_result_marker(markers[0], task="completed-once", execution_status="succeeded")
    assert marker_payload["type"] == "background_tool_result"
    projected_events = [event for event in materialized["session_events"] if event.event.get("type") == "tool_results_update"]
    assert len(projected_events) == 1
    assert projected_events[0].event["messages"][0]["id"] == markers[0].id

    async with external_work_database() as db:
        work = await db.get(SessionReplyWorkItem, case.work_id)
        assert work is not None
        checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work.id)
        assert isinstance(checkpoint, dict)
        assert "deferred_external_results" not in checkpoint
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (case.work_id, "external-resume-worker")
        await external_work_module.prepare_external_work_resume(
            db,
            uid=case.uid,
            session_id=case.session_id,
            profile_id=case.profile_id,
            checkpoint=checkpoint,
        )
        await db.commit()

    repeated = await _snapshot(case)
    assert len(_messages_of_type(repeated, MessageType.BACKGROUND_TASK_RESULT)) == 1
    assert len([event for event in repeated["session_events"] if event.event.get("type") == "tool_results_update"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_external_resume_projects_tool_result_notification(external_work_database, show_tool_calls: bool):
    case = await _create_case(external_work_database)
    final_content = "completed-for-status-projection"

    async with external_work_database() as db:
        session = await db.get(ChatSession, case.session_id)
        assert session is not None
        session.show_tool_calls = show_tool_calls
        await db.commit()

    await _complete_task(case, "call-1", final_content)
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is True

    before_claim = await _snapshot(case)
    assert before_claim["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert not _messages_of_type(before_claim, MessageType.BACKGROUND_TASK_RESULT)
    assert not [event for event in before_claim["session_events"] if event.event.get("type") == "tool_results_update"]

    await _prepare_claimed_external_resume(case)
    result_manager = SessionReplyResult()
    async with external_work_database() as db:
        first_status = await result_manager.get_work_status(db, case.work_id, uid=case.uid)
    first_snapshot = await _snapshot(case)

    assert first_status["status"] == SessionReplyWorkStatus.RUNNING.value
    assert first_snapshot["work"].status == SessionReplyWorkStatus.RUNNING
    tool_result_rows = _messages_of_type(first_snapshot, MessageType.TOOL_RESULT)
    assert len(tool_result_rows) == 1
    original_tool_row = tool_result_rows[0]
    assert original_tool_row.id == case.result_message_ids["call-1"]
    _assert_queued_tool_result(first_snapshot, case, "call-1")

    markers = _messages_of_type(first_snapshot, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 1
    marker = markers[0]
    _assert_background_result_marker(marker, task=final_content, execution_status="succeeded")

    projected_events = [event for event in first_status.get("session_events", []) if event.get("type") == "tool_results_update"]
    persisted_events = [event for event in first_snapshot["session_events"] if event.event.get("type") == "tool_results_update"]
    if show_tool_calls:
        assert len(projected_events) == 1
        assert len(persisted_events) == 1
        projected_event = projected_events[0]
        projected_message = projected_event["messages"][0]
        assert projected_message["id"] == marker.id
        assert projected_message["db_id"] == marker.id
        assert projected_message["role"] == "assistant"
        assert projected_message["type"] == MessageType.BACKGROUND_TASK_RESULT.value
        assert projected_message["content"] == marker.content
        projected_payload = json.loads(projected_message["content"] or "{}")
        assert projected_payload["task"] == final_content
        assert projected_payload["execution_status"] == "succeeded"

        persisted_event = persisted_events[0].event
        assert persisted_event["type"] == projected_event["type"]
        assert persisted_event["session_id"] == projected_event["session_id"]
        assert persisted_event["work_id"] == projected_event["work_id"]
        assert persisted_event["messages"] == projected_event["messages"]
    else:
        assert not projected_events
        assert not persisted_events
        assert final_content not in json.dumps(first_status, ensure_ascii=False)
        assert final_content not in json.dumps([event.event for event in first_snapshot["session_events"]], ensure_ascii=False)

    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False
    async with external_work_database() as db:
        second_status = await result_manager.get_work_status(db, case.work_id, uid=case.uid)
    second_snapshot = await _snapshot(case)

    assert second_status == first_status
    assert len(_messages_of_type(second_snapshot, MessageType.TOOL_RESULT)) == 1
    assert len(_messages_of_type(second_snapshot, MessageType.BACKGROUND_TASK_RESULT)) == 1
    assert len([event for event in second_snapshot["session_events"] if event.event.get("type") == "tool_results_update"]) == (1 if show_tool_calls else 0)


@pytest.mark.asyncio
async def test_external_resume_delivers_ready_tasks_one_by_one(external_work_database):
    case = await _create_case(external_work_database, call_ids=("call-1", "call-2"))
    await _complete_task(case, "call-1", "first-result")

    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is True
    partial = await _snapshot(case)
    assert partial["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert not _messages_of_type(partial, MessageType.BACKGROUND_TASK_RESULT)
    partial_context = partial["work"].execution_state["external_resume_context"]
    assert len(partial_context) == 1
    assert {(payload["task"], payload["execution_status"]) for payload in partial_context} == {("first-result", "succeeded")}
    assert {slot["task_id"] for slot in partial["work"].execution_state["pending_tools"]} == {case.task_ids["call-2"]}
    assert len([event for event in partial["session_events"] if event.event.get("type") == "work_ready"]) == 1
    _assert_queued_tool_result(partial, case, "call-1")

    await _complete_task(case, "call-2", "second-result")
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-2"]) is False
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-1"]) is False

    still_ready = await _snapshot(case)
    assert still_ready["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    still_ready_context = still_ready["work"].execution_state["external_resume_context"]
    assert len(still_ready_context) == 1
    assert {(payload["task"], payload["execution_status"]) for payload in still_ready_context} == {("first-result", "succeeded")}
    assert {slot["task_id"] for slot in still_ready["work"].execution_state["pending_tools"]} == {case.task_ids["call-2"]}
    _assert_queued_tool_result(still_ready, case, "call-1")
    _assert_queued_tool_result(still_ready, case, "call-2")

    await _prepare_claimed_external_resume(case)
    async with external_work_database() as db:
        work = await db.get(SessionReplyWorkItem, case.work_id)
        assert work is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (case.work_id, "external-resume-worker")
        received = await receive_pending_external_results(
            db,
            uid=case.uid,
            session_id=case.session_id,
            profile_id=case.profile_id,
        )
        assert {payload["task"] for message in received for payload in _result_payloads(message.content or "{}")} == {
            "first-result",
            "second-result",
        }
        checkpoint = await _save_received_results_checkpoint(
            db,
            work=work,
            worker_id="external-resume-worker",
            user_message_id=case.user_message_id,
            received=received,
        )
        assert len(checkpoint["messages"]) == 3
        assert {payload["task"] for message in checkpoint["messages"][1:] for payload in _result_payloads(message["content"])} == {
            "first-result",
            "second-result",
        }
        await db.commit()

    materialized = await _snapshot(case)
    assert materialized["work"].status == SessionReplyWorkStatus.RUNNING
    assert not materialized["work"].execution_state.get("pending_tools")
    assert not materialized["work"].execution_state.get("external_resume_context")
    _assert_queued_tool_result(materialized, case, "call-1")
    _assert_queued_tool_result(materialized, case, "call-2")
    markers = _messages_of_type(materialized, MessageType.BACKGROUND_TASK_RESULT)
    assert len(markers) == 2
    assert {(payload["task"], payload["execution_status"]) for marker in markers for payload in _result_payloads(marker.content or "{}")} == {
        ("first-result", "succeeded"),
        ("second-result", "succeeded"),
    }
    for call_id in ("call-1", "call-2"):
        task = next(task for task in materialized["tasks"] if task.id == case.task_ids[call_id])
        owner = task.extra["reply_owner"]
        expected_task = "first-result" if call_id == "call-1" else "second-result"
        assert owner["work_id"] == case.work_id
        assert owner["identity"] == case.dedupe_key
        assert owner["sequence_no"] == materialized["work"].sequence_no
        assert owner["status"] == "received"
        assert owner["marker_id"] == next(marker.id for marker in markers if any(payload["task"] == expected_task for payload in _result_payloads(marker.content or "{}")))
    tool_end_events = [event for event in materialized["stream_events"] if event.event.get("type") == "tool_end"]
    assert len(tool_end_events) == 1
    assert tool_end_events[0].event["tool_call_id"] == "call-1"
    result_update_events = [event for event in materialized["session_events"] if event.event.get("type") == "tool_results_update"]
    assert result_update_events

    async with external_work_database() as db:
        work = await db.get(SessionReplyWorkItem, case.work_id)
        assert work is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (case.work_id, "external-resume-worker")
        assert (
            await receive_pending_external_results(
                db,
                uid=case.uid,
                session_id=case.session_id,
                profile_id=case.profile_id,
            )
            == []
        )
        await db.rollback()

    repeated = await _snapshot(case)
    assert len(_messages_of_type(repeated, MessageType.BACKGROUND_TASK_RESULT)) == 2
    assert len([event for event in repeated["stream_events"] if event.event.get("type") == "tool_end"]) == 1


@pytest.mark.asyncio
async def test_external_result_delivery_rolls_back_partial_claim_and_preserves_source_waits(external_work_database):
    source = await _create_case(
        external_work_database,
        call_ids=("partial-call-1", "partial-call-2"),
    )
    await _complete_task(source, "partial-call-1", "partial-result")
    target_id, target_user_id, target_worker = await _create_target_work(source, "你应该查一下")
    source_before_delivery = await _snapshot(source)

    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)
        received = await receive_pending_external_results(
            db,
            uid=source.uid,
            session_id=source.session_id,
            profile_id=source.profile_id,
        )
        assert {payload["task"] for message in received for payload in _result_payloads(message.content or "{}")} == {"partial-result"}
        checkpoint = await _build_received_results_checkpoint(
            db,
            user_message_id=target_user_id,
            received=received,
        )
        assert len(checkpoint["messages"]) == 2
        assert {payload["task"] for payload in _result_payloads(checkpoint["messages"][1]["content"])} == {"partial-result"}
        await db.rollback()

    rolled_back = await _snapshot(source)
    assert rolled_back["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert rolled_back["work"].merged_into_id is None
    assert rolled_back["work"].execution_state["pending_tools"] == source_before_delivery["work"].execution_state["pending_tools"]
    assert rolled_back["work"].execution_state.get("external_resume_context") == source_before_delivery["work"].execution_state.get("external_resume_context")
    assert not _messages_of_type(rolled_back, MessageType.BACKGROUND_TASK_RESULT)
    partial_task = next(task for task in rolled_back["tasks"] if task.id == source.task_ids["partial-call-1"])
    assert "reply_owner" not in partial_task.extra
    remaining_task = next(task for task in rolled_back["tasks"] if task.id == source.task_ids["partial-call-2"])
    assert remaining_task.status == BackgroundTaskStatus.PENDING
    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        assert target.input_message_ids == [target_user_id]
        assert "received_external_task_ids" not in target.execution_state
        checkpoint = await session_reply_work_item_crud.get_checkpoint(db, target.id)
        assert isinstance(checkpoint, dict)
        assert len(checkpoint["messages"]) == 1

    await _make_work_ready_for_claim(external_work_database, target_id)
    assert await _claim_next(external_work_database, target_worker) == target_id

    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)
        received = await receive_pending_external_results(
            db,
            uid=source.uid,
            session_id=source.session_id,
            profile_id=source.profile_id,
        )
        assert {payload["task"] for message in received for payload in _result_payloads(message.content or "{}")} == {"partial-result"}
        checkpoint = await _save_received_results_checkpoint(
            db,
            work=target,
            worker_id=target_worker,
            user_message_id=target_user_id,
            received=received,
        )
        assert [message["role"] for message in checkpoint["messages"]] == [MessageRole.USER.value, MessageRole.USER.value]
        assert {payload["task"] for payload in _result_payloads(checkpoint["messages"][1]["content"])} == {"partial-result"}
        await db.commit()

    delivered = await _snapshot(source)
    assert delivered["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert delivered["work"].merged_into_id is None
    assert {slot["task_id"] for slot in delivered["work"].execution_state["pending_tools"]} == {source.task_ids["partial-call-2"]}
    assert len(_messages_of_type(delivered, MessageType.BACKGROUND_TASK_RESULT)) == 1

    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        assert target.input_message_ids == [target_user_id]
        assert target.execution_state["received_external_task_ids"] == [source.task_ids["partial-call-1"]]
        partial_task = await db.get(BackgroundTask, source.task_ids["partial-call-1"])
        assert partial_task is not None
        assert partial_task.extra["reply_owner"]["status"] == "received"
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)
        await external_work_crud.finish_received_results(db, work=target, commit=False)
        await db.commit()

    finished = await _snapshot(source)
    partial_task = next(task for task in finished["tasks"] if task.id == source.task_ids["partial-call-1"])
    assert partial_task.extra["reply_owner"]["status"] == "handled"
    remaining_task = next(task for task in finished["tasks"] if task.id == source.task_ids["partial-call-2"])
    assert "reply_owner" not in remaining_task.extra

    await _complete_task(source, "partial-call-2", "remaining-result")
    assert await external_work_module.resume_external_work_for_task(source.task_ids["partial-call-2"]) is True
    resumed = await _snapshot(source)
    assert resumed["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    resumed_context = resumed["work"].execution_state["external_resume_context"]
    assert len(resumed_context) == 1
    assert {(payload["task"], payload["execution_status"]) for payload in resumed_context} == {("remaining-result", "succeeded")}
    remaining_task = next(task for task in resumed["tasks"] if task.id == source.task_ids["partial-call-2"])
    assert remaining_task.status == BackgroundTaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_external_result_delivery_reuses_markers_after_failed_target(external_work_database):
    source_one = await _create_case(external_work_database, call_ids=("merge-call-1",))
    source_two = await _create_case(external_work_database, call_ids=("merge-call-2",))
    await _complete_task(source_one, "merge-call-1", "merge-result-1")
    await _complete_task(source_two, "merge-call-2", "merge-result-2")

    async with external_work_database() as db:
        for source, request_id in (
            (source_one, "source-request-1"),
            (source_two, "source-request-2"),
        ):
            work = await db.get(SessionReplyWorkItem, source.work_id)
            assert work is not None
            state = dict(work.execution_state or {})
            state["request_ids"] = [request_id]
            work.execution_state = state
        await db.commit()

    target_id, target_user_id, target_worker = await _create_target_work(source_one, "合并这些结果")
    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)
        received = await receive_pending_external_results(
            db,
            uid=source_one.uid,
            session_id=source_one.session_id,
            profile_id=source_one.profile_id,
        )
        assert {payload["task"] for message in received for payload in _result_payloads(message.content or "{}")} == {
            "merge-result-1",
            "merge-result-2",
        }
        checkpoint = await _save_received_results_checkpoint(
            db,
            work=target,
            worker_id=target_worker,
            user_message_id=target_user_id,
            received=received,
        )
        assert len(checkpoint["messages"]) == 3
        assert {payload["task"] for message in checkpoint["messages"][1:] for payload in _result_payloads(message["content"])} == {
            "merge-result-1",
            "merge-result-2",
        }
        await db.commit()

    first_snapshot = await _snapshot(source_one)
    first_marker_ids = {payload["task"]: marker.id for marker in _messages_of_type(first_snapshot, MessageType.BACKGROUND_TASK_RESULT) for payload in _result_payloads(marker.content or "{}")}
    source_one_work = first_snapshot["work"]
    assert source_one_work.status == SessionReplyWorkStatus.MERGED
    assert source_one_work.merged_into_id == target_id
    source_two_snapshot = await _snapshot(source_two)
    source_two_work = source_two_snapshot["work"]
    assert source_two_work.status == SessionReplyWorkStatus.MERGED
    assert source_two_work.merged_into_id == target_id

    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        assert target.input_message_ids == [target_user_id]
        assert set(target.execution_state["request_ids"]) >= {
            "target-request-" + str(target_user_id),
            "source-request-1",
            "source-request-2",
        }
        for task_id in (source_one.task_ids["merge-call-1"], source_two.task_ids["merge-call-2"]):
            task = await db.get(BackgroundTask, task_id)
            assert task is not None
            assert task.extra["reply_owner"]["work_id"] == target_id
            assert task.extra["reply_owner"]["status"] == "received"
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)
        assert (
            await receive_pending_external_results(
                db,
                uid=source_one.uid,
                session_id=source_one.session_id,
                profile_id=source_one.profile_id,
            )
            == []
        )
        await db.rollback()

    assert len(_messages_of_type(first_snapshot, MessageType.BACKGROUND_TASK_RESULT)) == 2

    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        target.status = SessionReplyWorkStatus.FAILED
        target.locked_by = None
        target.lock_until = None
        await db.commit()

    target_two_id, target_two_user_id, target_two_worker = await _create_target_work(source_one, "再次接收已有结果")
    async with external_work_database() as db:
        target_two = await db.get(SessionReplyWorkItem, target_two_id)
        assert target_two is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_two_id, target_two_worker)
        received_again = await receive_pending_external_results(
            db,
            uid=source_one.uid,
            session_id=source_one.session_id,
            profile_id=source_one.profile_id,
        )
        assert {payload["task"] for message in received_again for payload in _result_payloads(message.content or "{}")} == {
            "merge-result-1",
            "merge-result-2",
        }
        checkpoint_two = await _save_received_results_checkpoint(
            db,
            work=target_two,
            worker_id=target_two_worker,
            user_message_id=target_two_user_id,
            received=received_again,
        )
        assert len(checkpoint_two["messages"]) == 3
        assert {payload["task"] for message in checkpoint_two["messages"][1:] for payload in _result_payloads(message["content"])} == {
            "merge-result-1",
            "merge-result-2",
        }
        await db.commit()

    second_snapshot = await _snapshot(source_one)
    assert len(_messages_of_type(second_snapshot, MessageType.BACKGROUND_TASK_RESULT)) == 2
    assert {payload["task"]: marker.id for marker in _messages_of_type(second_snapshot, MessageType.BACKGROUND_TASK_RESULT) for payload in _result_payloads(marker.content or "{}")} == first_marker_ids
    assert second_snapshot["work"].status == SessionReplyWorkStatus.MERGED
    assert second_snapshot["work"].merged_into_id == target_id

    async with external_work_database() as db:
        target_two = await db.get(SessionReplyWorkItem, target_two_id)
        assert target_two is not None
        assert target_two.input_message_ids == [target_two_user_id]
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_two_id, target_two_worker)
        await external_work_crud.finish_received_results(db, work=target_two, commit=False)
        await db.commit()

    final_snapshot = await _snapshot(source_one)
    for task_id in (source_one.task_ids["merge-call-1"], source_two.task_ids["merge-call-2"]):
        task = next(task for task in final_snapshot["tasks"] if task.id == task_id)
        assert task.extra["reply_owner"]["work_id"] == target_two_id
        assert task.extra["reply_owner"]["status"] == "handled"


@pytest.mark.asyncio
async def test_external_result_delivery_can_isolate_current_work_from_siblings(external_work_database):
    source = await _create_case(external_work_database, call_ids=("isolated-call",))
    await _complete_task(source, "isolated-call", "foreground-only-result")
    target_id, _target_user_id, target_worker = await _create_target_work(source, "isolated background agent")

    async with external_work_database() as db:
        target = await db.get(SessionReplyWorkItem, target_id)
        assert target is not None
        target.work_type = SessionReplyWorkType.BACKGROUND_TASK_RESUME
        target.source_type = SessionReplySourceType.BACKGROUND_TASK
        target.source_id = "999999"
        db.add(target)
        await db.commit()
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)

        received = await receive_pending_external_results(
            db,
            uid=source.uid,
            session_id=source.session_id,
            profile_id=source.profile_id,
        )

        assert received == []
        source_work = await db.get(SessionReplyWorkItem, source.work_id)
        assert source_work is not None
        assert source_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert source_work.merged_into_id is None
        completed_task = await db.get(BackgroundTask, source.task_ids["isolated-call"])
        assert completed_task is not None
        assert "reply_owner" not in completed_task.extra


@pytest.mark.asyncio
async def test_foreground_external_result_delivery_does_not_hijack_background_agent(external_work_database):
    source = await _create_case(external_work_database, call_ids=("background-agent-call",))
    await _complete_task(source, "background-agent-call", "background-agent-only-result")
    target_id, _target_user_id, target_worker = await _create_target_work(source, "foreground target")

    async with external_work_database() as db:
        source_work = await db.get(SessionReplyWorkItem, source.work_id)
        assert source_work is not None
        source_work.work_type = SessionReplyWorkType.BACKGROUND_TASK_RESUME
        source_work.source_type = SessionReplySourceType.BACKGROUND_TASK
        source_work.source_id = "888888"
        db.add(source_work)
        await db.commit()

        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (target_id, target_worker)
        received = await receive_pending_external_results(
            db,
            uid=source.uid,
            session_id=source.session_id,
            profile_id=source.profile_id,
        )

        assert received == []
        await db.refresh(source_work)
        assert source_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert source_work.merged_into_id is None
        completed_task = await db.get(BackgroundTask, source.task_ids["background-agent-call"])
        assert completed_task is not None
        assert "reply_owner" not in completed_task.extra
