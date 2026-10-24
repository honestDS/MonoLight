import asyncio
import copy
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.task.background import background_task_crud
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.terminal_session import TerminalSession
from app.models.user import User

UID = "terminal-auto-watch-owner"
SESSION_ID = "terminal-auto-watch-chat"
PROFILE_ID = 1
TERMINAL_SESSION_ID = "terminal-auto-watch-session-0123456789abcdef"
AFTER_OFFSET = 37


@pytest_asyncio.fixture
async def terminal_auto_watch_database(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="terminal-auto-watch-concurrency.sqlite3",
    ) as session_factory:
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid=UID, username="terminal-auto-watch-owner"),
                    Profile(id=PROFILE_ID, uid=UID, name="terminal-auto-watch-profile", configs={}),
                    ChatSession(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                    ),
                    TerminalSession(
                        terminal_session_id=TERMINAL_SESSION_ID,
                        uid=UID,
                        session_id=SESSION_ID,
                        original_tool_call_id="initial-terminal-watch-call",
                        profile_id=PROFILE_ID,
                        command="test-terminal-command",
                        working_directory="/tmp/terminal-auto-watch",
                        status=TerminalSessionStatus.RUNNING,
                        allowed_actions=[action.value for action in TerminalAction],
                    ),
                ]
            )
            await db.commit()
        yield session_factory


async def _terminal_watch_tasks(db: AsyncSession) -> list[BackgroundTask]:
    result = await db.execute(
        select(BackgroundTask).where(
            BackgroundTask.uid == UID,
            BackgroundTask.session_id == SESSION_ID,
            BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
        )
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_terminal_auto_watch_registration_is_idempotent_and_preserves_manual_watch(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
) -> None:
    async with terminal_auto_watch_database() as db:
        manual_task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="model-terminal-watch-call",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 11,
                "condition": "output_contains",
                "text": "ready",
            },
            auto_reply=False,
            extra={
                "source": "model",
                "request_context": {"turn": 4, "reason": "wait for readiness"},
            },
        )
        manual_task_id = manual_task.id
        assert manual_task_id is not None
        manual_snapshot = copy.deepcopy(manual_task.model_dump())

        active_manual = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
        )
        assert active_manual is not None
        assert active_manual.id == manual_task_id

    async def register_auto_watch() -> None:
        async with terminal_auto_watch_database() as db:
            await ensure_terminal_auto_watch(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=AFTER_OFFSET,
                session_status=TerminalSessionStatus.RUNNING,
            )

    await asyncio.gather(register_auto_watch(), register_auto_watch())

    async with terminal_auto_watch_database() as db:
        await ensure_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=AFTER_OFFSET,
            session_status=TerminalSessionStatus.RUNNING,
        )

        tasks = await _terminal_watch_tasks(db)
        auto_tasks = [task for task in tasks if isinstance(task.extra, dict) and task.extra.get("auto_terminal_watch") is True]
        assert len(tasks) == 2
        assert len(auto_tasks) == 1

        auto_task = auto_tasks[0]
        assert auto_task.uid == UID
        assert auto_task.session_id == SESSION_ID
        assert auto_task.profile_id == PROFILE_ID
        assert auto_task.status == BackgroundTaskStatus.PENDING
        assert auto_task.reply_status == BackgroundTaskReplyStatus.PENDING
        assert auto_task.auto_reply is True
        assert auto_task.arguments == {
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": AFTER_OFFSET,
            "condition": "process_exit",
        }
        assert auto_task.extra.get("terminal_session_id") == TERMINAL_SESSION_ID

        manual_persisted = await db.get(BackgroundTask, manual_task_id)
        assert manual_persisted is not None
        assert manual_persisted.model_dump() == manual_snapshot

        active_task = await background_task_crud.get_active_terminal_watch(
            db,
            UID,
            SESSION_ID,
            TERMINAL_SESSION_ID,
        )
        assert active_task is not None
        assert active_task.id in {manual_task_id, auto_task.id}


@pytest.mark.parametrize(
    ("observed_content", "expected_next_offset"),
    [
        pytest.param(
            {
                "event": "timeout",
                "eof": False,
                "next_offset": 19,
                "observed_at": "2026-10-24T21:40:00",
            },
            19,
            id="timeout-renews",
        ),
        pytest.param(
            {"event": "process_exit", "eof": True, "next_offset": 19},
            None,
            id="process-exit-stops",
        ),
        pytest.param(
            {"event": "timeout", "eof": True, "next_offset": 19},
            None,
            id="eof-stops",
        ),
    ],
)
@pytest.mark.asyncio
async def test_terminal_auto_watch_renews_after_timeout_and_stops_at_eof(
    terminal_auto_watch_database: async_sessionmaker[AsyncSession],
    observed_content: dict,
    expected_next_offset: int | None,
) -> None:
    async with terminal_auto_watch_database() as db:
        task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="terminal-auto-watch:cycle-test",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments={
                "terminal_session_id": TERMINAL_SESSION_ID,
                "after_offset": 0,
                "condition": "process_exit",
            },
            auto_reply=True,
            extra={
                "source": "terminal_auto_watch",
                "auto_terminal_watch": True,
                "terminal_session_id": TERMINAL_SESSION_ID,
            },
        )
        assert task.id is not None

        worker_id = "terminal-auto-watch-cycle-worker"
        claimed_task = await background_task_crud.try_claim(
            db,
            task_id=task.id,
            worker_id=worker_id,
        )
        assert claimed_task is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=task.id,
            worker_id=worker_id,
            result={
                "status": "succeeded",
                "tool_name": TERMINAL_WATCH_TOOL_NAME,
                "content": observed_content,
            },
            auto_reply=True,
        )

        await db.refresh(task)
        task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED
        db.add(task)
        await db.commit()

        next_task = await background_task_crud.create_terminal_auto_watch(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            previous_task_id=task.id,
        )

        if expected_next_offset is None:
            assert next_task is None
            tasks = await _terminal_watch_tasks(db)
            assert len(tasks) == 1
            assert tasks[0].extra.get("next_auto_watch_task_id") is None
            return

        assert next_task is not None
        assert next_task.arguments == {
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": expected_next_offset,
            "condition": "process_exit",
        }
        assert next_task.auto_reply is True
        assert next_task.extra == {
            "source": "terminal_auto_watch",
            "auto_terminal_watch": True,
            "terminal_session_id": TERMINAL_SESSION_ID,
            "observation_started_at": observed_content["observed_at"],
        }

        await db.refresh(task)
        assert task.extra.get("next_auto_watch_task_id") == next_task.id
        tasks = await _terminal_watch_tasks(db)
        assert len(tasks) == 2
