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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.utils.file_access as file_access
import app.providers.database as database_provider
from app.api.v1 import chat as chat_api
from app.core.constants import ERR_SESSION_NO_PERMISSION, ERR_SESSION_READ_ONLY
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
    await _seed_sessions(runtime, [(session_id, "owner", "ws")])
    _hold_stream(monkeypatch)

    forbidden_initial = [
        ("initial-other-attachment", {"message": "other", "attachments": [str(runtime.other_file)]}),
        ("initial-outside-attachment", {"message": "outside", "attachments": [str(runtime.outside_file)]}),
        ("initial-admin-attachment", {"message": "admin", "attachments": [str(runtime.admin_file)]}),
        ("initial-other-file", {"message": _structured_file_message("file", runtime.other_file)}),
        ("initial-outside-file", {"message": _structured_file_message("file", runtime.outside_file)}),
        ("initial-admin-file", {"message": _structured_file_message("file", runtime.admin_file)}),
        ("initial-other-audio", {"message": _structured_file_message("audio", runtime.other_file)}),
        ("initial-outside-audio", {"message": _structured_file_message("audio", runtime.outside_file)}),
        ("initial-admin-audio", {"message": _structured_file_message("audio", runtime.admin_file)}),
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
            ("active-other-file", {"message": _structured_file_message("file", runtime.other_file)}),
            ("active-outside-file", {"message": _structured_file_message("file", runtime.outside_file)}),
            ("active-admin-file", {"message": _structured_file_message("file", runtime.admin_file)}),
            ("active-other-audio", {"message": _structured_file_message("audio", runtime.other_file)}),
            ("active-outside-audio", {"message": _structured_file_message("audio", runtime.outside_file)}),
            ("active-admin-audio", {"message": _structured_file_message("audio", runtime.admin_file)}),
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
