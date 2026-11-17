import json
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.background_tasks.manager import background_task_manager
from app.core.background_tasks.terminal_watch_coordinator import complete_terminal_watch_round
from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.background_task import BackgroundTask
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalSession
from app.models.user import User

UID = "terminal-auto-watch-owner"
SESSION_ID = "terminal-auto-watch-chat"
PROFILE_ID = 1
TERMINAL_SESSION_ID = "terminal-auto-watch-session-0123456789abcdef"
SECOND_TERMINAL_SESSION_ID = "terminal-auto-watch-session-fedcba9876543210"
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
                        process_identity={"pid": 1},
                    ),
                    TerminalSession(
                        terminal_session_id=SECOND_TERMINAL_SESSION_ID,
                        uid=UID,
                        session_id=SESSION_ID,
                        original_tool_call_id="second-terminal-watch-call",
                        profile_id=PROFILE_ID,
                        command="test-terminal-command-second",
                        working_directory="/tmp/terminal-auto-watch-second",
                        status=TerminalSessionStatus.RUNNING,
                        allowed_actions=[action.value for action in TerminalAction],
                        process_identity={"pid": 2},
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


async def _create_claimed_reply_work(
    db: AsyncSession,
    *,
    worker_id: str,
) -> SessionReplyWorkItem:
    sequence_no = await session_reply_work_item_crud.allocate_sequence_no(db, SESSION_ID)
    work = SessionReplyWorkItem(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        sequence_no=sequence_no,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="terminal-auto-watch-origin-message",
        dedupe_key=f"terminal-auto-watch-origin:{worker_id}",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by=worker_id,
        lock_until=2_000_000_000,
        attempt_count=1,
    )
    db.add(work)
    await db.flush()
    assert work.id is not None
    db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work.id, worker_id)
    return work


async def _create_claimed_auto_watch(
    db: AsyncSession,
    *,
    worker_id: str,
    after_offset: int = 0,
) -> tuple[SessionReplyWorkItem, BackgroundTask]:
    work = await _create_claimed_reply_work(db, worker_id=worker_id)
    task = await ensure_terminal_auto_watch(
        db,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        terminal_session_id=TERMINAL_SESSION_ID,
        after_offset=after_offset,
        session_status=TerminalSessionStatus.RUNNING,
    )
    assert task is not None
    assert task.id is not None
    return work, task


async def _complete_auto_watch(
    db: AsyncSession,
    *,
    task: BackgroundTask,
    worker_id: str,
    content: dict,
) -> None:
    assert task.id is not None
    claimed = await background_task_crud.try_claim(
        db,
        task_id=task.id,
        worker_id=worker_id,
        lease_seconds=300,
    )
    assert claimed is not None
    assert await complete_terminal_watch_round(
        db,
        task=task,
        worker_id=worker_id,
        content=content,
    )
    await db.refresh(task)


async def _submit_terminal_watch_subscription(
    db: AsyncSession,
    *,
    tool_call_id: str,
) -> tuple[BackgroundTask, InternalToolCall, InternalMessage, str]:
    profile = await db.get(Profile, PROFILE_ID)
    assert profile is not None
    tool_call = InternalToolCall(
        id=tool_call_id,
        name=TERMINAL_WATCH_TOOL_NAME,
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 0,
            "condition": "output_contains",
            "text": "never-seen",
        },
    )
    assistant = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call])
    task = await background_task_manager.submit(
        db,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        arguments=tool_call.arguments,
        messages=[assistant],
        source="model",
    )
    assert task.id is not None
    queued_payload = {
        "status": "queued",
        "task_id": task.id,
        "terminal_session_id": TERMINAL_SESSION_ID,
        "session_status": TerminalSessionStatus.RUNNING.value,
    }
    queued_body = json.dumps(queued_payload, ensure_ascii=False)
    tool_result = await save_tool_response(
        db,
        SESSION_ID,
        UID,
        PROFILE_ID,
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=queued_body,
        ),
        [],
        [],
    )
    assert tool_result.id is not None
    return task, tool_call, tool_result, queued_body
