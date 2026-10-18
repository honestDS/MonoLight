import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import WebSocketDisconnect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.utils.file_access as file_access
import app.providers.database as database_provider
from app.api.v1 import chat as chat_api
from app.core.constants import ERR_SESSION_NO_PERMISSION, ERR_SESSION_READ_ONLY
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.session_notifier import SessionNotifier
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.system_setting import SystemSetting
from app.models.user import User

_DISCONNECT = object()


class _Socket:
    query_params = {"lang": "zh"}

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.accepted = asyncio.Event()
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()
        self._waiters: list[tuple[Callable[[dict[str, Any]], bool], asyncio.Future[dict[str, Any]]]] = []

    async def accept(self) -> None:
        self.accepted.set()

    async def receive_json(self) -> dict[str, Any]:
        value = await self._incoming.get()
        if value is _DISCONNECT:
            raise WebSocketDisconnect(code=1000)
        return value

    async def send_json(self, event: dict[str, Any]) -> None:
        self.sent.append(dict(event))
        for predicate, future in list(self._waiters):
            if not future.done() and predicate(event):
                future.set_result(event)
                self._waiters.remove((predicate, future))

    async def feed(self, value: Any) -> None:
        await self._incoming.put(value)

    async def disconnect(self) -> None:
        await self.feed(_DISCONNECT)

    async def wait_for(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        for event in self.sent:
            if predicate(event):
                return event
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiter = (predicate, future)
        self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, timeout=5)
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def wait_for_request(self, request_id: str) -> dict[str, Any]:
        return await self.wait_for(lambda event: event.get("request_id") == request_id)


@dataclass(frozen=True)
class _Runtime:
    factory: async_sessionmaker[AsyncSession]
    notifier: SessionNotifier
    owner_profile_id: int
    other_profile_id: int
    temp_root: Path
    owner_file: Path
    other_file: Path
    admin_file: Path
    outside_file: Path


