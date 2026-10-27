import asyncio
import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

import app.core.terminal.watcher as watcher_module
import app.core.tools.terminal_watch as terminal_watch_module
from app.core.background_tasks.terminal_watch_coordinator import create_terminal_watch_subscription
from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.task.background import background_task_crud
from app.core.dispatchers.context import DispatchContext
from app.core.terminal import BoundedPtyOutputBuffer, TerminalAction, TerminalReadRequest, TerminalReadResult, TerminalSessionStatus
from app.core.terminal.session_runtime_commands import TerminalSessionRuntimeCommands
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.tools.terminal_watch import TerminalWatchExecutor
from app.core.utils.time import get_local_time
from app.models.profile import Profile, ProfileConfig
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


def _controlled_clock(monkeypatch):
    clock = {"now": datetime(2030, 1, 1)}
    monkeypatch.setattr(watcher_module, "get_local_time", lambda: clock["now"])
    return clock


def _append_action(factory, driver: _BufferDriver, text: str):
    async def action() -> None:
        await _append_output(factory, driver, text)

    return action


def _install_snapshot_script(monkeypatch, clock, script):
    manager = watcher_module.terminal_session_manager
    original_get_snapshot = type(manager).get_snapshot.__get__(manager, type(manager))
    snapshot_calls = []

    async def scripted_get_snapshot(db, terminal_session_id, uid, session_id):
        call_index = len(snapshot_calls)
        if call_index >= len(script):
            raise AssertionError("terminal watch snapshot script exhausted")
        snapshot_calls.append(call_index)
        advance, action = script[call_index]
        clock["now"] += timedelta(seconds=advance)
        if action is not None:
            await action()
        return await original_get_snapshot(db, terminal_session_id, uid, session_id)

    monkeypatch.setattr(watcher_module.terminal_session_manager, "get_snapshot", scripted_get_snapshot)
    return snapshot_calls


async def _create_watch_subscription(factory, *, tool_call_id: str, arguments: dict, extra: dict | None = None) -> int:
    async with factory() as db:
        subscription = await create_terminal_watch_subscription(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id=tool_call_id,
            arguments=arguments,
            extra=extra or {},
        )
    assert subscription is not None
    assert subscription.id is not None
    return subscription.id


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


@pytest.mark.asyncio
async def test_coordinator_watch_extends_deadline_for_continuous_output_until_process_exit(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    clock = _controlled_clock(monkeypatch)

    async def finish() -> None:
        driver.eof = True
        await _sync_session(
            factory,
            driver,
            status=TerminalSessionStatus.EXITED,
            exit_code=0,
            failure_reason=None,
        )

    snapshot_calls = _install_snapshot_script(
        monkeypatch,
        clock,
        [
            (0.6, _append_action(factory, driver, "a")),
            (0.6, _append_action(factory, driver, "b")),
            (0.6, _append_action(factory, driver, "c")),
            (0.6, _append_action(factory, driver, "d")),
            (0.6, finish),
        ],
    )
    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver))
    result = await asyncio.wait_for(
        watcher_module.watch_terminal(
            task_id=10,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="process_exit",
                max_bytes=OUTPUT_CAPACITY,
            ),
            deadline=clock["now"] + timedelta(seconds=1),
            read_on_timeout=True,
            idle_timeout=1,
            subscription_profile_id=PROFILE_ID,
            subscription_states={},
        ),
        timeout=2,
    )

    assert result["event"] == "process_exit"
    assert result["condition"] == "process_exit"
    assert result["output"] == "abcd"
    assert result["output_truncated"] is False
    assert result["session_status"] == TerminalSessionStatus.EXITED.value
    assert result["eof"] is True
    assert result["matched"] is True
    assert result["matched_watch_task_ids"] == []
    assert result["watch_results"] == {}
    assert len(snapshot_calls) == 5


