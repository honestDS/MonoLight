from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.terminal.worker_coordinator as worker_coordinator_module
from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.message import message_crud
from app.core.crud.terminal.session import terminal_session_crud
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageContentFormat, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession

OWNER_UID = "terminal-idle-owner"
OTHER_UID = "terminal-idle-other-user"
PROFILE_ID = 1
FIXED_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
IDLE_TIMEOUT = timedelta(hours=24)


@dataclass(frozen=True)
class CaseSpec:
    label: str
    uid: str
    created_at: datetime
    last_message_at: datetime | None
    message_at: datetime


@dataclass(frozen=True)
class SeededCase:
    label: str
    session_id: str
    uid: str
    terminal_session_id: str
    command_id: int
    watch_id: int
    message_id: int


@pytest_asyncio.fixture
async def idle_cleanup_database(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="terminal-idle-cleanup.sqlite3",
    ) as session_factory:
        monkeypatch.setattr(worker_coordinator_module, "AsyncSessionLocal", session_factory)
        yield session_factory


async def _seed_cases(
    session_factory: async_sessionmaker[AsyncSession],
    specs: list[CaseSpec],
) -> dict[str, SeededCase]:
    seeded: dict[str, SeededCase] = {}
    chats: list[ChatSession] = []
    terminals: list[TerminalSession] = []
    commands: list[TerminalControlCommand] = []
    watches: list[BackgroundTask] = []
    messages: list[Message] = []

    for spec in specs:
        session_id = f"terminal-idle-chat-{spec.label.lower()}"
        terminal_session_id = f"terminal-idle-terminal-{spec.label.lower()}-0123456789abcdef"
        chats.append(
            ChatSession(
                session_id=session_id,
                uid=spec.uid,
                profile_id=PROFILE_ID,
                source="http",
                created_at=spec.created_at,
                last_message_at=spec.last_message_at,
            )
        )
        terminals.append(
            TerminalSession(
                terminal_session_id=terminal_session_id,
                uid=spec.uid,
                session_id=session_id,
                original_tool_call_id=f"terminal-idle-call-{spec.label.lower()}",
                profile_id=PROFILE_ID,
                command=f"echo terminal-{spec.label.lower()}",
                working_directory="/tmp/terminal-idle-cleanup",
                status=TerminalSessionStatus.STARTING,
                allowed_actions=[action.value for action in TerminalAction],
                process_identity=None,
                locked_by=None,
                lock_until=None,
                created_at=spec.created_at,
                updated_at=spec.created_at,
            )
        )
        commands.append(
            TerminalControlCommand(
                terminal_session_id=terminal_session_id,
                request_id=f"terminal-idle-request-{spec.label.lower()}",
                action=TerminalAction.WRITE,
                payload={"data": f"{spec.label}\n"},
                payload_hash=sha256(spec.label.encode()).hexdigest(),
                status=TerminalControlCommandStatus.PENDING,
                created_at=spec.created_at,
                updated_at=spec.created_at,
            )
        )
        watches.append(
            BackgroundTask(
                uid=spec.uid,
                session_id=session_id,
                profile_id=PROFILE_ID,
                tool_call_id=f"terminal-idle-watch-call-{spec.label.lower()}",
                tool_name=TERMINAL_WATCH_TOOL_NAME,
                status=BackgroundTaskStatus.PENDING,
                arguments={
                    "terminal_session_id": terminal_session_id,
                    "after_offset": 0,
                    "condition": "process_exit",
                },
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.PENDING,
                extra={
                    "auto_terminal_watch": True,
                    "terminal_session_id": terminal_session_id,
                },
                created_at=spec.created_at,
            )
        )
        messages.append(
            Message(
                session_id=session_id,
                uid=spec.uid,
                profile_id=PROFILE_ID,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content_format=MessageContentFormat.TEXT,
                content=f"seed message {spec.label}",
                is_processed=True,
                created_at=spec.message_at,
            )
        )

    async with session_factory() as db:
        db.add_all(chats)
        await db.flush()
        db.add_all(terminals)
        await db.flush()
        db.add_all([*commands, *watches, *messages])
        await db.commit()

    for spec, terminal, command, watch, message in zip(specs, terminals, commands, watches, messages, strict=True):
        assert command.id is not None
        assert watch.id is not None
        assert message.id is not None
        seeded[spec.label] = SeededCase(
            label=spec.label,
            session_id=terminal.session_id,
            uid=terminal.uid,
            terminal_session_id=terminal.terminal_session_id,
            command_id=command.id,
            watch_id=watch.id,
            message_id=message.id,
        )
    return seeded