@pytest_asyncio.fixture
async def websocket_runtime(database_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    tables = [
        PromptLibrary.__table__,
        Profile.__table__,
        User.__table__,
        ChatSession.__table__,
        Message.__table__,
        AuditRecord.__table__,
        AuditConfirmationClaim.__table__,
        SessionReplySequence.__table__,
        SessionReplyWorkItem.__table__,
        SessionReplyStreamEvent.__table__,
        SessionEvent.__table__,
        SystemSetting.__table__,
    ]
    async with database_factory(tables=tables, foreign_keys=True, wal=True, name="chat-ws-permissions.sqlite3") as factory:
        async with factory() as db:
            db.add_all(
                [
                    User(uid="owner", username="owner", is_superuser=False),
                    User(uid="other", username="other", is_superuser=False),
                    User(uid="admin", username="admin", is_superuser=True),
                    Profile(id=1, uid="owner", name="owner-profile", configs={}, is_default=True),
                    Profile(id=2, uid="other", name="other-profile", configs={}, is_default=True),
                ]
            )
            await db.commit()

        monkeypatch.setattr(database_provider, "AsyncSessionLocal", factory)
        monkeypatch.setattr(chat_api, "AsyncSessionLocal", factory)

        async def runtime_settings(_db: AsyncSession) -> SimpleNamespace:
            return SimpleNamespace(log_locale="zh")

        async def skip_external_model_validation(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(chat_api.system_setting_crud, "get_runtime_settings", runtime_settings)
        monkeypatch.setattr(ChatDispatcher, "validate_initial_message_before_save", skip_external_model_validation)

        temp_root = tmp_path / "temp"
        owner_file = temp_root / "temp_owner" / "owner.txt"
        other_file = temp_root / "temp_other" / "other.txt"
        admin_file = temp_root / "temp_admin" / "admin.txt"
        outside_file = tmp_path / "outside.txt"
        for path in (owner_file, other_file, admin_file, outside_file):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(path.name, encoding="utf-8")
        monkeypatch.setattr(file_access, "TEMP_DIR", temp_root)

        notifier = SessionNotifier()
        monkeypatch.setattr(chat_api, "session_notifier", notifier)
        yield _Runtime(
            factory=factory,
            notifier=notifier,
            owner_profile_id=1,
            other_profile_id=2,
            temp_root=temp_root,
            owner_file=owner_file,
            other_file=other_file,
            admin_file=admin_file,
            outside_file=outside_file,
        )


async def _seed_sessions(
    runtime: _Runtime,
    sessions: list[tuple[str, str, str]],
) -> None:
    async with runtime.factory() as db:
        for session_id, uid, source in sessions:
            profile_id = runtime.owner_profile_id if uid == "owner" else runtime.other_profile_id
            db.add(
                ChatSession(
                    session_id=session_id,
                    uid=uid,
                    profile_id=profile_id,
                    profile_override_id=profile_id,
                    source=source,
                    reply_target_source=source,
                )
            )
        await db.commit()


async def _snapshot_session(runtime: _Runtime, session_id: str) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    async with runtime.factory() as db:
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
    return (
        [(item.id, item.uid, item.profile_id, item.role.value, item.content, item.attachments) for item in messages],
        [(item.id, item.uid, item.profile_id, item.status.value, item.sequence_no, item.source_id) for item in works],
    )


async def _open_socket(*, uid: str, is_superuser: bool) -> tuple[_Socket, asyncio.Task]:
    socket = _Socket()
    task = asyncio.create_task(
        chat_api.chat_websocket(
            socket,
            SimpleNamespace(uid=uid, is_superuser=is_superuser),
        )
    )
    await asyncio.wait_for(socket.accepted.wait(), timeout=5)
    return socket, task


async def _close_socket(socket: _Socket, task: asyncio.Task) -> None:
    await socket.disconnect()
    await asyncio.wait_for(task, timeout=5)


def _hold_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    held = asyncio.Event()

    async def wait_for_stream(_work_id: int, **_kwargs: Any):
        await held.wait()
        if False:
            yield {}

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", wait_for_stream)


def _gate_real_stream(monkeypatch: pytest.MonkeyPatch, *, initially_released: bool = False) -> tuple[asyncio.Event, asyncio.Event]:
    started = asyncio.Event()
    release = asyncio.Event()
    if initially_released:
        release.set()
    original_wait_for_stream = session_reply_queue_manager.wait_for_stream

    async def wait_for_stream(work_id: int, *args: Any, **kwargs: Any):
        started.set()
        await release.wait()
        async for event in original_wait_for_stream(work_id, *args, **kwargs):
            yield event

    monkeypatch.setattr(session_reply_queue_manager, "wait_for_stream", wait_for_stream)
    return started, release


async def _persist_stream_outcome(
    runtime: _Runtime,
    work_id: int,
    *,
    status: SessionReplyWorkStatus,
    content: str,
) -> tuple[int, int]:
    async with runtime.factory() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        assert work is not None
        assert work.id is not None
        response_id = f"response-{work_id}"
        is_failure = status == SessionReplyWorkStatus.FAILED
        assistant_message = Message(
            session_id=work.session_id,
            uid=work.uid,
            profile_id=work.profile_id,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content=content,
            is_processed=True,
        )
        db.add(assistant_message)
        await db.flush()
        assert assistant_message.id is not None
        result_message = assistant_message
        if is_failure:
            result_message = Message(
                session_id=work.session_id,
                uid=work.uid,
                profile_id=work.profile_id,
                role=MessageRole.ERR,
                type=MessageType.TEXT,
                content=content,
                is_processed=True,
            )
            db.add(result_message)
            await db.flush()
        assert result_message.id is not None
        stream_events = [
            {
                "type": "content",
                "content": content,
                "response_id": response_id,
                "session_id": work.session_id,
                "work_id": work.id,
                "work_sequence_no": work.sequence_no,
                "event_sequence_no": 1,
            },
            {
                "type": "turn_end",
                "message_id": assistant_message.id,
                "response_id": response_id,
                "session_id": work.session_id,
                "work_id": work.id,
                "work_sequence_no": work.sequence_no,
                "event_sequence_no": 2,
            },
        ]
        db.add_all([SessionReplyStreamEvent(work_id=work.id, sequence_no=sequence_no, event=event) for sequence_no, event in enumerate(stream_events, 1)])
        work.result_message_id = result_message.id
        work.status = status
        work.event_sent = True
        work.execution_state = {
            **(work.execution_state or {}),
            "response": {
                "content": content,
                "history": [],
                "files": None,
                "response_id": response_id,
            },
        }
        if is_failure:
            work.error = content
        db.add(work)
        await db.commit()
        return assistant_message.id, result_message.id


async def _submit_real_work(runtime: _Runtime, session_id: str, *, message: str, request_id: str) -> int:
    async with runtime.factory() as db:
        profile = await db.get(Profile, runtime.owner_profile_id)
        assert profile is not None
        _initial_message, work, _submission_status, _confirmation_events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id=session_id,
            profile=profile,
            message=message,
            attachments=None,
            source="ws",
            request_id=request_id,
        )
        assert work.id is not None
        return work.id


def _permission_error(event: dict[str, Any], *, session_id: str, request_id: str | None = None) -> None:
    assert event["type"] == "error"
    assert event["session_id"] == session_id
    assert event["message"] == t(ERR_SESSION_NO_PERMISSION)
    if request_id is not None:
        assert event["request_id"] == request_id


async def _assert_owner_persistence(
    runtime: _Runtime,
    session_id: str,
    *,
    expected_uid: str = "owner",
) -> None:
    expected_profile_id = runtime.owner_profile_id if expected_uid == "owner" else runtime.other_profile_id
    async with runtime.factory() as db:
        session = await db.get(ChatSession, session_id)
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
    assert session is not None
    assert session.uid == expected_uid
    assert session.profile_id == expected_profile_id
    assert session.profile_override_id == expected_profile_id
    assert messages
    assert works
    assert all(item.uid == expected_uid and item.profile_id == expected_profile_id for item in messages)
    assert all(item.uid == expected_uid and item.profile_id == expected_profile_id for item in works)


@pytest.mark.asyncio
async def test_websocket_rejects_other_owner_without_switching_subscription(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = websocket_runtime
    owner_session_id = "ws-owner-permission"
    other_session_id = "ws-other-permission"
    await _seed_sessions(
        runtime,
        [
            (owner_session_id, "owner", "ws"),
            (other_session_id, "other", "ws"),
        ],
    )
    _hold_stream(monkeypatch)

    owner_socket, owner_task = await _open_socket(uid="owner", is_superuser=False)
    other_socket = other_task = None
    try:
        await owner_socket.feed(
            {
                "message": "owner input",
                "session_id": owner_session_id,
                "request_id": "owner-request",
            }
        )
        accepted = await owner_socket.wait_for_request("owner-request")
        assert accepted["type"] == "input_accepted"
        before_owner = await _snapshot_session(runtime, owner_session_id)
        before_other = await _snapshot_session(runtime, other_session_id)

        await owner_socket.feed(
            {
                "message": "cross-owner input",
                "session_id": other_session_id,
                "request_id": "cross-owner-request",
            }
        )
        error = await owner_socket.wait_for_request("cross-owner-request")
        _permission_error(error, session_id=other_session_id, request_id="cross-owner-request")

        await owner_socket.feed({"type": "resume", "session_id": other_session_id})
        resume_error = await owner_socket.wait_for(lambda event: event.get("type") == "error" and event.get("session_id") == other_session_id and event.get("request_id") is None)
        _permission_error(resume_error, session_id=other_session_id)

        await runtime.notifier._notify_local(
            "owner",
            owner_session_id,
            {"type": "owner-notification", "session_id": owner_session_id},
            event_sequence_no=1,
        )
        owner_notification = await owner_socket.wait_for(lambda event: event.get("type") == "owner-notification")
        assert owner_notification["session_id"] == owner_session_id
        await runtime.notifier._notify_local(
            "other",
            other_session_id,
            {"type": "other-notification", "session_id": other_session_id},
            event_sequence_no=2,
        )
        assert not any(event.get("type") == "other-notification" for event in owner_socket.sent)
        assert await _snapshot_session(runtime, owner_session_id) == before_owner
        assert await _snapshot_session(runtime, other_session_id) == before_other

        other_socket, other_task = await _open_socket(uid="other", is_superuser=False)
        await other_socket.feed(
            {
                "message": "unauthorized input",
                "session_id": owner_session_id,
                "request_id": "other-input-request",
            }
        )
        other_error = await other_socket.wait_for_request("other-input-request")
        _permission_error(other_error, session_id=owner_session_id, request_id="other-input-request")
        await other_socket.feed({"type": "resume", "session_id": owner_session_id})
        other_resume_error = await other_socket.wait_for(lambda event: event.get("type") == "error" and event.get("session_id") == owner_session_id and event.get("request_id") is None)
        _permission_error(other_resume_error, session_id=owner_session_id)
        assert await _snapshot_session(runtime, owner_session_id) == before_owner
    finally:
        if other_socket is not None and other_task is not None:
            await _close_socket(other_socket, other_task)
        await _close_socket(owner_socket, owner_task)

    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
async def test_websocket_processes_input_when_receive_and_notification_complete_together(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = websocket_runtime
    session_id = "ws-receive-notification-race"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])

    original_wait = chat_api.asyncio.wait
    armed = asyncio.Event()
    entered = asyncio.Event()
    first_marker_sent = asyncio.Event()
    injected = False

    async def wait_with_race(tasks, *args: Any, **kwargs: Any):
        nonlocal injected
        task_set = set(tasks)
        if armed.is_set() and first_marker_sent.is_set() and not injected and len(task_set) == 2:
            injected = True
            entered.set()
            await asyncio.gather(*(asyncio.shield(task) for task in task_set))
            return set(task_set), set()
        return await original_wait(tasks, *args, **kwargs)

    monkeypatch.setattr(chat_api.asyncio, "wait", wait_with_race)
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    first_marker = {
        "type": "receive-race-marker-before",
        "session_id": session_id,
        "marker_id": "receive-race-marker-before",
    }
    original_send_json = socket.send_json

    async def send_json(event: dict[str, Any]) -> None:
        if event.get("marker_id") == first_marker["marker_id"]:
            first_marker_sent.set()
        await original_send_json(event)

    socket.send_json = send_json
    try:
        await socket.feed(
            {
                "type": "resume",
                "session_id": session_id,
                "history_message_id": 0,
            }
        )
        await socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_id)
        armed.set()
        await runtime.notifier._notify_local("owner", session_id, first_marker, event_sequence_no=1)
        await first_marker_sent.wait()
        await entered.wait()

        message_text = "input received during notification"
        request_id = "receive-race-request"
        await socket.feed(
            {
                "message": message_text,
                "session_id": session_id,
                "request_id": request_id,
            }
        )
        second_marker = {
            "type": "receive-race-marker-after",
            "session_id": session_id,
            "marker_id": "receive-race-marker-after",
        }
        await runtime.notifier._notify_local("owner", session_id, second_marker, event_sequence_no=2)
        await socket.wait_for(lambda event: event.get("marker_id") == second_marker["marker_id"])
        accepted = await socket.wait_for_request(request_id)
        assert accepted["type"] == "input_accepted"
        assert accepted["session_id"] == session_id
        assert accepted["work_id"] is not None

        async with runtime.factory() as db:
            messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
            works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
        assert len(messages) == 1
        assert messages[0].uid == "owner"
        assert messages[0].role == MessageRole.USER
        assert messages[0].content == message_text
        assert len(works) == 1
        assert works[0].uid == "owner"
        assert works[0].source_id == str(messages[0].id)
        assert works[0].id == accepted["work_id"]
    finally:
        await _close_socket(socket, task)

    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