@pytest.mark.asyncio
async def test_coordinator_watch_times_out_after_idle_without_suppressing_root_process_exit(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    clock = _controlled_clock(monkeypatch)
    subscription_id = await _create_watch_subscription(
        factory,
        tool_call_id="terminal-watch-idle-unmatched",
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 0,
            "condition": "output_contains",
            "text": "wrong-keyword",
            "max_bytes": OUTPUT_CAPACITY,
        },
    )
    snapshot_calls = _install_snapshot_script(
        monkeypatch,
        clock,
        [(0.4, None), (0.7, None)],
    )

    result = await watcher_module.watch_terminal(
        task_id=11,
        uid=UID,
        session_id=SESSION_ID,
        request=TerminalWatchRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            condition="output_contains",
            text="root-wrong-keyword",
            max_bytes=OUTPUT_CAPACITY,
        ),
        deadline=clock["now"] + timedelta(seconds=1),
        read_on_timeout=True,
        idle_timeout=1,
        subscription_profile_id=PROFILE_ID,
        subscription_states={},
    )

    assert result["event"] == "timeout"
    assert result["condition"] == "process_exit"
    assert result["output"] == ""
    assert result["session_status"] == TerminalSessionStatus.RUNNING.value
    assert result["eof"] is False
    assert result["matched"] is False
    assert result["matched_watch_task_ids"] == []
    assert result["watch_results"] == {}
    assert set(result["watch_states"]) == {str(subscription_id)}
    assert result["watch_states"][str(subscription_id)]["cursor"] == 0
    assert result["watch_states"][str(subscription_id)]["suffix"] == ""
    assert len(snapshot_calls) == 2


