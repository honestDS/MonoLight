import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import select

from app.core.background_tasks import runner as runner_module
from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.session.cleanup import delete_session_data
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, Message, MessageRole
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from tests.integration.chat.test_chat_stop_workflow import (
    _build_app,
    _patch_queue_runtime_database,
    _seed_reply_work,
)
from tests.integration.chat.test_chat_stop_workflow import (
    db_session as _db_session,
)

db_session = _db_session


@pytest.mark.asyncio
async def test_executor_rejects_late_assistant_save_after_http_stop(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="executor-stop-session",
        status=SessionReplyWorkStatus.RUNNING,
        worker_id="executor-worker",
    )
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    cancellation_seen = asyncio.Event()
    late_save_attempted = asyncio.Event()
    dispatch_calls = 0

    async def controlled_dispatch(**kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        dispatch_started.set()
        try:
            await release_dispatch.wait()
            late_save_attempted.set()
            await save_assistant_message(
                kwargs["db"],
                kwargs["session_id"],
                kwargs["uid"],
                kwargs["persisted_profile_id"],
                InternalMessage(role=MessageRole.ASSISTANT, content="late assistant"),
                dedupe_key=kwargs["final_message_dedupe_key"],
            )
        except asyncio.CancelledError:
            cancellation_seen.set()
            raise
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "late assistant"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    monkeypatch.setattr(ChatDispatcher, "dispatch", controlled_dispatch)
    execution_task = asyncio.create_task(executor_lifecycle_module.execute_session_reply_work(work_id, "executor-worker"))
    try:
        await asyncio.wait_for(dispatch_started.wait(), timeout=2)
        app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": session_id},
            )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"]["cancelled_count"] == 1
        release_dispatch.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(execution_task, timeout=2)
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)

    assert late_save_attempted.is_set()
    assert cancellation_seen.is_set()
    assert dispatch_calls == 1
    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert all(message.role != MessageRole.ASSISTANT for message in messages)


@pytest.mark.asyncio
async def test_cancelled_stream_is_terminal_and_late_content_is_not_persisted(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="stream-stop-session",
        status=SessionReplyWorkStatus.CANCELLED,
        worker_id=None,
        stream_requested=True,
    )

    events = [event async for event in session_reply_queue_manager.wait_for_stream(work_id)]
    assert [event["type"] for event in events] == ["cancelled"]
    assert events[0]["session_id"] == session_id
    assert events[0]["work_id"] == work_id
    assert not {event["type"] for event in events} & {"error", "done"}

    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
    assert work is not None
    stream_state = executor_interactive_module._InteractiveWorkStreamEventState(
        work=work,
        worker_id="stream-worker",
        next_sequence=1,
        dequeued_request_ids=set(),
        turn_end_content_by_response_id={},
        tool_names_by_response_id={},
    )
    with pytest.raises(asyncio.CancelledError):
        await executor_interactive_module._persist_interactive_work_stream_event(
            stream_state,
            {"type": "content", "content": "late content", "response_id": "late-response"},
        )

    async with db_session() as db:
        persisted_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == work_id))).scalars().all())
    assert persisted_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_requested", [False, True])