async def test_superuser_websocket_submission_keeps_owner_and_switches_notifications(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = websocket_runtime
    first_session_id = "ws-admin-owner-a"
    second_session_id = "ws-admin-owner-b"
    await _seed_sessions(
        runtime,
        [
            (first_session_id, "owner", "ws"),
            (second_session_id, "other", "ws"),
        ],
    )
    _hold_stream(monkeypatch)

    socket, task = await _open_socket(uid="admin", is_superuser=True)
    try:
        await socket.feed(
            {
                "message": "admin first input",
                "session_id": first_session_id,
                "request_id": "admin-first-request",
            }
        )
        first_accepted = await socket.wait_for_request("admin-first-request")
        assert first_accepted["type"] == "input_accepted"
        await socket.feed(
            {
                "message": "admin appended input",
                "session_id": first_session_id,
                "request_id": "admin-appended-request",
            }
        )
        appended = await socket.wait_for_request("admin-appended-request")
        assert appended["type"] == "input_accepted"
        assert appended["submission_status"] == "queued"
        await _assert_owner_persistence(runtime, first_session_id)

        await runtime.notifier._notify_local(
            "owner",
            first_session_id,
            {"type": "first-owner-notification", "session_id": first_session_id},
            event_sequence_no=1,
        )
        await socket.wait_for(lambda event: event.get("type") == "first-owner-notification")

        await socket.feed(
            {
                "message": "admin switched input",
                "session_id": second_session_id,
                "request_id": "admin-switched-request",
            }
        )
        switched = await socket.wait_for_request("admin-switched-request")
        assert switched["type"] == "input_accepted"
        await _assert_owner_persistence(runtime, first_session_id)
        await _assert_owner_persistence(runtime, second_session_id, expected_uid="other")

        await runtime.notifier._notify_local(
            "owner",
            first_session_id,
            {"type": "old-owner-notification", "session_id": first_session_id},
            event_sequence_no=2,
        )
        await runtime.notifier._notify_local(
            "other",
            second_session_id,
            {"type": "new-owner-notification", "session_id": second_session_id},
            event_sequence_no=3,
        )
        await socket.wait_for(lambda event: event.get("type") == "new-owner-notification")
        assert not any(event.get("type") == "old-owner-notification" for event in socket.sent)
    finally:
        await _close_socket(socket, task)

    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
async def test_websocket_session_stream_drains_independent_queued_works_in_sequence(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = websocket_runtime
    session_id = "ws-ordered-queued-works"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])
    _stream_started, release_stream = _gate_real_stream(monkeypatch)
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    first_work_id: int
    second_work_id: int
    cancelled_work_id: int
    try:
        await socket.feed(
            {
                "message": "first queued input",
                "session_id": session_id,
                "request_id": "queued-first-request",
            }
        )
        first_accepted = await socket.wait_for_request("queued-first-request")
        first_work_id = first_accepted["work_id"]
        assert isinstance(first_work_id, int)

        await socket.feed(
            {
                "message": "second queued input",
                "session_id": session_id,
                "request_id": "queued-second-request",
            }
        )
        second_accepted = await socket.wait_for_request("queued-second-request")
        second_work_id = second_accepted["work_id"]
        assert isinstance(second_work_id, int)
        assert second_work_id != first_work_id
        assert second_accepted["submission_status"] == "queued"

        await socket.feed(
            {
                "message": "cancelled queued input",
                "session_id": session_id,
                "request_id": "queued-cancelled-request",
            }
        )
        cancelled_accepted = await socket.wait_for_request("queued-cancelled-request")
        cancelled_work_id = cancelled_accepted["work_id"]
        assert isinstance(cancelled_work_id, int)
        assert cancelled_work_id not in {first_work_id, second_work_id}
        assert cancelled_accepted["submission_status"] == "queued"

        first_content = "first persisted content"
        second_content = "second persisted content"
        first_assistant_message_id, first_result_message_id = await _persist_stream_outcome(
            runtime,
            first_work_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            content=first_content,
        )
        second_assistant_message_id, second_result_message_id = await _persist_stream_outcome(
            runtime,
            second_work_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            content=second_content,
        )
        await runtime.notifier._notify_local(
            "owner",
            session_id,
            {
                "type": "proactive_reply",
                "source": "foreground",
                "session_id": session_id,
                "work_id": first_work_id,
                "event_id": f"queued-first-wakeup-{first_work_id}",
                "content": first_content,
            },
            event_sequence_no=1,
        )
        release_stream.set()
        await socket.wait_for(lambda event: event.get("type") == "done" and event.get("work_id") == first_work_id)

        await runtime.notifier._notify_local(
            "owner",
            session_id,
            {
                "type": "proactive_reply",
                "source": "foreground",
                "session_id": session_id,
                "work_id": second_work_id,
                "event_id": f"queued-second-wakeup-{second_work_id}",
                "content": second_content,
            },
            event_sequence_no=2,
        )
        await socket.wait_for(lambda event: event.get("type") == "done" and event.get("work_id") == second_work_id)

        async with runtime.factory() as db:
            cancelled_work = await db.get(SessionReplyWorkItem, cancelled_work_id)
            assert cancelled_work is not None
            cancelled_work.status = SessionReplyWorkStatus.CANCELLED
            cancelled_work.result_message_id = None
            db.add(cancelled_work)
            await db.commit()
        await runtime.notifier._notify_local(
            "owner",
            session_id,
            {
                "type": "proactive_reply",
                "source": "foreground",
                "session_id": session_id,
                "work_id": cancelled_work_id,
                "event_id": f"queued-cancelled-wakeup-{cancelled_work_id}",
            },
            event_sequence_no=3,
        )
        await socket.wait_for(lambda event: event.get("type") == "cancelled" and event.get("work_id") == cancelled_work_id)

        ordered_events = [event for event in socket.sent if event.get("work_id") in {first_work_id, second_work_id, cancelled_work_id} and event.get("type") in {"content", "turn_end", "done", "error", "cancelled"}]
        assert [(event["work_id"], event["type"]) for event in ordered_events] == [
            (first_work_id, "content"),
            (first_work_id, "turn_end"),
            (first_work_id, "done"),
            (second_work_id, "content"),
            (second_work_id, "turn_end"),
            (second_work_id, "done"),
            (cancelled_work_id, "cancelled"),
        ]
        assert ordered_events[0]["content"] == first_content
        assert ordered_events[1]["message_id"] == first_assistant_message_id
        assert ordered_events[2]["message_id"] == first_result_message_id
        assert ordered_events[3]["content"] == second_content
        assert ordered_events[4]["message_id"] == second_assistant_message_id
        assert ordered_events[5]["message_id"] == second_result_message_id
        assert not any(event.get("type") == "proactive_reply" and event.get("work_id") in {first_work_id, second_work_id, cancelled_work_id} for event in socket.sent)
        assert len([event for event in ordered_events if event.get("type") == "done" and event.get("work_id") == first_work_id]) == 1
        assert len([event for event in ordered_events if event.get("type") == "done" and event.get("work_id") == second_work_id]) == 1
        assert len([event for event in ordered_events if event.get("type") == "cancelled" and event.get("work_id") == cancelled_work_id]) == 1
    finally:
        await _close_socket(socket, task)

    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