async def _resource_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    cases: dict[str, SeededCase],
) -> dict[str, Any]:
    async with session_factory() as db:
        sessions: dict[str, dict[str, Any] | None] = {}
        terminals: dict[str, dict[str, Any] | None] = {}
        commands: dict[str, list[dict[str, Any]]] = {}
        watches: dict[str, dict[str, Any] | None] = {}
        messages: dict[str, dict[str, Any] | None] = {}

        for label, case in cases.items():
            session = await db.get(ChatSession, case.session_id)
            terminal = await db.get(TerminalSession, case.terminal_session_id)
            command_rows = list((await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.terminal_session_id == case.terminal_session_id).order_by(TerminalControlCommand.id))).scalars().all())
            watch = await db.get(BackgroundTask, case.watch_id)
            message = await db.get(Message, case.message_id)
            sessions[label] = session.model_dump() if session is not None else None
            terminals[label] = terminal.model_dump() if terminal is not None else None
            commands[label] = [row.model_dump() for row in command_rows]
            watches[label] = watch.model_dump() if watch is not None else None
            messages[label] = message.model_dump() if message is not None else None

        all_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())

    return {
        "sessions": sessions,
        "terminals": terminals,
        "commands": commands,
        "watches": watches,
        "messages": messages,
        "all_tasks": [task.model_dump() for task in all_tasks],
    }