@pytest.mark.asyncio
async def test_coordinator_watch_restores_independent_subscription_suffixes_across_timeout(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    clock = _controlled_clock(monkeypatch)
    first_subscription_id = await _create_watch_subscription(
        factory,
        tool_call_id="terminal-watch-cross-round-first",
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 0,
            "condition": "output_contains",
            "text": "ABCD",
            "max_bytes": 2,
        },
    )
    second_subscription_id = await _create_watch_subscription(
        factory,
        tool_call_id="terminal-watch-cross-round-second",
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 1,
            "condition": "output_contains",
            "text": "ABCD",
            "max_bytes": 2,
        },
    )
    _install_snapshot_script(
        monkeypatch,
        clock,
        [(0.1, _append_action(factory, driver, "AB")), (1.1, None)],
    )
    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver))

    first_result = await asyncio.wait_for(
        watcher_module.watch_terminal(
            task_id=12,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="output_contains",
                text="ABCD",
                max_bytes=2,
            ),
            deadline=clock["now"] + timedelta(seconds=0.5),
            read_on_timeout=False,
            idle_timeout=1,
            subscription_profile_id=PROFILE_ID,
            subscription_states={},
        ),
        timeout=2,
    )

    assert first_result["event"] == "timeout"
    assert first_result["output"] == ""
    assert first_result["matched_watch_task_ids"] == []
    assert first_result["watch_results"] == {}
    assert first_result["watch_states"][str(first_subscription_id)]["cursor"] == 2
    assert first_result["watch_states"][str(first_subscription_id)]["suffix"] == "AB"
    assert first_result["watch_states"][str(second_subscription_id)]["cursor"] == 2
    assert first_result["watch_states"][str(second_subscription_id)]["suffix"] == "B"

    _install_snapshot_script(
        monkeypatch,
        clock,
        [(0.1, _append_action(factory, driver, "CD"))],
    )
    second_result = await asyncio.wait_for(
        watcher_module.watch_terminal(
            task_id=13,
            uid=UID,
            session_id=SESSION_ID,
            request=TerminalWatchRequest(
                terminal_session_id=TERMINAL_SESSION_ID,
                after_offset=0,
                condition="output_contains",
                text="ABCD",
                max_bytes=2,
            ),
            deadline=clock["now"] + timedelta(seconds=2),
            read_on_timeout=False,
            idle_timeout=1,
            subscription_profile_id=PROFILE_ID,
            subscription_states=first_result["watch_states"],
        ),
        timeout=2,
    )

    assert second_result["event"] == "output_contains"
    assert second_result["condition"] == "process_exit"
    assert second_result["output"] == "CD"
    assert second_result["start_offset"] == 2
    assert second_result["next_offset"] == 4
    assert second_result["matched"] is False
    assert second_result["matched_watch_task_ids"] == [first_subscription_id]
    first_watch_result = second_result["watch_results"][str(first_subscription_id)]
    assert first_watch_result["event"] == "output_contains"
    assert first_watch_result["output"] == "CD"
    assert first_watch_result["matched"] is True
    assert first_watch_result["matched_text"] == "ABCD"
    assert first_watch_result["match_context"] == "AB"
    assert set(second_result["watch_states"]) == {str(second_subscription_id)}
    assert second_result["watch_states"][str(second_subscription_id)]["cursor"] == 4
    assert second_result["watch_states"][str(second_subscription_id)]["suffix"] == "BCD"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_status", "expected_event"),
    [
        (TerminalSessionStatus.FAILED, "terminal_error"),
        (TerminalSessionStatus.EXITED, "process_exit"),
    ],
)
async def test_coordinator_watch_preserves_terminal_event_when_final_output_read_times_out(
    terminal_watch_database,
    monkeypatch,
    final_status: TerminalSessionStatus,
    expected_event: str,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    clock = _controlled_clock(monkeypatch)
    subscription_id = await _create_watch_subscription(
        factory,
        tool_call_id=f"terminal-watch-final-read-{final_status.value}",
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 0,
            "condition": "output_contains",
            "text": "never-seen",
            "max_bytes": OUTPUT_CAPACITY,
        },
    )

    async def finish_with_output() -> None:
        await _append_output(factory, driver, "payload")
        driver.eof = True
        await _sync_session(
            factory,
            driver,
            status=final_status,
            exit_code=0 if final_status is TerminalSessionStatus.EXITED else None,
            failure_reason=None if final_status is TerminalSessionStatus.EXITED else "terminal failed",
        )

    read_attempts = []

    async def raise_timeout(read_count: int) -> None:
        read_attempts.append(read_count)
        raise TimeoutError("terminal read timeout")

    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver, before_read=raise_timeout))
    _install_snapshot_script(monkeypatch, clock, [(0.1, finish_with_output)])

    result = await watcher_module.watch_terminal(
        task_id=14,
        uid=UID,
        session_id=SESSION_ID,
        request=TerminalWatchRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            condition="output_contains",
            text="root-never-seen",
            max_bytes=OUTPUT_CAPACITY,
        ),
        deadline=clock["now"] + timedelta(seconds=10),
        read_on_timeout=False,
        idle_timeout=10,
        subscription_profile_id=PROFILE_ID,
        subscription_states={},
    )

    assert result["event"] == expected_event
    assert result["condition"] == "process_exit"
    assert result["session_status"] == final_status.value
    assert result["output"] == ""
    assert result["eof"] is True
    assert result["output_read_error"] is True
    assert result["matched_watch_task_ids"] == []
    assert result["event"] != "observation_error"
    assert result["error"] == "terminal read timeout"
    child_result = result["watch_results"][str(subscription_id)]
    assert child_result["event"] == expected_event
    assert child_result["output"] == ""
    assert child_result["eof"] is True
    assert child_result["output_read_error"] is True
    assert child_result["event"] != "observation_error"
    assert len(read_attempts) == 2
    if final_status is TerminalSessionStatus.EXITED:
        assert result["exit_code"] == 0
        assert result["failure_reason"] is None
    else:
        assert result["exit_code"] is None
        assert result["failure_reason"] == "terminal failed"