async def test_websocket_idle_resume_skips_history_and_streams_new_submission_after_resume_complete(
    websocket_runtime: _Runtime,
) -> None:
    runtime = websocket_runtime
    session_id = "ws-ordered-resume-history"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])
    old_work_id = await _submit_real_work(runtime, session_id, message="old input", request_id="old-request")
    old_assistant_message_id, _old_result_message_id = await _persist_stream_outcome(
        runtime,
        old_work_id,
        status=SessionReplyWorkStatus.SUCCEEDED,
        content="old persisted content",
    )
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    try:
        await socket.feed(
            {
                "type": "resume",
                "session_id": session_id,
                "history_message_id": old_assistant_message_id,
            }
        )
        await socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_id)
        assert not any(event.get("work_id") == old_work_id for event in socket.sent)

        await socket.feed(
            {
                "message": "new input after resume",
                "session_id": session_id,
                "request_id": "new-after-resume-request",
            }
        )
        accepted = await socket.wait_for_request("new-after-resume-request")
        new_work_id = accepted["work_id"]
        assert isinstance(new_work_id, int)
        new_content = "new persisted content"
        new_assistant_message_id, new_result_message_id = await _persist_stream_outcome(
            runtime,
            new_work_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            content=new_content,
        )
        await runtime.notifier._notify_local(
            "owner",
            session_id,
            {
                "type": "proactive_reply",
                "source": "foreground",
                "session_id": session_id,
                "work_id": new_work_id,
                "event_id": f"resume-new-wakeup-{new_work_id}",
                "content": new_content,
            },
            event_sequence_no=1,
        )
        await socket.wait_for(lambda event: event.get("type") == "done" and event.get("work_id") == new_work_id)
        new_events = [event for event in socket.sent if event.get("work_id") == new_work_id and event.get("type") in {"content", "turn_end", "done", "error", "cancelled"}]
        assert [event["type"] for event in new_events] == ["content", "turn_end", "done"]
        assert new_events[0]["content"] == new_content
        assert new_events[1]["message_id"] == new_assistant_message_id
        assert new_events[2]["message_id"] == new_result_message_id
        assert not any(event.get("event_id") == f"resume-new-wakeup-{new_work_id}" for event in socket.sent)
    finally:
        await _close_socket(socket, task)

    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
async def test_websocket_idle_subscription_wakes_for_cross_connection_foreground_without_streaming_background_done(
    websocket_runtime: _Runtime,
) -> None:
    runtime = websocket_runtime
    session_id = "ws-ordered-cross-connection"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])
    idle_socket, idle_task = await _open_socket(uid="owner", is_superuser=False)
    writer_socket = writer_task = None
    background_work_id: int
    foreground_work_id: int
    try:
        await idle_socket.feed(
            {
                "type": "resume",
                "session_id": session_id,
                "history_message_id": 0,
            }
        )
        await idle_socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_id)

        async with runtime.factory() as db:
            background_work, created = await session_reply_queue_manager.enqueue_background_summary(
                db,
                uid="owner",
                session_id=session_id,
                profile_id=runtime.owner_profile_id,
                background_task_id=700001,
            )
            assert created is True
            assert background_work.id is not None
            background_work_id = background_work.id
        background_content = "background persisted content"
        await _persist_stream_outcome(
            runtime,
            background_work_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            content=background_content,
        )
        background_notification = {
            "type": "proactive_reply",
            "source": "background_task",
            "session_id": session_id,
            "work_id": background_work_id,
            "task_id": 700001,
            "background_task_id": 700001,
            "event_id": f"background-notification-{background_work_id}",
            "content": background_content,
        }
        await runtime.notifier._notify_local("owner", session_id, background_notification, event_sequence_no=1)
        await idle_socket.wait_for(lambda event: event.get("event_id") == background_notification["event_id"])
        assert not any(event.get("type") == "done" and event.get("work_id") == background_work_id for event in idle_socket.sent)

        writer_socket, writer_task = await _open_socket(uid="owner", is_superuser=False)
        await writer_socket.feed(
            {
                "message": "cross connection foreground input",
                "session_id": session_id,
                "request_id": "cross-connection-foreground-request",
            }
        )
        accepted = await writer_socket.wait_for_request("cross-connection-foreground-request")
        foreground_work_id = accepted["work_id"]
        assert isinstance(foreground_work_id, int)
        await _close_socket(writer_socket, writer_task)
        writer_socket = writer_task = None

        foreground_content = "cross connection foreground persisted content"
        foreground_assistant_message_id, foreground_result_message_id = await _persist_stream_outcome(
            runtime,
            foreground_work_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            content=foreground_content,
        )
        foreground_notification = {
            "type": "proactive_reply",
            "source": "foreground",
            "session_id": session_id,
            "work_id": foreground_work_id,
            "event_id": f"foreground-notification-{foreground_work_id}",
            "content": foreground_content,
        }
        await runtime.notifier._notify_local("owner", session_id, foreground_notification, event_sequence_no=2)
        marker = {
            "type": "cross-connection-marker",
            "source": "background_task",
            "session_id": session_id,
            "background_task_id": 700002,
            "marker_id": f"cross-connection-marker-{foreground_work_id}",
        }
        await runtime.notifier._notify_local("owner", session_id, marker, event_sequence_no=3)
        await idle_socket.wait_for(lambda event: event.get("marker_id") == marker["marker_id"])
        await idle_socket.wait_for(lambda event: event.get("type") == "done" and event.get("work_id") == foreground_work_id)

        foreground_events = [event for event in idle_socket.sent if event.get("work_id") == foreground_work_id and event.get("type") in {"content", "turn_end", "done", "error", "cancelled"}]
        assert [event["type"] for event in foreground_events] == ["content", "turn_end", "done"]
        assert foreground_events[0]["content"] == foreground_content
        assert foreground_events[1]["message_id"] == foreground_assistant_message_id
        assert foreground_events[2]["message_id"] == foreground_result_message_id
        assert len([event for event in foreground_events if event["type"] == "done"]) == 1
        assert len([event for event in idle_socket.sent if event.get("event_id") == background_notification["event_id"]]) == 1
        assert not any(event.get("type") == "done" and event.get("work_id") == background_work_id for event in idle_socket.sent)
        assert not any(event.get("event_id") == foreground_notification["event_id"] for event in idle_socket.sent)
    finally:
        if writer_socket is not None and writer_task is not None:
            await _close_socket(writer_socket, writer_task)
        await _close_socket(idle_socket, idle_task)

    assert runtime.notifier._queues == {}