async def test_consumer_cancels_http_stopped_claim_without_retry(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    stream_requested: bool,
) -> None:
    _patch_queue_runtime_database(monkeypatch, db_session)
    work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="consumer-stop-session",
        status=SessionReplyWorkStatus.READY_FOR_LLM,
        worker_id=None,
        stream_requested=stream_requested,
    )
    dispatch_started = asyncio.Event()
    dispatch_cancelled = asyncio.Event()
    dispatch_finished = asyncio.Event()
    release_dispatch = asyncio.Event()
    dispatch_calls = 0

    async def runtime_settings(_db):
        return SimpleNamespace(agent_work_max_concurrency=1)

    async def controlled_dispatch(**_kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError:
            dispatch_cancelled.set()
            raise
        finally:
            dispatch_finished.set()
        return {
            "choices": [
                {
                    "message": {"role": MessageRole.ASSISTANT, "content": "should not persist"},
                    "finish_reason": "stop",
                }
            ],
            "history": [],
            "files": None,
        }

    monkeypatch.setattr(consumer_module.system_setting_crud, "get_runtime_settings", runtime_settings)
    if stream_requested:
        import app.core.dispatchers.stream as stream_module

        monkeypatch.setattr(stream_module, "dispatch_interactive", controlled_dispatch)
    else:
        monkeypatch.setattr(ChatDispatcher, "dispatch", controlled_dispatch)
    consumer = consumer_module.SessionReplyConsumer()
    consumer_task = consumer.start()
    try:
        await asyncio.wait_for(dispatch_started.wait(), timeout=2)
        app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": session_id},
            )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"]["cancelled_count"] == 1
        await asyncio.wait_for(dispatch_cancelled.wait(), timeout=2)
    finally:
        await consumer.stop()
        if not consumer_task.done():
            consumer_task.cancel()
        await asyncio.gather(consumer_task, return_exceptions=True)

    assert dispatch_finished.is_set()
    assert dispatch_calls == 1
    async with db_session() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        all_works = list((await db.execute(select(SessionReplyWorkItem))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        stream_events = list((await db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == work_id))).scalars().all())
    assert work is not None
    assert work.status == SessionReplyWorkStatus.CANCELLED
    assert work.locked_by is None
    assert work.attempt_count == 1
    assert len(all_works) == 1
    assert all(message.role != MessageRole.ASSISTANT for message in messages)
    event_types = [stream_event.event["type"] for stream_event in stream_events]
    if stream_requested:
        assert set(event_types) <= {"task_start"}
    else:
        assert stream_events == []
    assert not set(event_types) & {"done", "error", "content"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [TERMINAL_WATCH_TOOL_NAME, "ordinary_background_tool"],
    ids=["terminal-watch", "ordinary-background"],
)
async def test_http_stop_cancels_running_background_runner(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    _work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id=f"background-runner-stop-{tool_name}",
        status=SessionReplyWorkStatus.RUNNING,
        worker_id="reply-worker",
    )
    arguments = (
        {
            "terminal_session_id": "background-runner-stop-terminal",
            "after_offset": 0,
            "condition": "process_exit",
        }
        if tool_name == TERMINAL_WATCH_TOOL_NAME
        else {"value": "blocking"}
    )
    async with db_session() as db:
        task = await background_task_crud.create_task(
            db,
            uid="owner",
            session_id=session_id,
            profile_id=1,
            tool_call_id=f"{session_id}-tool-call",
            tool_name=tool_name,
            arguments=arguments,
            auto_reply=True,
            extra={"source": "test"},
        )
    assert task.id is not None
    assert task.status == BackgroundTaskStatus.PENDING
    assert task.reply_status == BackgroundTaskReplyStatus.PENDING
    task_id = task.id

    execution_started = asyncio.Event()
    execution_cancelled = asyncio.Event()
    release_execution = asyncio.Event()

    class BlockingExecutor:
        requires_audit = False

        def __init__(self, project_root: str, uid: str = "default") -> None:
            self.project_root = project_root
            self.uid = uid

        def set_config(self, _cfg) -> None:
            pass

        def set_runtime_context(self, **_kwargs) -> None:
            pass

        async def execute(self, **_kwargs):
            execution_started.set()
            try:
                await release_execution.wait()
            except asyncio.CancelledError:
                execution_cancelled.set()
                raise
            return {"content": "unexpected completion"}

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", db_session)
    monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, tool_name, BlockingExecutor)
    runner_task = asyncio.create_task(runner_module.run_background_task(task_id, worker_id="background-worker"))
    try:
        await asyncio.wait_for(execution_started.wait(), timeout=5)
        app = _build_app(db_session, {"uid": "owner", "is_superuser": False})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            stopped = await client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": session_id},
            )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"] == {
            "session_id": session_id,
            "cancelled_count": 1,
        }
        await asyncio.wait_for(execution_cancelled.wait(), timeout=5)
        await asyncio.wait_for(runner_task, timeout=5)
    finally:
        if not runner_task.done():
            runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)

    async with db_session() as db:
        cancelled_task = await db.get(BackgroundTask, task_id)
        late_claim = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id="late-worker",
        )
    assert late_claim is None
    assert cancelled_task is not None
    assert cancelled_task.status == BackgroundTaskStatus.CANCELLED
    assert cancelled_task.auto_reply is False
    assert cancelled_task.reply_status == BackgroundTaskReplyStatus.NONE
    assert cancelled_task.locked_by is None
    assert cancelled_task.lock_until is None
    assert cancelled_task.reply_locked_by is None
    assert cancelled_task.reply_lock_until is None
    assert cancelled_task.result is None

    async with db_session() as db:
        tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
    assert len(tasks) == 1
    assert tasks[0].id == task_id
    assert tasks[0].result is None


