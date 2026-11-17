import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks import external_work as external_work_module
from app.core.background_tasks import reply_trigger as reply_trigger_module
from app.core.background_tasks.result_delivery import receive_pending_external_results
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.background import background_task_crud
from app.core.session_reply_queue import executor_common as executor_common_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.tools.background_tasks import ManageBackgroundTasksExecutor
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from tests.integration.background_tasks import test_external_work_lifecycle as lifecycle_module
from tests.integration.background_tasks.test_external_work_lifecycle import (
    PROFILE_ID,
    UID,
    ExternalWorkCase,
    _assert_queued_tool_result,
    _claim_next,
    _complete_task,
    _create_case,
    _enqueue_followup,
    _fail_task,
    _messages_of_type,
    _prepare_claimed_external_resume,
    _result_payloads,
    _save_received_results_checkpoint,
    _snapshot,
)

external_work_database = lifecycle_module.external_work_database


async def _prepare_stopped_task_reply_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ledger_mode: str,
    mixed_wait: bool,
    report_state: str = "reported",
) -> tuple[ExternalWorkCase, int, list[int]]:
    call_ids = ("call-1", "call-other") if mixed_wait else ("call-1",)
    case = await _create_case(session_factory, call_ids=call_ids)
    for call_id in call_ids:
        await _complete_task(case, call_id, f"terminal-result-{call_id}")

    closing_work_id, closing_result_message_id = await _enqueue_followup(case)
    stopped_task_id = case.task_ids["call-1"]
    terminal_session_id = "terminal-shared-observation-000000000000"
    cross_uid = "external-work-cross-user"
    cross_work_specs = (
        (cross_uid, "external-work-cross-uid-session", PROFILE_ID + 1),
        (UID, "external-work-cross-session", PROFILE_ID),
        (UID, "external-work-cross-profile-session", PROFILE_ID + 2),
    )

    async with session_factory() as db:
        closing_work = await db.get(SessionReplyWorkItem, closing_work_id)
        assert closing_work is not None and closing_work.dedupe_key

        db.add_all(
            [
                User(uid=cross_uid, username=cross_uid),
                Profile(id=PROFILE_ID + 1, uid=cross_uid, name="external-work-cross-profile", configs={}),
                Profile(id=PROFILE_ID + 2, uid=UID, name="external-work-cross-profile-same-user", configs={}),
                ChatSession(session_id="external-work-cross-uid-session", uid=cross_uid, profile_id=PROFILE_ID + 1),
                ChatSession(session_id="external-work-cross-session", uid=UID, profile_id=PROFILE_ID),
                ChatSession(session_id="external-work-cross-profile-session", uid=UID, profile_id=PROFILE_ID + 2),
            ]
        )

        for call_id in call_ids:
            task_id = case.task_ids[call_id]
            task = await db.get(BackgroundTask, task_id)
            assert task is not None
            task.tool_name = TERMINAL_WATCH_TOOL_NAME
            task.arguments = {
                "terminal_session_id": terminal_session_id,
                "after_offset": 0,
                "condition": "process_exit",
            }
            task.result = {"content": {"event": "process_exit", "eof": True, "next_offset": 1}}
            task.error = None
            task.status = BackgroundTaskStatus.SUCCEEDED
            task.auto_reply = True
            task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED
            task.extra = {
                "source": "terminal_auto_watch",
                "auto_terminal_watch": True,
                "terminal_session_id": terminal_session_id,
            }
            if ledger_mode == "new-work":
                task.extra["terminal_watch_deliveries"] = {
                    str(closing_work_id): {
                        "identity": closing_work.dedupe_key,
                        "consumed": False,
                        "routed": False,
                    }
                }
            elif ledger_mode != "empty":
                raise ValueError(f"unknown ledger mode: {ledger_mode}")

        old_work = await db.get(SessionReplyWorkItem, case.work_id)
        assert old_work is not None
        old_work.execution_state = {
            "terminal_watch_waits": [
                {
                    "task_id": case.task_ids[call_id],
                    "tool_call_id": call_id,
                    "tool_name": TERMINAL_WATCH_TOOL_NAME,
                    "tool_result_message_id": case.result_message_ids[call_id],
                    "response_id": f"terminal-response-{call_id}",
                    "terminal_watch": True,
                    "terminal_auto_watch": True,
                    "terminal_session_id": terminal_session_id,
                }
                for call_id in call_ids
            ]
        }
        old_work.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        old_work.locked_by = None
        old_work.lock_until = None

        closing_work.execution_state = {"stopped_task_ids": [stopped_task_id]}
        closing_work.locked_by = None
        closing_work.lock_until = None
        if report_state == "reported":
            closing_work.status = SessionReplyWorkStatus.SUCCEEDED
            closing_work.event_sent = True
            closing_work.result_message_id = closing_result_message_id
        elif report_state == "not-succeeded":
            closing_work.status = SessionReplyWorkStatus.RUNNING
            closing_work.event_sent = True
            closing_work.result_message_id = closing_result_message_id
        elif report_state == "not-sent":
            closing_work.status = SessionReplyWorkStatus.SUCCEEDED
            closing_work.event_sent = False
            closing_work.result_message_id = closing_result_message_id
        elif report_state == "no-result":
            closing_work.status = SessionReplyWorkStatus.SUCCEEDED
            closing_work.event_sent = True
            closing_work.result_message_id = None
        else:
            raise ValueError(f"unknown report state: {report_state}")

        cross_works: list[SessionReplyWorkItem] = []
        for index, (uid, session_id, profile_id) in enumerate(cross_work_specs, start=1):
            cross_work = SessionReplyWorkItem(
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                sequence_no=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id=f"cross-source-{index}",
                dedupe_key=f"external-work-cross:{index}",
                status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                execution_state={
                    "terminal_watch_waits": [
                        {
                            "task_id": stopped_task_id,
                            "tool_call_id": "call-1",
                            "tool_name": TERMINAL_WATCH_TOOL_NAME,
                            "tool_result_message_id": case.result_message_ids["call-1"],
                            "response_id": "cross-terminal-response",
                            "terminal_watch": True,
                            "terminal_auto_watch": True,
                            "terminal_session_id": terminal_session_id,
                        }
                    ]
                },
            )
            db.add(cross_work)
            cross_works.append(cross_work)

        await db.commit()
        assert all(work.id is not None for work in cross_works)
        return case, closing_work_id, [work.id for work in cross_works if work.id is not None]