def _structured_file_message(part_type: str, path: Path) -> list[dict[str, str]]:
    part: dict[str, str] = {"type": part_type, "path": str(path)}
    if part_type == "audio":
        part["format"] = "wav"
    return [part]


@pytest.mark.asyncio
async def test_websocket_file_inputs_use_session_owner_for_initial_and_active_stream_messages(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = websocket_runtime
    session_id = "ws-file-owner-boundary"
    old_session_id = "ws-file-owner-boundary-old"
    old_session_file = runtime.temp_root / f"temp_{old_session_id}" / "old.txt"
    shared_file = runtime.temp_root / "weixin_openclaw" / "shared.txt"
    for path in (old_session_file, shared_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    await _seed_sessions(
        runtime,
        [
            (session_id, "owner", "ws"),
            (old_session_id, "owner", "ws"),
        ],
    )
    async with runtime.factory() as db:
        db.add(
            Message(
                session_id=old_session_id,
                uid="owner",
                profile_id=runtime.owner_profile_id,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="old session attachment",
                attachments=[str(old_session_file), str(shared_file)],
                is_processed=True,
            )
        )
        await db.commit()
    _hold_stream(monkeypatch)

    forbidden_initial = [
        ("initial-other-attachment", {"message": "other", "attachments": [str(runtime.other_file)]}),
        ("initial-outside-attachment", {"message": "outside", "attachments": [str(runtime.outside_file)]}),
        ("initial-admin-attachment", {"message": "admin", "attachments": [str(runtime.admin_file)]}),
        ("initial-old-session-attachment", {"message": "old session", "attachments": [str(old_session_file)]}),
        ("initial-shared-attachment", {"message": "shared", "attachments": [str(shared_file)]}),
        ("initial-other-file", {"message": _structured_file_message("file", runtime.other_file)}),
        ("initial-outside-file", {"message": _structured_file_message("file", runtime.outside_file)}),
        ("initial-admin-file", {"message": _structured_file_message("file", runtime.admin_file)}),
        ("initial-old-session-file", {"message": _structured_file_message("file", old_session_file)}),
        ("initial-shared-file", {"message": _structured_file_message("file", shared_file)}),
        ("initial-other-audio", {"message": _structured_file_message("audio", runtime.other_file)}),
        ("initial-outside-audio", {"message": _structured_file_message("audio", runtime.outside_file)}),
        ("initial-admin-audio", {"message": _structured_file_message("audio", runtime.admin_file)}),
        ("initial-old-session-audio", {"message": _structured_file_message("audio", old_session_file)}),
        ("initial-shared-audio", {"message": _structured_file_message("audio", shared_file)}),
    ]

    socket, task = await _open_socket(uid="admin", is_superuser=True)
    try:
        for request_id, payload in forbidden_initial:
            before = await _snapshot_session(runtime, session_id)
            await socket.feed({**payload, "session_id": session_id, "request_id": request_id})
            error = await socket.wait_for_request(request_id)
            _permission_error(error, session_id=session_id, request_id=request_id)
            assert await _snapshot_session(runtime, session_id) == before

        await socket.feed(
            {
                "message": "owner upload",
                "attachments": [str(runtime.owner_file)],
                "session_id": session_id,
                "request_id": "owner-attachment-request",
            }
        )
        accepted = await socket.wait_for_request("owner-attachment-request")
        assert accepted["type"] == "input_accepted"
        assert accepted["submission_status"] == "accepted"

        forbidden_active = [
            ("active-other-attachment", {"message": "other", "attachments": [str(runtime.other_file)]}),
            ("active-outside-attachment", {"message": "outside", "attachments": [str(runtime.outside_file)]}),
            ("active-admin-attachment", {"message": "admin", "attachments": [str(runtime.admin_file)]}),
            ("active-old-session-attachment", {"message": "old session", "attachments": [str(old_session_file)]}),
            ("active-shared-attachment", {"message": "shared", "attachments": [str(shared_file)]}),
            ("active-other-file", {"message": _structured_file_message("file", runtime.other_file)}),
            ("active-outside-file", {"message": _structured_file_message("file", runtime.outside_file)}),
            ("active-admin-file", {"message": _structured_file_message("file", runtime.admin_file)}),
            ("active-old-session-file", {"message": _structured_file_message("file", old_session_file)}),
            ("active-shared-file", {"message": _structured_file_message("file", shared_file)}),
            ("active-other-audio", {"message": _structured_file_message("audio", runtime.other_file)}),
            ("active-outside-audio", {"message": _structured_file_message("audio", runtime.outside_file)}),
            ("active-admin-audio", {"message": _structured_file_message("audio", runtime.admin_file)}),
            ("active-old-session-audio", {"message": _structured_file_message("audio", old_session_file)}),
            ("active-shared-audio", {"message": _structured_file_message("audio", shared_file)}),
        ]
        for request_id, payload in forbidden_active:
            before = await _snapshot_session(runtime, session_id)
            await socket.feed({**payload, "session_id": session_id, "request_id": request_id})
            error = await socket.wait_for_request(request_id)
            _permission_error(error, session_id=session_id, request_id=request_id)
            assert await _snapshot_session(runtime, session_id) == before

        await socket.feed(
            {
                "message": _structured_file_message("file", runtime.owner_file),
                "session_id": session_id,
                "request_id": "owner-file-request",
            }
        )
        owner_file_accepted = await socket.wait_for_request("owner-file-request")
        assert owner_file_accepted["type"] == "input_accepted"
        await socket.feed(
            {
                "message": _structured_file_message("audio", runtime.owner_file),
                "session_id": session_id,
                "request_id": "owner-audio-request",
            }
        )
        owner_audio_accepted = await socket.wait_for_request("owner-audio-request")
        assert owner_audio_accepted["type"] == "input_accepted"
    finally:
        await _close_socket(socket, task)

    async with runtime.factory() as db:
        messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id).order_by(SessionReplyWorkItem.sequence_no))).scalars().all())
    assert len(messages) == len(works) == 3
    assert all(message.uid == "owner" and message.profile_id == runtime.owner_profile_id for message in messages)
    assert all(work.uid == "owner" and work.profile_id == runtime.owner_profile_id for work in works)
    assert messages[0].attachments == [str(runtime.owner_file)]
    assert json.loads(messages[1].content or "[]")[0]["path"] == str(runtime.owner_file)
    assert json.loads(messages[2].content or "[]")[0]["path"] == str(runtime.owner_file)
    assert runtime.notifier._queues == {}