@pytest.mark.asyncio
async def test_coordinator_watch_returns_one_observation_error_for_running_read_timeout(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    driver = _BufferDriver()
    clock = _controlled_clock(monkeypatch)
    subscription_id = await _create_watch_subscription(
        factory,
        tool_call_id="terminal-watch-running-read-timeout",
        arguments={
            "terminal_session_id": TERMINAL_SESSION_ID,
            "after_offset": 0,
            "condition": "output_contains",
            "text": "never-seen",
            "max_bytes": OUTPUT_CAPACITY,
        },
    )

    async def append_running_output() -> None:
        await _append_output(factory, driver, "payload")

    read_attempts = []

    async def raise_timeout(read_count: int) -> None:
        read_attempts.append(read_count)
        raise TimeoutError("terminal read timeout")

    monkeypatch.setattr(watcher_module, "_read", _read_adapter(driver, before_read=raise_timeout))
    snapshot_calls = _install_snapshot_script(monkeypatch, clock, [(0.1, append_running_output)])

    result = await watcher_module.watch_terminal(
        task_id=15,
        uid=UID,
        session_id=SESSION_ID,
        request=TerminalWatchRequest(
            terminal_session_id=TERMINAL_SESSION_ID,
            after_offset=0,
            condition="output_contains",
            text="root-never-seen",
            max_bytes=OUTPUT_CAPACITY,
        ),
        deadline=clock["now"] + timedelta(seconds=10),
        read_on_timeout=False,
        idle_timeout=10,
        subscription_profile_id=PROFILE_ID,
        subscription_states={},
    )

    assert result["event"] == "observation_error"
    assert result["condition"] == "process_exit"
    assert result["session_status"] == TerminalSessionStatus.RUNNING.value
    assert result["output"] == ""
    assert result["eof"] is False
    assert result["matched"] is False
    assert result["matched_watch_task_ids"] == []
    assert result["error"] == "terminal read timeout"
    assert result["event"] != "timeout"
    child_result = result["watch_results"][str(subscription_id)]
    assert child_result["event"] == "observation_error"
    assert child_result["eof"] is False
    assert child_result["output"] == ""
    assert child_result.get("output_read_error") is not True
    assert set(result["watch_states"]) == {str(subscription_id)}
    assert result["watch_states"][str(subscription_id)]["cursor"] == 0
    assert result["watch_states"][str(subscription_id)]["suffix"] == ""
    assert read_attempts == [0]
    assert len(snapshot_calls) == 1


@pytest.mark.asyncio
async def test_terminal_watch_executor_starts_timeout_after_aged_task_queue_delay(
    terminal_watch_database,
    monkeypatch,
) -> None:
    factory = terminal_watch_database
    clock = _controlled_clock(monkeypatch)
    monkeypatch.setattr(terminal_watch_module, "get_local_time", lambda: clock["now"])
    _install_snapshot_script(
        monkeypatch,
        clock,
        [(0, None), (7.4, None), (0.1, None)],
    )

    arguments = {
        "terminal_session_id": TERMINAL_SESSION_ID,
        "after_offset": 0,
        "condition": "process_exit",
        "max_bytes": OUTPUT_CAPACITY,
    }
    async with factory() as db:
        task = await background_task_crud.create_task(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile_id=PROFILE_ID,
            tool_call_id="terminal-watch-aged-task",
            tool_name=TERMINAL_WATCH_TOOL_NAME,
            arguments=arguments,
            extra={
                "auto_terminal_watch": True,
                "terminal_session_id": TERMINAL_SESSION_ID,
            },
            commit=False,
        )
        task.created_at = clock["now"] - timedelta(seconds=60)
        await db.commit()

        profile = await db.get(Profile, task.profile_id)
        assert task.id is not None
        assert profile is not None
        executor = TerminalWatchExecutor(project_root=".", uid=task.uid)
        executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 7.5}}))
        executor.set_runtime_context(
            dispatch_context=DispatchContext(
                mode="background",
                source="integration-test",
                uid=task.uid,
                session_id=task.session_id,
                profile=profile,
                db=db,
                tool_call_id=task.tool_call_id,
                task_id=task.id,
            )
        )
        observation_started = clock["now"]
        payload = await executor.execute(**task.arguments)

    result = json.loads(payload)
    assert result["event"] == "timeout"
    assert result["eof"] is False
    assert clock["now"] - observation_started == timedelta(seconds=7.5)
