import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio

import app.core.terminal.watcher as watcher_module
from app.core.terminal import BoundedPtyOutputBuffer, TerminalAction, TerminalReadRequest, TerminalReadResult, TerminalSessionStatus
from app.core.terminal.session_runtime_commands import TerminalSessionRuntimeCommands
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.utils.time import get_local_time
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from app.models.user import User

UID = "terminal-watch-workflow-owner"
SESSION_ID = "terminal-watch-workflow-chat"
PROFILE_ID = 1
TERMINAL_SESSION_ID = "terminal-watch-workflow-session-0123456789"
OUTPUT_CAPACITY = 8
_UNSET = object()


class _BufferDriver:
    def __init__(self) -> None:
        self.buffer = BoundedPtyOutputBuffer(OUTPUT_CAPACITY)
        self.eof = False

    def read_output(self, offset: int, max_bytes: int):
        return self.buffer.read(offset, max_bytes, eof=self.eof)


@pytest_asyncio.fixture
async def terminal_watch_database(database_factory, monkeypatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="terminal-watch-workflow.sqlite3",
    ) as factory:
        async with factory() as db:
            db.add_all(
                [
                    User(uid=UID, username=UID),
                    Profile(id=PROFILE_ID, uid=UID, name="terminal-watch-workflow-profile", configs={}),
                    ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID),
                    TerminalSession(
                        terminal_session_id=TERMINAL_SESSION_ID,
                        uid=UID,
                        session_id=SESSION_ID,
                        original_tool_call_id="terminal-watch-workflow-call",
                        profile_id=PROFILE_ID,
                        command="test-terminal-command",
                        working_directory="temp/terminal-watch-workflow",
                        status=TerminalSessionStatus.RUNNING,
                        allowed_actions=[action.value for action in TerminalAction],
                        output_capacity_bytes=OUTPUT_CAPACITY,
                    ),
                ]
            )
            await db.commit()

        monkeypatch.setattr(watcher_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(watcher_module, "TERMINAL_WATCH_POLL_INTERVAL_SECONDS", 0.005)
        yield factory


async def _sync_session(
    factory,
    driver: _BufferDriver,
    *,
    status: TerminalSessionStatus | None = None,
    exit_code: int | None | object = _UNSET,
    failure_reason: str | None | object = _UNSET,
) -> None:
    bounds = driver.buffer.state()
    async with factory() as db:
        terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
        assert terminal_session is not None
        terminal_session.oldest_output_offset = bounds.oldest_offset
        terminal_session.next_output_offset = bounds.next_offset
        terminal_session.oldest_output_sequence = bounds.oldest_sequence
        terminal_session.next_output_sequence = bounds.next_sequence
        if status is not None:
            terminal_session.status = status
        if exit_code is not _UNSET:
            terminal_session.exit_code = exit_code
        if failure_reason is not _UNSET:
            terminal_session.failure_reason = failure_reason
        await db.commit()


async def _append_output(factory, driver: _BufferDriver, text: str) -> None:
    driver.buffer.append(text.encode())
    await _sync_session(factory, driver)


def _read_adapter(driver: _BufferDriver, before_read=None, after_read=None):
    runtime = TerminalSessionRuntimeCommands()
    runtime.terminal_session_id = TERMINAL_SESSION_ID
    runtime._status = TerminalSessionStatus.RUNNING
    runtime._driver = driver
    read_count = 0

    async def read(*, offset: int, max_bytes: int, **_kwargs) -> TerminalReadResult:
        nonlocal read_count
        if before_read is not None:
            await before_read(read_count)
        request = TerminalReadRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            offset=offset,
            max_bytes=max_bytes,
        )
        command = TerminalControlCommand(
            terminal_session_id=TERMINAL_SESSION_ID,
            request_id=f"terminal-watch-read-{read_count}-{offset}",
            action=TerminalAction.READ,
            payload={
                **request.model_dump(mode="json"),
            },
            payload_hash="0" * 64,
        )
        result = TerminalReadResult.model_validate(await runtime._execute_command(command))
        read_count += 1
        if after_read is not None:
            await after_read(read_count)
        return result

    return read


def _deadline(seconds: float):
    return get_local_time().replace(tzinfo=None) + timedelta(seconds=seconds)


async def _cancel_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_on_timeout", [False, True], ids=["explicit", "automatic"])
@pytest.mark.parametrize(
    ("final_status", "expected_event"),
    [
        (TerminalSessionStatus.EXITED, "process_exit"),
        (TerminalSessionStatus.FAILED, "terminal_error"),
    ],
)
async def test_watch_keeps_observing_existing_gap_until_terminal_state(
    terminal_watch_database,
    monkeypatch,
    read_on_timeout: bool,
    final_status: TerminalSessionStatus,
    expected_event: str,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    await _append_output(factory, driver, "0123456789")
    monkeypatch.setattr(
        watcher_module,
        "_read",
        _read_adapter(driver),
    )
    observation = asyncio.create_task(
        watcher_module.watch_terminal(
            task_id=1,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="process_exit",
                max_bytes=OUTPUT_CAPACITY,
            ),
            deadline=_deadline(2),
            read_on_timeout=read_on_timeout,
        )
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(observation), timeout=0.05)

        driver.eof = True
        await _sync_session(
            factory,
            driver,
            status=final_status,
            exit_code=0 if final_status is TerminalSessionStatus.EXITED else None,
            failure_reason=None if final_status is TerminalSessionStatus.EXITED else "terminal failed",
        )
        result = await asyncio.wait_for(observation, timeout=2)
    finally:
        await _cancel_task(observation)

    assert result["event"] == expected_event
    assert result["output_truncated"] is True
    assert result["event"] != "output_gap"
    assert result["session_status"] == final_status.value
    assert result["eof"] is True
    if final_status is TerminalSessionStatus.EXITED:
        assert result["exit_code"] == 0
    else:
        assert result["exit_code"] is None
        assert result["failure_reason"] == "terminal failed"
    assert result["output"] == "23456789"