async def _seed_resume_case(runtime: _Runtime) -> dict[str, int | str]:
    cases = {
        "resume-http": "http",
        "resume-ws": "ws",
        "resume-external": "external",
    }
    result: dict[str, int | str] = {}
    async with runtime.factory() as db:
        for session_id, source in cases.items():
            db.add(
                ChatSession(
                    session_id=session_id,
                    uid="owner",
                    profile_id=runtime.owner_profile_id,
                    profile_override_id=runtime.owner_profile_id,
                    source=source,
                    reply_target_source=source,
                )
            )
            await db.flush()
            user_message = Message(
                session_id=session_id,
                uid="owner",
                profile_id=runtime.owner_profile_id,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=f"persisted input {source}",
                is_processed=True,
            )
            assistant_message = Message(
                session_id=session_id,
                uid="owner",
                profile_id=runtime.owner_profile_id,
                role=MessageRole.ASSISTANT,
                type=MessageType.TEXT,
                content=f"persisted answer {source}",
                is_processed=True,
            )
            db.add_all([user_message, assistant_message])
            await db.flush()
            work = SessionReplyWorkItem(
                uid="owner",
                session_id=session_id,
                profile_id=runtime.owner_profile_id,
                sequence_no=1,
                work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                source_type=SessionReplySourceType.USER_MESSAGE,
                source_id=str(user_message.id),
                dedupe_key=f"{session_id}-work",
                status=SessionReplyWorkStatus.SUCCEEDED,
                result_message_id=assistant_message.id,
                event_sent=True,
                execution_state={
                    "message_source": source,
                    "request_ids": [f"{session_id}-request"],
                    "response": {
                        "content": f"persisted answer {source}",
                        "history": [],
                        "files": None,
                        "response_id": f"{session_id}-response",
                    },
                },
            )
            db.add(work)
            await db.flush()
            db.add(
                SessionReplyStreamEvent(
                    work_id=work.id,
                    sequence_no=1,
                    event={
                        "type": "content",
                        "content": f"persisted chunk {source}",
                        "response_id": f"{session_id}-response",
                        "session_id": session_id,
                        "work_id": work.id,
                    },
                )
            )
            result[session_id] = work.id
        await db.commit()
    return result


async def _resume_socket(
    session_id: str,
    *,
    uid: str,
    is_superuser: bool,
) -> tuple[_Socket, asyncio.Task]:
    socket, task = await _open_socket(uid=uid, is_superuser=is_superuser)
    await socket.feed({"type": "resume", "session_id": session_id, "history_message_id": 0})
    return socket, task