async def _snapshot_stopped_task_replies(
    case: ExternalWorkCase,
    *,
    closing_work_id: int,
    cross_work_ids: list[int],
    task_id: int,
) -> dict[str, Any]:
    async with case.factory() as db:
        old_work = await db.get(SessionReplyWorkItem, case.work_id)
        closing_work = await db.get(SessionReplyWorkItem, closing_work_id)
        task = await db.get(BackgroundTask, task_id)
        assert old_work is not None and closing_work is not None and task is not None
        old_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, old_work.id)
        cross_works = [await db.get(SessionReplyWorkItem, work_id) for work_id in cross_work_ids]
        assert all(work is not None for work in cross_works)

        session = await session_crud.get_by_session_id(db, case.session_id)
        assert session is not None
        activity = next(row for row in await session_crud.get_user_activity(db, uid=case.uid) if row.session_id == case.session_id)
        return {
            "old": (old_work.status, old_work.execution_state, old_work.locked_by, old_work.lock_until, old_checkpoint),
            "closing": (closing_work.status, closing_work.event_sent, closing_work.result_message_id, closing_work.execution_state),
            "cross": {work.id: (work.status, work.execution_state, work.locked_by, work.lock_until) for work in cross_works if work is not None and work.id is not None},
            "task": (task.status, task.reply_status, task.extra, task.result),
            "session_is_reply_running": getattr(activity, "is_reply_running", getattr(session, "is_reply_running", None)),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "resume_before_delivery", "remaining_task"),
    [
        ("running", False, False),
        ("failed", False, False),
        ("failed", True, False),
        ("succeeded", False, False),
        ("succeeded", True, False),
        ("succeeded", False, True),
    ],
    ids=[
        "running-not-resumed",
        "failed-not-resumed",
        "failed-resumed",
        "succeeded-not-resumed",
        "succeeded-resumed",
        "succeeded-with-remaining-task",
    ],
)
async def test_cancel_reply_delivery_cancels_original_external_work(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
    task_status: str,
    resume_before_delivery: bool,
    remaining_task: bool,
) -> None:
    case = await _create_case(external_work_database, call_ids=("call-1", "call-remaining") if remaining_task else ("call-1",))
    unrelated_case = await _create_case(external_work_database, call_ids=("call-2",))
    task_id = case.task_ids["call-1"]

    if task_status == "running":
        async with external_work_database() as db:
            claimed = await background_task_crud.try_claim(
                db,
                task_id=task_id,
                worker_id=f"external-task-worker-{task_id}",
                lease_seconds=3600,
            )
            assert claimed is not None
            assert claimed.status == BackgroundTaskStatus.RUNNING
    elif task_status == "failed":
        await _fail_task(case, "call-1", error="failed-before-cancel", content="failed-result-before-cancel")
    else:
        await _complete_task(case, "call-1", "succeeded-result-before-cancel")

    before = await _snapshot(case)
    before_task = next(task for task in before["tasks"] if task.id == task_id)
    before_task_result = before_task.result
    before_task_error = before_task.error
    before_remaining_task = next((task for task in before["tasks"] if task.id == case.task_ids.get("call-remaining")), None)
    assert before["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    if remaining_task:
        assert before_remaining_task is not None
        assert before_remaining_task.status == BackgroundTaskStatus.PENDING

    stop_work_id, _ = await _enqueue_followup(case)
    assert await _claim_next(external_work_database, "stop-worker") == stop_work_id

    if resume_before_delivery:
        assert task_status != "running"
        assert await external_work_module.resume_external_work_for_task(task_id) is (not remaining_task)
        resumed_before_delivery = await _snapshot(case)
        assert resumed_before_delivery["work"].status == (SessionReplyWorkStatus.WAITING_EXTERNAL_WORK if remaining_task else SessionReplyWorkStatus.READY_FOR_LLM)

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)

    async def execute_foreground(db, work, _worker_id):
        cancel_executor = ManageBackgroundTasksExecutor(project_root=".", uid=work.uid)
        cancel_executor.set_runtime_context(db=db, session_id=work.session_id)
        cancel_response = json.loads(await cancel_executor.execute(operation="cancel", task_id=task_id))
        assert cancel_response["status"] == "success"
        assert cancel_response["operation"] == "cancel"
        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="stop response"),
            work.profile_id,
            dedupe_key=executor_common_module._result_message_dedupe_key(work),
            commit=False,
        )
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "stop response"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    delivered_events: list[dict[str, Any]] = []

    async def capture_session_event(_uid: str, _session_id: str, event: dict[str, Any]) -> None:
        delivered_events.append(event)

    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)

    await executor_lifecycle_module.execute_session_reply_work(stop_work_id, "stop-worker")

    assert delivered_events and delivered_events[-1]["work_id"] == stop_work_id
    after = await _snapshot(case)
    assert after["work"].status == (SessionReplyWorkStatus.WAITING_EXTERNAL_WORK if remaining_task else SessionReplyWorkStatus.CANCELLED)
    assert await _claim_next(external_work_database, "must-not-claim") is None
    assert await external_work_module.resume_external_work_for_task(task_id) is False

    task = next(task for task in after["tasks"] if task.id == task_id)
    expected_task_status = BackgroundTaskStatus.CANCELLED if task_status == "running" else BackgroundTaskStatus(task_status)
    assert task.status == expected_task_status
    assert task.result == before_task_result
    assert task.error == before_task_error
    _assert_queued_tool_result(after, case, "call-1")
    if remaining_task:
        remaining_task_row = next(task for task in after["tasks"] if task.id == case.task_ids["call-remaining"])
        assert remaining_task_row.status == BackgroundTaskStatus.PENDING
        assert remaining_task_row.result == before_remaining_task.result
        assert remaining_task_row.error == before_remaining_task.error

    async with external_work_database() as db:
        stop_work = await db.get(SessionReplyWorkItem, stop_work_id)
    assert stop_work is not None
    assert stop_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert stop_work.event_sent is True

    assistant_texts = [message for message in after["messages"] if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(assistant_texts) == 1
    assert assistant_texts[0].content == "stop response"

    if remaining_task:
        await _complete_task(case, "call-remaining", "remaining-result")
        assert await external_work_module.resume_external_work_for_task(case.task_ids["call-remaining"]) is True
        resumed = await _snapshot(case)
        assert resumed["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
        _assert_queued_tool_result(resumed, case, "call-1")
        _assert_queued_tool_result(resumed, case, "call-remaining")
        completed_remaining_task = next(task for task in resumed["tasks"] if task.id == case.task_ids["call-remaining"])
        assert completed_remaining_task.status == BackgroundTaskStatus.SUCCEEDED
        assert completed_remaining_task.result == {"content": "remaining-result"}
        assert completed_remaining_task.error is None

    unrelated = await _snapshot(unrelated_case)
    assert unrelated["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert unrelated["work"].locked_by is None
    _assert_queued_tool_result(unrelated, unrelated_case, "call-2")
    unrelated_task = next(task for task in unrelated["tasks"] if task.id == unrelated_case.task_ids["call-2"])
    assert unrelated_task.status == BackgroundTaskStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_completed_external_work_waits_for_stop_reply_delivery(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _create_case(external_work_database)
    task_id = case.task_ids["call-1"]
    await _complete_task(case, "call-1", "completed-before-transport-failure")
    before = await _snapshot(case)
    before_task = next(task for task in before["tasks"] if task.id == task_id)

    stop_work_id, _ = await _enqueue_followup(case)
    assert await _claim_next(external_work_database, "stop-worker") == stop_work_id

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)

    async def execute_foreground(db, work, _worker_id):
        cancel_executor = ManageBackgroundTasksExecutor(project_root=".", uid=work.uid)
        cancel_executor.set_runtime_context(db=db, session_id=work.session_id)
        cancel_response = json.loads(await cancel_executor.execute(operation="cancel", task_id=task_id))
        assert cancel_response["status"] == "success"
        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="stop response after failed delivery"),
            work.profile_id,
            dedupe_key=executor_common_module._result_message_dedupe_key(work),
            commit=False,
        )
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "stop response after failed delivery"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    async def fail_session_event(_uid: str, _session_id: str, _event: dict[str, Any]) -> None:
        raise RuntimeError("stop reply transport failed")

    monkeypatch.setattr(executor_lifecycle_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", fail_session_event)

    with pytest.raises(RuntimeError, match="stop reply transport failed"):
        await executor_lifecycle_module.execute_session_reply_work(stop_work_id, "stop-worker")

    waiting = await _snapshot(case)
    assert waiting["work"].status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    task = next(task for task in waiting["tasks"] if task.id == task_id)
    assert task.status == BackgroundTaskStatus.SUCCEEDED
    assert task.result == before_task.result
    assert task.error == before_task.error

    async with external_work_database() as db:
        stop_work = await db.get(SessionReplyWorkItem, stop_work_id)
    assert stop_work is not None
    assert stop_work.status == SessionReplyWorkStatus.RUNNING
    assert stop_work.event_sent is False

    assert await external_work_module.resume_external_work_for_task(task_id) is True
    resumed = await _snapshot(case)
    assert resumed["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    _assert_queued_tool_result(resumed, case, "call-1")


@pytest.mark.asyncio
async def test_ordinary_result_completed_during_reply_delivery_is_requeued_after_completion(
    external_work_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _create_case(external_work_database, call_ids=("call-first", "call-late"))
    await _complete_task(case, "call-first", "first result delivered")
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-first"]) is True
    assert await _claim_next(external_work_database, "delivery-window-worker") == case.work_id
    before_delivery = await _snapshot(case)
    assert before_delivery["work"].status == SessionReplyWorkStatus.RUNNING
    delivery_sequence_no = before_delivery["work"].sequence_no

    external_resume_calls = 0

    async def execute_external_resume(db, work, worker_id):
        nonlocal external_resume_calls
        external_resume_calls += 1
        assert work.id == case.work_id
        assert worker_id == "delivery-window-worker"
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (case.work_id, worker_id)
        received = await receive_pending_external_results(
            db,
            uid=case.uid,
            session_id=case.session_id,
            profile_id=case.profile_id,
        )
        assert {payload["task"] for message in received for payload in _result_payloads(message.content or "{}")} == {"first result delivered"}
        checkpoint = await _save_received_results_checkpoint(
            db,
            work=work,
            worker_id=worker_id,
            user_message_id=case.user_message_id,
            received=received,
        )
        assert {payload["task"] for payload in _result_payloads(checkpoint["messages"][1]["content"])} == {"first result delivered"}
        await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="first result delivered"),
            work.profile_id,
            dedupe_key=executor_common_module._result_message_dedupe_key(work),
            commit=False,
        )
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "first result delivered"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    delivered_events: list[dict[str, Any]] = []
    late_resume_result: bool | None = None

    async def capture_session_event(_uid: str, _session_id: str, event: dict[str, Any]) -> None:
        nonlocal late_resume_result
        delivered_events.append(event)
        await _complete_task(case, "call-late", "late result delivered")
        late_resume_result = await external_work_module.resume_external_work_for_task(case.task_ids["call-late"])
        assert late_resume_result is False
        during_delivery = await _snapshot(case)
        assert during_delivery["work"].status == SessionReplyWorkStatus.RUNNING

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", external_work_database)
    monkeypatch.setattr(executor_lifecycle_module, "_execute_external_resume", execute_external_resume)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)

    await executor_lifecycle_module.execute_session_reply_work(case.work_id, "delivery-window-worker")

    after = await _snapshot(case)
    assert after["work"].id == case.work_id
    assert after["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert after["work"].sequence_no > delivery_sequence_no
    assert external_resume_calls == 1
    assert late_resume_result is False

    resume_context = after["work"].execution_state["external_resume_context"]
    assert len(resume_context) == 1
    assert {(payload["task"], payload["execution_status"]) for payload in resume_context} == {("late result delivered", "succeeded")}
    _assert_queued_tool_result(after, case, "call-first")
    _assert_queued_tool_result(after, case, "call-late")

    first_task = next(task for task in after["tasks"] if task.id == case.task_ids["call-first"])
    late_task = next(task for task in after["tasks"] if task.id == case.task_ids["call-late"])
    assert first_task.status == BackgroundTaskStatus.SUCCEEDED
    assert first_task.result == {"content": "first result delivered"}
    assert first_task.extra["reply_owner"]["status"] == "handled"
    assert late_task.status == BackgroundTaskStatus.SUCCEEDED
    assert late_task.result == {"content": "late result delivered"}
    assert "reply_owner" not in late_task.extra

    assistant_texts = [message for message in after["messages"] if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
    assert len(assistant_texts) == 1
    assert assistant_texts[0].content == "first result delivered"
    assert len(delivered_events) == 1
    assert delivered_events[0]["work_id"] == case.work_id

    async with external_work_database() as db:
        works = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == case.session_id,
                        SessionReplyWorkItem.uid == case.uid,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [work.id for work in works] == [case.work_id]

    ready_count = len([event for event in after["session_events"] if event.event.get("type") == "work_ready"])
    marker_count = len(_messages_of_type(after, MessageType.BACKGROUND_TASK_RESULT))
    tool_end_count = len([event for event in after["stream_events"] if event.event.get("type") == "tool_end"])
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-late"]) is False
    repeated = await _snapshot(case)
    assert len([event for event in repeated["session_events"] if event.event.get("type") == "work_ready"]) == ready_count
    assert len(_messages_of_type(repeated, MessageType.BACKGROUND_TASK_RESULT)) == marker_count
    assert len([event for event in repeated["stream_events"] if event.event.get("type") == "tool_end"]) == tool_end_count
    assert len(delivered_events) == 1
    assert await _claim_next(external_work_database, "post-delivery-worker") == case.work_id


@pytest.mark.asyncio
@pytest.mark.parametrize("late_has_file", [False, True], ids=["without-file", "with-file"])
async def test_reopened_completed_work_keeps_only_new_result_files(
    external_work_database,
    late_has_file: bool,
) -> None:
    case = await _create_case(external_work_database, call_ids=("call-delivered", "call-late"))
    old_file = {"id": "already-delivered-file", "name": "old.txt", "mime_type": "text/plain"}
    new_file = {"id": "new-result-file", "name": "new.txt", "mime_type": "text/plain"}
    delivered_content = json.dumps({"type": "files_to_user", "files": [old_file]})
    late_content = json.dumps({"type": "files_to_user", "files": [new_file]}) if late_has_file else "late result without file"

    await _complete_task(case, "call-delivered", delivered_content)
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-delivered"]) is True
    await _prepare_claimed_external_resume(case)

    old_history_content = json.dumps({"type": "assistant_files", "text": "", "files": [old_file]})
    resume_worker = "external-resume-worker"
    async with external_work_database() as db:
        work = await db.get(SessionReplyWorkItem, case.work_id)
        assert work is not None
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (case.work_id, resume_worker)
        received = await receive_pending_external_results(
            db,
            uid=case.uid,
            session_id=case.session_id,
            profile_id=case.profile_id,
        )
        assert {payload["task"] for message in received for payload in _result_payloads(message.content or "{}")} == {delivered_content}
        checkpoint = await _save_received_results_checkpoint(
            db,
            work=work,
            worker_id=resume_worker,
            user_message_id=case.user_message_id,
            received=received,
        )
        checkpoint["files_to_user"] = [old_file]
        state = dict(work.execution_state)
        assert await session_reply_work_item_crud.update_claimed(
            db,
            work_id=case.work_id,
            worker_id=resume_worker,
            values={"execution_state": state},
            commit=False,
        )
        await db.flush()
        await session_reply_work_item_crud.save_checkpoint(db, work.id, checkpoint)
        old_history = await save_message(
            db,
            case.session_id,
            case.uid,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content=old_history_content),
            case.profile_id,
            dedupe_key=executor_common_module._result_message_dedupe_key(work),
            commit=False,
        )
        assert old_history.id is not None
        assert await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=case.work_id,
            worker_id=resume_worker,
            status=SessionReplyWorkStatus.SUCCEEDED,
            result_message_id=old_history.id,
            event_sent=True,
            commit=False,
        )
        await external_work_crud.finish_received_results(db, work=work, commit=False)
        await db.commit()

    delivered = await _snapshot(case)
    assert delivered["work"].status == SessionReplyWorkStatus.SUCCEEDED
    assert delivered["work"].event_sent is True
    assert delivered["work"].result_message_id == old_history.id
    assert delivered["checkpoint"]["files_to_user"] == [old_file]
    assert {slot["task_id"] for slot in delivered["work"].execution_state["pending_tools"]} == {case.task_ids["call-late"]}
    late_task_before = next(task for task in delivered["tasks"] if task.id == case.task_ids["call-late"])
    assert late_task_before.status == BackgroundTaskStatus.PENDING
    delivered_history = next(message for message in delivered["messages"] if message.id == old_history.id)
    assert delivered_history.content == old_history_content

    await _complete_task(case, "call-late", late_content)
    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-late"]) is True

    after = await _snapshot(case)
    assert after["work"].id == case.work_id
    assert after["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    expected_files = [new_file] if late_has_file else []
    assert after["checkpoint"].get("files_to_user", []) == expected_files

    preserved_history = next(message for message in after["messages"] if message.id == old_history.id)
    assert preserved_history.id == old_history.id
    assert preserved_history.content == old_history_content
    assert len([message for message in after["messages"] if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT and message.content == old_history_content]) == 1

    delivered_task = next(task for task in after["tasks"] if task.id == case.task_ids["call-delivered"])
    late_task = next(task for task in after["tasks"] if task.id == case.task_ids["call-late"])
    assert delivered_task.status == BackgroundTaskStatus.SUCCEEDED
    assert delivered_task.result == {"content": delivered_content}
    assert delivered_task.extra["reply_owner"]["status"] == "handled"
    assert late_task.status == BackgroundTaskStatus.SUCCEEDED
    assert late_task.result == {"content": late_content}

    async with external_work_database() as db:
        works = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == case.session_id,
                        SessionReplyWorkItem.uid == case.uid,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [work.id for work in works] == [case.work_id]

    assert await external_work_module.resume_external_work_for_task(case.task_ids["call-late"]) is False
    repeated = await _snapshot(case)
    assert repeated["work"].status == SessionReplyWorkStatus.READY_FOR_LLM
    assert repeated["checkpoint"].get("files_to_user", []) == expected_files
    repeated_history = next(message for message in repeated["messages"] if message.id == old_history.id)
    assert repeated_history.id == old_history.id
    assert repeated_history.content == old_history_content
    assert len([message for message in repeated["messages"] if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT and message.content == old_history_content]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_mode", ["empty", "new-work"], ids=["empty-ledger", "new-work-ledger"])
@pytest.mark.parametrize("mixed_wait", [False, True], ids=["only-stopped-task", "stopped-and-other-task"])
async def test_complete_stopped_task_replies_cleans_unledgered_terminal_watch_waits(
    external_work_database,
    ledger_mode: str,
    mixed_wait: bool,
) -> None:
    case, closing_work_id, cross_work_ids = await _prepare_stopped_task_reply_case(
        external_work_database,
        ledger_mode=ledger_mode,
        mixed_wait=mixed_wait,
    )
    task_id = case.task_ids["call-1"]
    before = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=task_id,
    )
    assert before["closing"][0] == SessionReplyWorkStatus.SUCCEEDED
    assert before["closing"][1] is True
    assert before["closing"][2] is not None
    assert before["task"][0] == BackgroundTaskStatus.SUCCEEDED
    assert before["task"][1] == BackgroundTaskReplyStatus.SUCCEEDED

    async with external_work_database() as db:
        await external_work_module.complete_stopped_task_replies(db, work_id=closing_work_id)
        await db.commit()

    first = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=task_id,
    )
    if mixed_wait:
        assert first["old"][0] == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert {slot["task_id"] for slot in first["old"][1]["terminal_watch_waits"]} == {case.task_ids["call-other"]}
        assert first["session_is_reply_running"] is True
    else:
        assert first["old"][0] == SessionReplyWorkStatus.CANCELLED
        assert "terminal_watch_waits" not in first["old"][1]
        assert first["session_is_reply_running"] is False
    assert first["cross"] == before["cross"]
    assert first["task"] == before["task"]

    async with external_work_database() as db:
        await external_work_module.complete_stopped_task_replies(db, work_id=closing_work_id)
        await db.commit()

    repeated = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=task_id,
    )
    assert repeated == first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report_state",
    ["not-succeeded", "not-sent", "no-result"],
    ids=["not-succeeded", "event-not-sent", "result-not-persisted"],
)
async def test_complete_stopped_task_replies_requires_successful_stop_report(
    external_work_database,
    report_state: str,
) -> None:
    case, closing_work_id, cross_work_ids = await _prepare_stopped_task_reply_case(
        external_work_database,
        ledger_mode="empty",
        mixed_wait=False,
        report_state=report_state,
    )
    task_id = case.task_ids["call-1"]
    before = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=task_id,
    )

    async with external_work_database() as db:
        await external_work_module.complete_stopped_task_replies(db, work_id=closing_work_id)
        await db.commit()

    after = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=task_id,
    )
    assert after == before
    assert after["old"][0] == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert {slot["task_id"] for slot in after["old"][1]["terminal_watch_waits"]} == {task_id}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stopped_position",
    ["first", "nested", "all"],
    ids=["top-level-stopped", "nested-stopped", "all-stopped"],
)
async def test_complete_stopped_task_replies_merges_external_resume_context_tasks(
    external_work_database,
    stopped_position: str,
) -> None:
    case, closing_work_id, cross_work_ids = await _prepare_stopped_task_reply_case(
        external_work_database,
        ledger_mode="empty",
        mixed_wait=True,
    )
    first_task_id = case.task_ids["call-1"]
    second_task_id = case.task_ids["call-other"]
    stopped_task_ids = {
        "first": [first_task_id],
        "nested": [second_task_id],
        "all": [first_task_id, second_task_id],
    }[stopped_position]
    checkpoint = {
        "deferred_external_results": True,
        "messages": [{"role": "user", "content": "preserve-message"}],
        "turn_messages": [{"role": "assistant", "content": "preserve-turn"}],
        "preserve": {"value": "checkpoint-field"},
    }

    async with external_work_database() as db:
        first_task = await db.get(BackgroundTask, first_task_id)
        second_task = await db.get(BackgroundTask, second_task_id)
        old_work = await db.get(SessionReplyWorkItem, case.work_id)
        closing_work = await db.get(SessionReplyWorkItem, closing_work_id)
        assert first_task is not None and second_task is not None
        assert old_work is not None and closing_work is not None
        first_payload = reply_trigger_module._build_background_tool_result_payload(first_task)
        second_payload = reply_trigger_module._build_background_tool_result_payload(second_task)
        old_work.execution_state = {
            "external_resume_context": [
                {
                    **first_payload,
                    "tasks": [first_payload, second_payload],
                }
            ],
            "terminal_auto_watch_task_ids": [first_task_id, second_task_id],
            "external_resume_pending": True,
            "terminal_auto_resume": True,
        }
        closing_work.execution_state = {"stopped_task_ids": stopped_task_ids}
        await db.flush()
        await session_reply_work_item_crud.save_checkpoint(db, old_work.id, checkpoint)
        await db.commit()

    before = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=first_task_id,
    )
    before_state = before["old"][1]
    assert set(before_state) == {
        "external_resume_context",
        "terminal_auto_watch_task_ids",
        "external_resume_pending",
        "terminal_auto_resume",
    }
    assert before_state["external_resume_context"][0]["task_id"] == first_task_id
    assert before_state["external_resume_context"][0]["tasks"] == [first_payload, second_payload]
    assert before["old"][4]["deferred_external_results"] is True

    async with external_work_database() as db:
        await external_work_module.complete_stopped_task_replies(db, work_id=closing_work_id)
        await db.commit()

    first = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=first_task_id,
    )
    assert first["cross"] == before["cross"]
    if stopped_position == "all":
        assert first["old"][0] == SessionReplyWorkStatus.CANCELLED
        assert first["session_is_reply_running"] is False
        assert first["old"][1] == {}
        assert first["old"][4] == {key: value for key, value in checkpoint.items() if key != "deferred_external_results"}
    else:
        remaining_task_id = second_task_id if stopped_position == "first" else first_task_id
        remaining_payload = second_payload if stopped_position == "first" else first_payload
        state = first["old"][1]
        assert first["old"][0] == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert first["session_is_reply_running"] is True
        assert state["terminal_auto_watch_task_ids"] == [remaining_task_id]
        assert state["external_resume_pending"] is True
        assert "terminal_auto_resume" not in state
        assert first["old"][4] == checkpoint
        assert len(state["external_resume_context"]) == 1
        merged_payload = state["external_resume_context"][0]
        for key, value in remaining_payload.items():
            assert merged_payload[key] == value
        assert merged_payload["tasks"] == [remaining_payload]

    async with external_work_database() as db:
        await external_work_module.complete_stopped_task_replies(db, work_id=closing_work_id)
        await db.commit()

    repeated = await _snapshot_stopped_task_replies(
        case,
        closing_work_id=closing_work_id,
        cross_work_ids=cross_work_ids,
        task_id=first_task_id,
    )
    assert repeated == first