@pytest.mark.asyncio
async def test_cleanup_inactive_terminal_sessions_uses_message_activity_and_24h_boundary(
    idle_cleanup_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = FIXED_NOW
    monkeypatch.setattr(worker_coordinator_module, "get_local_time", lambda: now)
    old = now - timedelta(hours=25)
    one_hour_ago = now - timedelta(hours=1)
    twenty_three_hours_ago = now - timedelta(hours=23)
    exactly_idle = now - IDLE_TIMEOUT

    cases = await _seed_cases(
        idle_cleanup_database,
        [
            CaseSpec("A", OWNER_UID, old, old, old),
            CaseSpec("B", OWNER_UID, old, one_hour_ago, old),
            CaseSpec("C", OWNER_UID, old, None, old),
            CaseSpec("D", OWNER_UID, twenty_three_hours_ago, None, twenty_three_hours_ago),
            CaseSpec("E", OWNER_UID, exactly_idle, None, exactly_idle),
            CaseSpec("F", OTHER_UID, old, one_hour_ago, one_hour_ago),
        ],
    )
    before = await _resource_snapshot(idle_cleanup_database, cases)
    before_task_ids = [task["id"] for task in before["all_tasks"]]

    coordinator = worker_coordinator_module.TerminalWorkerCoordinator()
    await coordinator._cleanup_inactive_sessions()

    after = await _resource_snapshot(idle_cleanup_database, cases)
    assert [task["id"] for task in after["all_tasks"]] == before_task_ids

    for label in cases:
        assert after["sessions"][label] == before["sessions"][label]
        assert after["messages"][label] == before["messages"][label]

    for label in ("A", "C"):
        assert after["terminals"][label] is None
        assert after["commands"][label] == []
        watch = after["watches"][label]
        assert watch is not None
        assert watch["status"] == BackgroundTaskStatus.CANCELLED
        assert watch["auto_reply"] is False
        assert watch["reply_status"] == BackgroundTaskReplyStatus.NONE
        assert watch["locked_by"] is None
        assert watch["lock_until"] is None
        assert watch["reply_locked_by"] is None
        assert watch["reply_lock_until"] is None
        assert watch["finished_at"] is not None

    for label in ("B", "D", "E", "F"):
        assert after["terminals"][label] == before["terminals"][label]
        assert after["commands"][label] == before["commands"][label]
        assert after["watches"][label] == before["watches"][label]


@pytest.mark.asyncio
async def test_cleanup_rechecks_activity_after_inactive_session_snapshot(
    idle_cleanup_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = FIXED_NOW
    monkeypatch.setattr(worker_coordinator_module, "get_local_time", lambda: now)
    old = now - timedelta(hours=25)
    activity_at = now - timedelta(hours=1)
    cases = await _seed_cases(
        idle_cleanup_database,
        [CaseSpec("race", OWNER_UID, old, None, old)],
    )
    case = cases["race"]
    before = await _resource_snapshot(idle_cleanup_database, cases)
    activity_message_id: int | None = None

    original_session_factory = worker_coordinator_module.AsyncSessionLocal
    session_open_count = 0

    async def persist_activity() -> None:
        nonlocal activity_message_id
        async with idle_cleanup_database() as activity_db:
            activity_message = Message(
                session_id=case.session_id,
                uid=case.uid,
                profile_id=PROFILE_ID,
                role=MessageRole.USER,
                type=MessageType.BACKGROUND_TASK_RESULT,
                content_format=MessageContentFormat.TEXT,
                content="background activity arrived after the idle snapshot",
                is_processed=True,
                created_at=activity_at,
            )
            await message_crud.persist(activity_db, message=activity_message)
            activity_message_id = activity_message.id

    class _SessionContext:
        def __init__(self, context, *, trigger_activity: bool) -> None:
            self._context = context
            self._trigger_activity = trigger_activity

        async def __aenter__(self):
            return await self._context.__aenter__()

        async def __aexit__(self, exc_type, exc_value, traceback):
            result = await self._context.__aexit__(exc_type, exc_value, traceback)
            if self._trigger_activity:
                await persist_activity()
            return result

    def session_factory_with_activity_after_first_use():
        nonlocal session_open_count
        session_open_count += 1
        return _SessionContext(
            original_session_factory(),
            trigger_activity=session_open_count == 1,
        )

    monkeypatch.setattr(
        worker_coordinator_module,
        "AsyncSessionLocal",
        session_factory_with_activity_after_first_use,
    )

    coordinator = worker_coordinator_module.TerminalWorkerCoordinator()
    await coordinator._cleanup_inactive_sessions()

    assert activity_message_id is not None
    after = await _resource_snapshot(idle_cleanup_database, cases)
    assert after["terminals"]["race"] == before["terminals"]["race"]
    assert after["commands"]["race"] == before["commands"]["race"]
    assert after["watches"]["race"] == before["watches"]["race"]
    assert [task["id"] for task in after["all_tasks"]] == [task["id"] for task in before["all_tasks"]]

    async with idle_cleanup_database() as db:
        session = await db.get(ChatSession, case.session_id)
        persisted_activity = await db.get(Message, activity_message_id)
        assert session is not None
        assert persisted_activity is not None
        assert session.last_message_at is not None
        stored_activity_at = session.last_message_at
        if stored_activity_at.tzinfo is None:
            stored_activity_at = stored_activity_at.replace(tzinfo=UTC)
        assert stored_activity_at == activity_at
        assert persisted_activity.type == MessageType.BACKGROUND_TASK_RESULT

        rechecked = await terminal_session_crud.lock_chat_session(
            db,
            uid=case.uid,
            session_id=case.session_id,
            inactive_before=now - IDLE_TIMEOUT,
        )
        assert rechecked is None