@pytest.mark.asyncio
async def test_websocket_resume_enforces_owner_and_transport_boundaries(
    websocket_runtime: _Runtime,
) -> None:
    runtime = websocket_runtime
    work_ids = await _seed_resume_case(runtime)

    admin_http_socket, admin_http_task = await _resume_socket(
        "resume-http",
        uid="admin",
        is_superuser=True,
    )
    try:
        chunk = await admin_http_socket.wait_for(lambda event: event.get("type") == "content")
        done = await admin_http_socket.wait_for(lambda event: event.get("type") == "done")
        complete = await admin_http_socket.wait_for(lambda event: event.get("type") == "resume_complete")
        assert chunk["content"] == "persisted chunk http"
        assert chunk["session_id"] == "resume-http"
        assert done["response_id"] == "resume-http-response"
        assert done["session_id"] == "resume-http"
        assert done["work_id"] == work_ids["resume-http"]
        assert done["response"]["content"] == "persisted answer http"
        assert complete["session_id"] == "resume-http"
    finally:
        await _close_socket(admin_http_socket, admin_http_task)

    owner_ws_socket, owner_ws_task = await _resume_socket(
        "resume-ws",
        uid="owner",
        is_superuser=False,
    )
    try:
        chunk = await owner_ws_socket.wait_for(lambda event: event.get("type") == "content")
        done = await owner_ws_socket.wait_for(lambda event: event.get("type") == "done")
        complete = await owner_ws_socket.wait_for(lambda event: event.get("type") == "resume_complete")
        assert chunk["content"] == "persisted chunk ws"
        assert done["response_id"] == "resume-ws-response"
        assert done["session_id"] == "resume-ws"
        assert done["work_id"] == work_ids["resume-ws"]
        assert complete["session_id"] == "resume-ws"
    finally:
        await _close_socket(owner_ws_socket, owner_ws_task)

    other_socket, other_task = await _resume_socket(
        "resume-ws",
        uid="other",
        is_superuser=False,
    )
    try:
        error = await other_socket.wait_for(lambda event: event.get("type") == "error" and event.get("session_id") == "resume-ws")
        _permission_error(error, session_id="resume-ws")
    finally:
        await _close_socket(other_socket, other_task)
    assert not any(event.get("type") in {"content", "done", "resume_complete"} for event in other_socket.sent)

    external_socket, external_task = await _resume_socket(
        "resume-external",
        uid="admin",
        is_superuser=True,
    )
    try:
        error = await external_socket.wait_for(lambda event: event.get("type") == "error" and event.get("session_id") == "resume-external")
        assert error["message"] == t(ERR_SESSION_READ_ONLY)
    finally:
        await _close_socket(external_socket, external_task)
    assert not any(event.get("type") in {"content", "done", "resume_complete"} for event in external_socket.sent)

    async with runtime.factory() as db:
        persisted_works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
        persisted_events = list((await db.execute(select(SessionReplyStreamEvent).order_by(SessionReplyStreamEvent.id))).scalars().all())
    assert [work.id for work in persisted_works] == [work_ids["resume-http"], work_ids["resume-ws"], work_ids["resume-external"]]
    assert len(persisted_events) == 3
    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "notification_source", "notification_type", "terminal_type"),
    [
        (SessionReplyWorkStatus.SUCCEEDED, "foreground", "proactive_reply", "done"),
        (SessionReplyWorkStatus.FAILED, "foreground", "proactive_reply_error", "error"),
        (SessionReplyWorkStatus.SUCCEEDED, "confirmed_tool_execution", "proactive_reply", "done"),
        (SessionReplyWorkStatus.FAILED, "confirmed_tool_execution", "proactive_reply_error", "error"),
    ],
)
async def test_websocket_foreground_reply_notifications_only_wake_the_real_ordered_stream(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    status: SessionReplyWorkStatus,
    notification_source: str,
    notification_type: str,
    terminal_type: str,
) -> None:
    runtime = websocket_runtime
    session_id = f"ws-ordered-{status.value}-{notification_source}"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])
    stream_started, release_stream = _gate_real_stream(monkeypatch)
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    work_id: int
    try:
        await socket.feed(
            {
                "message": "ordered input",
                "session_id": session_id,
                "request_id": f"ordered-request-{status.value}-{notification_source}",
            }
        )
        accepted = await socket.wait_for(lambda event: event.get("type") == "input_accepted" and event.get("session_id") == session_id)
        work_id = accepted["work_id"]
        assert isinstance(work_id, int)
        await stream_started.wait()

        notification = {
            "type": notification_type,
            "source": notification_source,
            "session_id": session_id,
            "work_id": work_id,
            "event_id": f"unordered-notification-{work_id}",
            "content": "must stay out of the WebUI",
        }
        await runtime.notifier._notify_local("owner", session_id, notification, event_sequence_no=1)
        marker = {
            "type": "ordered-notifier-marker",
            "source": "background_task",
            "session_id": session_id,
            "background_task_id": 900000 + work_id,
            "marker_id": f"marker-{work_id}",
        }
        await runtime.notifier._notify_local("owner", session_id, marker, event_sequence_no=2)
        await socket.wait_for(lambda event: event.get("marker_id") == marker["marker_id"])
        assert not any(event.get("event_id") == notification["event_id"] for event in socket.sent)

        content = f"persisted {status.value} content"
        assistant_message_id, result_message_id = await _persist_stream_outcome(runtime, work_id, status=status, content=content)
        release_stream.set()

        await socket.wait_for(lambda event: event.get("type") == "content" and event.get("work_id") == work_id)
        await socket.wait_for(lambda event: event.get("type") == "turn_end" and event.get("work_id") == work_id)
        await socket.wait_for(lambda event: event.get("type") == terminal_type and event.get("work_id") == work_id)
        work_events = [event for event in socket.sent if event.get("work_id") == work_id and event.get("type") in {"content", "turn_end", "done", "error", "cancelled"}]
        assert [event["type"] for event in work_events] == ["content", "turn_end", terminal_type]
        assert work_events[0]["content"] == content
        assert work_events[1]["message_id"] == assistant_message_id
        assert work_events[2]["message_id"] == result_message_id
        if terminal_type == "done":
            assert work_events[2]["response"]["content"] == content
        else:
            assert work_events[2]["message"] == content
        assert len([event for event in work_events if event["type"] == terminal_type]) == 1
        assert not any(event.get("event_id") == notification["event_id"] for event in socket.sent)
    finally:
        await _close_socket(socket, task)

    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_kind", "expected_first_events"),
    [
        pytest.param("work_lookup", ("error", "resume_complete"), id="work-query-error"),
        pytest.param("stream_event_read", ("resume_complete",), id="stream-event-read-error"),
    ],
)
async def test_websocket_resume_completes_after_one_crud_read_failure(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    fault_kind: str,
    expected_first_events: tuple[str, ...],
) -> None:
    runtime = websocket_runtime
    session_id = f"ws-resume-crud-failure-{fault_kind}"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])
    work_id = await _submit_real_work(runtime, session_id, message="resume input", request_id=f"{session_id}-request")
    content = "persisted resume recovery content"
    assistant_message_id, result_message_id = await _persist_stream_outcome(
        runtime,
        work_id,
        status=SessionReplyWorkStatus.SUCCEEDED,
        content=content,
    )
    before_snapshot = await _snapshot_session(runtime, session_id)

    if fault_kind == "work_lookup":
        crud = chat_api.session_reply_work_item_crud
        method_name = "get_next_for_session_resume"
        original_method = getattr(crud, method_name)
        injected_exception = SQLAlchemyError("test resume work lookup failure")
    else:
        crud = session_reply_stream_event_crud
        method_name = "list_after_sequence"
        original_method = getattr(crud, method_name)
        injected_exception = RuntimeError("test resume stream event read failure")

    failed = False

    async def fail_once(*args: Any, **kwargs: Any):
        nonlocal failed
        if not failed:
            failed = True
            raise injected_exception
        return await original_method(*args, **kwargs)

    monkeypatch.setattr(crud, method_name, fail_once)
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    try:
        await socket.feed({"type": "resume", "session_id": session_id, "history_message_id": 0})
        await socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_id)

        first_round_end = len(socket.sent)
        first_round_events = [event for event in socket.sent[:first_round_end] if event.get("session_id") == session_id and event.get("type") in {"error", "resume_complete"}]
        assert [event["type"] for event in first_round_events] == list(expected_first_events)
        if fault_kind == "work_lookup":
            assert first_round_events[0]["message"] == t(chat_api.ERR_INTERNAL_SERVER_ERROR)
        assert len([event for event in first_round_events if event["type"] == "resume_complete"]) == 1
        assert await _snapshot_session(runtime, session_id) == before_snapshot

        await socket.feed({"type": "resume", "session_id": session_id, "history_message_id": 0})
        await socket.wait_for(lambda event: event.get("type") == "done" and event.get("work_id") == work_id)
        await socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_id and len([item for item in socket.sent if item.get("type") == "resume_complete" and item.get("session_id") == session_id]) >= 2)

        second_round_events = socket.sent[first_round_end:]
        second_completion_events = [event for event in second_round_events if event.get("session_id") == session_id and event.get("type") in {"error", "resume_complete"}]
        assert [event["type"] for event in second_completion_events] == ["resume_complete"]
        replay_events = [event for event in second_round_events if event.get("work_id") == work_id and event.get("type") in {"content", "turn_end", "done"}]
        assert [event["type"] for event in replay_events] == ["content", "turn_end", "done"]
        assert replay_events[0]["content"] == content
        assert replay_events[1]["message_id"] == assistant_message_id
        assert replay_events[2]["message_id"] == result_message_id
        assert replay_events[2]["response"]["content"] == content
        assert await _snapshot_session(runtime, session_id) == before_snapshot
    finally:
        await _close_socket(socket, task)

    assert len([event for event in socket.sent if event.get("type") == "resume_complete" and event.get("session_id") == session_id]) == 2
    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["read", "error_send"])