@pytest.mark.asyncio
async def test_delete_session_cancels_running_background_runner(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _work_id, _message_id, session_id = await _seed_reply_work(
        db_session,
        session_id="background-delete-session",
        status=SessionReplyWorkStatus.RUNNING,
        worker_id="reply-worker",
    )
    async with db_session() as db:
        task = BackgroundTask(
            uid="owner",
            session_id=session_id,
            profile_id=1,
            tool_call_id=f"{session_id}-tool-call",
            tool_name="ordinary_background_tool",
            arguments={},
            auto_reply=True,
            extra={},
        )
        db.add(task)
        await db.commit()
        assert task.id is not None
        task_id = task.id

    execution_started = asyncio.Event()
    execution_cancelled = asyncio.Event()
    release_execution = asyncio.Event()

    class BlockingExecutor:
        requires_audit = False

        def __init__(self, project_root: str, uid: str = "default") -> None:
            self.project_root = project_root
            self.uid = uid

        def set_config(self, _cfg) -> None:
            pass

        def set_runtime_context(self, **_kwargs) -> None:
            pass

        async def execute(self, **_kwargs):
            execution_started.set()
            try:
                await release_execution.wait()
            except asyncio.CancelledError:
                execution_cancelled.set()
                raise
            return {"content": "unexpected completion"}

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", db_session)
    monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "ordinary_background_tool", BlockingExecutor)
    runner_task = asyncio.create_task(runner_module.run_background_task(task_id, worker_id="background-worker"))
    try:
        await asyncio.wait_for(execution_started.wait(), timeout=5)
        async with db_session() as db:
            deleted = await delete_session_data(
                db,
                session_id=session_id,
                uid="owner",
                is_admin=False,
            )
            await db.commit()
        assert deleted is True
        await asyncio.wait_for(execution_cancelled.wait(), timeout=5)
        runner_result = await asyncio.wait_for(runner_task, timeout=5)
        assert runner_result is None
    finally:
        if not runner_task.done():
            runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)

    async with db_session() as db:
        deleted_session = await db.get(ChatSession, session_id)
        cancelled_task = await db.get(BackgroundTask, task_id)
        late_claim = await background_task_crud.try_claim(
            db,
            task_id=task_id,
            worker_id="late-worker",
        )
    assert deleted_session is None
    assert late_claim is None
    assert cancelled_task is not None
    assert cancelled_task.status == BackgroundTaskStatus.CANCELLED
    assert cancelled_task.session_id == f"deleted-session:{task_id}"
    assert cancelled_task.auto_reply is False
    assert cancelled_task.reply_status == BackgroundTaskReplyStatus.NONE
    assert cancelled_task.locked_by is None
    assert cancelled_task.lock_until is None
    assert cancelled_task.reply_locked_by is None
    assert cancelled_task.reply_lock_until is None
    assert cancelled_task.result is None