@pytest.mark.asyncio
@pytest.mark.parametrize("read_on_timeout", [False, True], ids=["explicit", "automatic"])
async def test_watch_existing_gap_times_out_with_expected_read_policy(
    terminal_watch_database,
    monkeypatch,
    read_on_timeout: bool,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    await _append_output(factory, driver, "0123456789")
    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver))
    observation = asyncio.create_task(
        watcher_module.watch_terminal(
            task_id=2,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="process_exit",
                max_bytes=OUTPUT_CAPACITY,
            ),
            deadline=_deadline(0.05),
            read_on_timeout=read_on_timeout,
        )
    )
    try:
        result = await asyncio.wait_for(observation, timeout=2)
    finally:
        await _cancel_task(observation)

    assert result["event"] == "timeout"
    assert result["output_truncated"] is True
    assert result["event"] != "output_gap"
    assert result["output"] == ("23456789" if read_on_timeout else "")


@pytest.mark.asyncio
async def test_watch_output_contains_keeps_match_across_continuous_pages(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    await _append_output(factory, driver, "AB")
    first_read = asyncio.Event()

    async def after_read(read_count: int) -> None:
        if read_count == 1:
            first_read.set()

    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver, after_read=after_read))
    observation = asyncio.create_task(
        watcher_module.watch_terminal(
            task_id=3,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="output_contains",
                text="ABCD",
                max_bytes=2,
            ),
            deadline=_deadline(2),
        )
    )
    try:
        await asyncio.wait_for(first_read.wait(), timeout=1)
        await _append_output(factory, driver, "CD")
        result = await asyncio.wait_for(observation, timeout=2)
    finally:
        await _cancel_task(observation)

    assert result["event"] == "output_contains"
    assert result["matched_text"] == "ABCD"
    assert result["match_context"] == "AB"
    assert result["output"] == "CD"


@pytest.mark.asyncio
async def test_watch_output_contains_does_not_join_suffix_after_snapshot_gap(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    await _append_output(factory, driver, "AB")
    first_read = asyncio.Event()

    async def after_read(read_count: int) -> None:
        if read_count == 1:
            first_read.set()

    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver, after_read=after_read))
    observation = asyncio.create_task(
        watcher_module.watch_terminal(
            task_id=4,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="output_contains",
                text="ABCD",
                max_bytes=OUTPUT_CAPACITY,
            ),
            deadline=_deadline(0.2),
        )
    )
    try:
        await asyncio.wait_for(first_read.wait(), timeout=1)
        await _append_output(factory, driver, "XCDXXXXXX")
        result = await asyncio.wait_for(observation, timeout=2)
    finally:
        await _cancel_task(observation)

    assert result["event"] == "timeout"
    assert result["output_truncated"] is True
    assert result["event"] != "output_contains"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_event"),
    [
        ("ABCD", "timeout"),
        ("CDXXXXXX", "output_contains"),
    ],
)
async def test_watch_output_contains_handles_eviction_during_read_without_losing_page(
    terminal_watch_database,
    monkeypatch,
    text: str,
    expected_event: str,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    await _append_output(factory, driver, "AB")
    first_read = asyncio.Event()

    async def before_read(read_count: int) -> None:
        if read_count == 1:
            await _append_output(factory, driver, "1234CDXXXXXX")

    async def after_read(read_count: int) -> None:
        if read_count == 1:
            first_read.set()

    monkeypatch.setattr(
        watcher_module,
        "_read",
        _read_adapter(driver, before_read=before_read, after_read=after_read),
    )
    observation = asyncio.create_task(
        watcher_module.watch_terminal(
            task_id=5,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="output_contains",
                text=text,
                max_bytes=OUTPUT_CAPACITY,
            ),
            deadline=_deadline(0.2),
        )
    )
    try:
        await asyncio.wait_for(first_read.wait(), timeout=1)
        await _append_output(factory, driver, "CD")
        result = await asyncio.wait_for(observation, timeout=2)
    finally:
        await _cancel_task(observation)

    assert result["event"] == expected_event
    assert result["output_truncated"] is True
    if expected_event == "output_contains":
        assert result["matched_text"] == text
        assert result["output"] == "CDXXXXXX"
        assert result["start_offset"] == 8
        assert result["next_offset"] == 16
    else:
        assert result["event"] != "output_contains"