@pytest.mark.parametrize("work_outcome", ["streaming", "cancelled"])
async def test_websocket_failed_resume_hands_off_pending_submission_wakeup(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    work_outcome: str,
) -> None:
    runtime = websocket_runtime
    session_id = f"ws-failed-resume-{failure_stage}-{work_outcome}"
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])

    crud = chat_api.session_reply_work_item_crud
    original_get_next = crud.get_next_for_session_resume
    blocked = asyncio.Event()
    release = asyncio.Event()
    failed = False

    async def fail_once(*args: Any, **kwargs: Any):
        nonlocal failed
        if failed:
            return await original_get_next(*args, **kwargs)
        failed = True
        if failure_stage == "read":
            blocked.set()
            await release.wait()
            raise RuntimeError("test resume read failure")
        raise SQLAlchemyError("test resume error send failure")

    monkeypatch.setattr(crud, "get_next_for_session_resume", fail_once)
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    error_send_blocked = False
    if failure_stage == "error_send":
        original_send_json = socket.send_json

        async def send_json(event: dict[str, Any]) -> None:
            nonlocal error_send_blocked
            if not error_send_blocked and event.get("type") == "error" and event.get("session_id") == session_id and "work_id" not in event:
                error_send_blocked = True
                blocked.set()
                await release.wait()
            await original_send_json(event)

        socket.send_json = send_json

    work_id: int
    try:
        await socket.feed({"type": "resume", "session_id": session_id, "history_message_id": 0})
        await asyncio.wait_for(blocked.wait(), timeout=5)

        request_id = f"failed-resume-request-{failure_stage}-{work_outcome}"
        await socket.feed({"message": "pending submission", "session_id": session_id, "request_id": request_id})
        accepted = await socket.wait_for_request(request_id)
        assert accepted["type"] == "input_accepted"
        work_id = accepted["work_id"]
        assert isinstance(work_id, int)

        marker = {
            "type": "failed-resume-marker",
            "session_id": session_id,
            "marker_id": f"failed-resume-marker-{work_id}",
        }
        await runtime.notifier._notify_local("owner", session_id, marker, event_sequence_no=1)
        await socket.wait_for(lambda event: event.get("marker_id") == marker["marker_id"])

        assert not release.is_set()
        content = f"failed resume {work_outcome} content"
        async with runtime.factory() as db:
            work = await db.get(SessionReplyWorkItem, work_id)
            assert work is not None
            assert work.id == work_id
            if work_outcome == "streaming":
                work.status = SessionReplyWorkStatus.RUNNING
                db.add(
                    SessionReplyStreamEvent(
                        work_id=work_id,
                        sequence_no=1,
                        event={
                            "type": "content",
                            "content": content,
                            "session_id": session_id,
                            "work_id": work_id,
                            "response_id": f"failed-resume-response-{work_id}",
                            "work_sequence_no": work.sequence_no,
                            "event_sequence_no": 1,
                        },
                    )
                )
            else:
                work.status = SessionReplyWorkStatus.CANCELLED
                work.result_message_id = None
            db.add(work)
            await db.commit()

        release.set()
        if work_outcome == "streaming":
            content_event = await socket.wait_for(lambda event: event.get("type") == "content" and event.get("work_id") == work_id)
            assert content_event["content"] == content
            assert not any(event.get("session_id") == session_id and event.get("type") == "resume_complete" for event in socket.sent)
            assert not any(event.get("work_id") == work_id and event.get("type") in {"done", "error", "cancelled"} for event in socket.sent)
            async with runtime.factory() as db:
                running_work = await db.get(SessionReplyWorkItem, work_id)
                assert running_work is not None
                assert running_work.status == SessionReplyWorkStatus.RUNNING
            async with runtime.factory() as db:
                cancelled_work = await db.get(SessionReplyWorkItem, work_id)
                assert cancelled_work is not None
                cancelled_work.status = SessionReplyWorkStatus.CANCELLED
                cancelled_work.result_message_id = None
                db.add(cancelled_work)
                await db.commit()

        await socket.wait_for(lambda event: event.get("type") == "cancelled" and event.get("work_id") == work_id)
        await socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_id)
    finally:
        release.set()
        await _close_socket(socket, task)

    work_events = [event for event in socket.sent if event.get("work_id") == work_id and event.get("type") in {"content", "turn_end", "done", "error", "cancelled"}]
    expected_work_events = ["content", "cancelled"] if work_outcome == "streaming" else ["cancelled"]
    assert [event["type"] for event in work_events] == expected_work_events
    cancelled_events = [event for event in work_events if event.get("type") == "cancelled"]
    assert len(cancelled_events) == 1
    resume_complete_events = [event for event in socket.sent if event.get("type") == "resume_complete" and event.get("session_id") == session_id]
    assert len(resume_complete_events) == 1
    assert socket.sent.index(resume_complete_events[0]) > socket.sent.index(cancelled_events[0])
    if failure_stage == "error_send":
        assert len([event for event in socket.sent if event.get("type") == "error" and event.get("session_id") == session_id and "work_id" not in event]) == 1
    assert runtime.notifier._queues == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["switch", "disconnect"])
async def test_websocket_cancelled_failed_resume_does_not_revive_pending_wakeup(
    websocket_runtime: _Runtime,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    runtime = websocket_runtime
    session_a = f"ws-cancelled-failed-resume-a-{action}"
    session_b = f"ws-cancelled-failed-resume-b-{action}"
    await _seed_sessions(runtime, [(session_a, "owner", "ws"), (session_b, "owner", "ws")])

    state = chat_api._WebSocketChatState()
    monkeypatch.setattr(chat_api, "_WebSocketChatState", lambda: state)
    crud = chat_api.session_reply_work_item_crud
    original_get_next = crud.get_next_for_session_resume
    failed = False

    async def fail_once(*args: Any, **kwargs: Any):
        nonlocal failed
        if not failed:
            failed = True
            raise SQLAlchemyError("test failed resume switch cancellation")
        return await original_get_next(*args, **kwargs)

    monkeypatch.setattr(crud, "get_next_for_session_resume", fail_once)
    blocked = asyncio.Event()
    release = asyncio.Event()
    socket, task = await _open_socket(uid="owner", is_superuser=False)
    error_blocked = False
    original_send_json = socket.send_json

    async def send_json(event: dict[str, Any]) -> None:
        nonlocal error_blocked
        if not error_blocked and event.get("type") == "error" and event.get("session_id") == session_a and "work_id" not in event:
            error_blocked = True
            blocked.set()
            await release.wait()
        await original_send_json(event)

    socket.send_json = send_json
    work_id: int
    try:
        await socket.feed({"type": "resume", "session_id": session_a, "history_message_id": 0})
        await asyncio.wait_for(blocked.wait(), timeout=5)

        request_id = f"cancelled-failed-resume-request-{action}"
        await socket.feed({"message": "cancelled pending submission", "session_id": session_a, "request_id": request_id})
        accepted = await socket.wait_for_request(request_id)
        assert accepted["type"] == "input_accepted"
        work_id = accepted["work_id"]
        assert isinstance(work_id, int)

        marker = {
            "type": "cancelled-failed-resume-marker",
            "session_id": session_a,
            "marker_id": f"cancelled-failed-resume-marker-{work_id}",
        }
        await runtime.notifier._notify_local("owner", session_a, marker, event_sequence_no=1)
        await socket.wait_for(lambda event: event.get("marker_id") == marker["marker_id"])

        async with runtime.factory() as db:
            work = await db.get(SessionReplyWorkItem, work_id)
            assert work is not None
            assert work.id == work_id
            assert work.uid == "owner"
            assert work.session_id == session_a
            work.status = SessionReplyWorkStatus.CANCELLED
            work.result_message_id = None
            db.add(work)
            await db.commit()

        if action == "switch":
            await socket.feed({"type": "resume", "session_id": session_b, "history_message_id": 0})
            b_complete = await socket.wait_for(lambda event: event.get("type") == "resume_complete" and event.get("session_id") == session_b)
            assert b_complete["session_id"] == session_b
        else:
            await _close_socket(socket, task)
            assert state.stream_task is None

        assert not any(event.get("work_id") == work_id and event.get("type") in {"content", "done", "cancelled", "error"} for event in socket.sent)
    finally:
        release.set()
        if not task.done():
            await _close_socket(socket, task)
        await chat_api._cancel_websocket_stream_task(state)

    assert runtime.notifier._queues == {}
