import asyncio
import secrets
import time
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select

import app.core.session_reply_queue.consumer as consumer_module
import app.core.session_reply_queue.executor_audit as executor_audit_module
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.system.setting import system_setting_crud
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import (
    Notification,
    NotificationResource,
    NotificationScope,
    NotificationTopic,
)
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.system_setting import SystemRuntimeSettings, SystemSetting
from app.models.user import User
from app.providers.database import ensure_sqlite_outer_transaction
from app.providers.database.client import CancellationSafeAsyncSession

UID = "reply-notification-user"
SESSION_IDS = ("s1", "s2", "s3", "s4")
WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class StartedAttempt:
    work_id: int
    attempt_count: int
    worker_id: str
    started_at: float


class ReplyNotificationHarness:
    def __init__(self, factory, runtime, server, inbox, scan_completed: asyncio.Event):
        self.factory = factory
        self.runtime = runtime
        self.server = server
        self.inbox = inbox
        self.scan_completed = scan_completed
        self.consumer = consumer_module.SessionReplyConsumer()
        self.started: asyncio.Queue[StartedAttempt] = asyncio.Queue()
        self.cancelled: asyncio.Queue[int] = asyncio.Queue()
        self.gates: dict[int, asyncio.Event] = {}
        self.fail_once: set[int | str] = set()
        self.execution_counts: dict[int, int] = {}

    def gate(self, work_id: int) -> asyncio.Event:
        return self.gates.setdefault(work_id, asyncio.Event())

    async def execute(self, work_id: int, worker_id: str) -> None:
        async with self.factory() as db:
            work = await session_reply_work_item_crud.get(db, work_id)
            assert work is not None
            assert work.status == SessionReplyWorkStatus.RUNNING
            assert work.locked_by == worker_id
            attempt_count = work.attempt_count
            dedupe_key = work.dedupe_key

        self.execution_counts[work_id] = self.execution_counts.get(work_id, 0) + 1
        await self.started.put(StartedAttempt(work_id, attempt_count, worker_id, time.time()))
        failure_key = work_id if work_id in self.fail_once else dedupe_key
        if failure_key in self.fail_once:
            self.fail_once.remove(failure_key)
            raise RuntimeError("controlled first-attempt failure")

        try:
            await self.gate(work_id).wait()
        except asyncio.CancelledError:
            await self.cancelled.put(work_id)
            raise

        async with self.factory() as db:
            assert await session_reply_work_item_crud.mark_terminal(
                db,
                work_id=work_id,
                worker_id=worker_id,
                status=SessionReplyWorkStatus.SUCCEEDED,
            )

    async def start_consumer(self) -> None:
        self.consumer.start(inbox=self.inbox)
        await asyncio.wait_for(self.scan_completed.wait(), timeout=WAIT_SECONDS)

    async def stop_consumer(self) -> None:
        await asyncio.wait_for(self.consumer.stop(), timeout=WAIT_SECONDS * 2)

    async def close(self) -> None:
        for gate in self.gates.values():
            gate.set()
        try:
            await asyncio.wait_for(self.consumer.stop(), timeout=WAIT_SECONDS * 2)
        finally:
            try:
                await asyncio.wait_for(self.runtime.stop(), timeout=WAIT_SECONDS * 2)
            finally:
                await asyncio.wait_for(self.server.stop(), timeout=WAIT_SECONDS * 2)


@pytest_asyncio.fixture
async def reply_harness(database_factory, monkeypatch: pytest.MonkeyPatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
        async with factory() as db:
            db.add_all(
                [
                    User(uid=UID, username="reply_notification_user"),
                    Profile(
                        id=1,
                        uid=UID,
                        name="reply notification profile",
                        configs=ProfileConfig().model_dump(),
                    ),
                    *[
                        ChatSession(
                            session_id=session_id,
                            uid=UID,
                            profile_id=1,
                            profile_override_id=1,
                        )
                        for session_id in SESSION_IDS
                    ],
                    SystemSetting(key="agent_work_max_concurrency", value="1"),
                ]
            )
            await db.commit()

        monkeypatch.setattr(consumer_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_audit_module, "AsyncSessionLocal", factory)

        scan_completed = asyncio.Event()
        original_get_next_available_at = consumer_module.session_reply_work_item_crud.get_next_available_at

        async def _track_first_scan(db, **kwargs):
            result = await original_get_next_available_at(db, **kwargs)
            if not scan_completed.is_set():
                scan_completed.set()
            return result

        monkeypatch.setattr(
            consumer_module.session_reply_work_item_crud,
            "get_next_available_at",
            _track_first_scan,
        )

        token = secrets.token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        runtime = None
        harness = None
        try:
            await asyncio.wait_for(server.start(), timeout=WAIT_SECONDS)
            host, port = server.address
            config = NotificationConfig(host=host, port=port, token=token)
            runtime = NotificationRuntime(
                NotificationClient(config, "reply-notification-integration"),
                bind=factory.kw["bind"].sync_engine,
            )
            inbox = runtime.subscribe(
                (
                    NotificationTopic.SESSION_REPLY,
                    NotificationTopic.PROFILE_CONFIG,
                    NotificationTopic.SYSTEM_SETTINGS,
                )
            )
            runtime.start()
            await asyncio.wait_for(runtime.wait_ready(), timeout=WAIT_SECONDS)
            harness = ReplyNotificationHarness(
                factory,
                runtime,
                server,
                inbox,
                scan_completed,
            )
            monkeypatch.setattr(
                consumer_module,
                "execute_session_reply_work",
                harness.execute,
            )
            yield harness
        finally:
            if harness is not None:
                await harness.close()
            elif runtime is not None:
                try:
                    await asyncio.wait_for(runtime.stop(), timeout=WAIT_SECONDS * 2)
                finally:
                    await asyncio.wait_for(server.stop(), timeout=WAIT_SECONDS * 2)
            else:
                await asyncio.wait_for(server.stop(), timeout=WAIT_SECONDS * 2)


async def _enqueue_work(
    harness: ReplyNotificationHarness,
    *,
    session_id: str,
    dedupe_key: str,
    source_id: str | int | None = None,
    work_type: SessionReplyWorkType = SessionReplyWorkType.FOREGROUND_REPLY,
    source_type: SessionReplySourceType | None = None,
    initial_status: SessionReplyWorkStatus | None = None,
) -> int:
    if source_type is None and work_type == SessionReplyWorkType.AGENT_WORK:
        source_type = SessionReplySourceType.SCHEDULED_TASK_RUN
    elif source_type is None:
        source_type = SessionReplySourceType.USER_MESSAGE
    async with harness.factory() as db:
        await ensure_sqlite_outer_transaction(db)
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=UID,
            session_id=session_id,
            profile_id=1,
            work_type=work_type,
            source_type=source_type,
            source_id=dedupe_key if source_id is None else source_id,
            dedupe_key=dedupe_key,
            commit=False,
        )
        assert created
        if initial_status is not None:
            work.status = initial_status
            db.add(work)
        await db.commit()
    assert work.id is not None
    return work.id


async def _read_work(factory, work_id: int) -> SessionReplyWorkItem | None:
    async with factory() as db:
        return await db.get(SessionReplyWorkItem, work_id)


async def _wait_for_work(
    harness: ReplyNotificationHarness,
    work_id: int,
    *,
    status: SessionReplyWorkStatus | None = None,
    attempts: int | None = None,
) -> SessionReplyWorkItem:
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            work = await _read_work(harness.factory, work_id)
            if work is not None and (status is None or work.status == status) and (attempts is None or work.attempt_count == attempts):
                return work
            await asyncio.sleep(0.01)


async def _next_started(
    harness: ReplyNotificationHarness,
    *,
    work_id: int,
) -> StartedAttempt:
    async with asyncio.timeout(WAIT_SECONDS):
        attempt = await harness.started.get()
    assert attempt.work_id == work_id
    return attempt


async def _assert_no_started(
    harness: ReplyNotificationHarness,
    *,
    timeout: float = 0.15,
) -> None:
    try:
        async with asyncio.timeout(timeout):
            attempt = await harness.started.get()
    except TimeoutError:
        return
    pytest.fail(f"unexpected execution started: {attempt}")


async def _flush_notifications(harness: ReplyNotificationHarness) -> None:
    await asyncio.wait_for(harness.runtime.flush(), timeout=WAIT_SECONDS * 2)


async def _update_profile_scheduled_limit(
    harness: ReplyNotificationHarness,
    limit: int,
) -> None:
    async with harness.factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        config = ProfileConfig.model_validate(profile.configs)
        config.tool.scheduled_task_max_concurrency = limit
        await profile_crud.update(
            db,
            db_obj=profile,
            obj_in={"configs": config.model_dump()},
        )


async def _update_global_agent_work_limit(
    harness: ReplyNotificationHarness,
    limit: int,
) -> None:
    async with harness.factory() as db:
        await system_setting_crud.update_runtime_settings(
            db,
            SystemRuntimeSettings(agent_work_max_concurrency=limit),
        )


async def _count_running_work(factory) -> int:
    async with factory() as db:
        result = await db.execute(select(func.count()).select_from(SessionReplyWorkItem).where(SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING))
        return int(result.scalar_one())


@pytest.mark.asyncio
async def test_committed_foreground_work_runs_once_and_duplicate_tcp_hints_are_idempotent(
    reply_harness: ReplyNotificationHarness,
):
    harness = reply_harness
    await harness.start_consumer()
    work_id = await _enqueue_work(harness, session_id="s1", source_id=1, dedupe_key="reply-notification:foreground:1")
    gate = harness.gate(work_id)

    first = await _next_started(harness, work_id=work_id)
    running = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.RUNNING, attempts=1)
    assert first.attempt_count == 1
    assert running.locked_by == first.worker_id

    duplicate = Notification(
        topic=NotificationTopic.SESSION_REPLY,
        event="ready",
        source="reply-notification-test",
        scope=NotificationScope(uid=UID, session_id="s1", profile_id=1),
        resource=NotificationResource(type="session_reply_work", id=str(work_id)),
    )
    for _ in range(2):
        published = await harness.runtime.client.publish(duplicate)
        assert published.recipient_count == 1
    await _assert_no_started(harness)
    assert harness.execution_counts[work_id] == 1

    gate.set()
    terminal = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)
    assert terminal.locked_by is None
    await _assert_no_started(harness)
    assert harness.execution_counts[work_id] == 1


@pytest.mark.asyncio
async def test_enqueue_notification_waits_for_outer_commit_and_rollback_is_silent(
    reply_harness: ReplyNotificationHarness,
):
    harness = reply_harness
    await harness.start_consumer()
    async with harness.factory() as db:
        await ensure_sqlite_outer_transaction(db)
        rolled_back, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=UID,
            session_id="s1",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=2,
            dedupe_key="reply-notification:rollback:2",
            commit=False,
        )
        assert created
        assert rolled_back.id is not None
        rolled_back_id = rolled_back.id
        await db.rollback()

    assert await _read_work(harness.factory, rolled_back_id) is None
    await _assert_no_started(harness)

    committed_id = await _enqueue_work(harness, session_id="s2", source_id=3, dedupe_key="reply-notification:commit-after-rollback:3")
    started = await _next_started(harness, work_id=committed_id)
    assert started.attempt_count == 1
    gate = harness.gate(committed_id)
    gate.set()
    completed = await _wait_for_work(harness, committed_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)
    assert completed.status == SessionReplyWorkStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_global_independent_work_limit_releases_capacity_without_polling(
    reply_harness: ReplyNotificationHarness,
):
    harness = reply_harness
    await harness.start_consumer()
    first_id = await _enqueue_work(harness, session_id="s1", source_id=10, dedupe_key="reply-notification:global-limit:first", work_type=SessionReplyWorkType.AGENT_WORK)
    first_gate = harness.gate(first_id)
    first_attempt = await _next_started(harness, work_id=first_id)

    second_id = await _enqueue_work(harness, session_id="s2", source_id=11, dedupe_key="reply-notification:global-limit:second", work_type=SessionReplyWorkType.AGENT_WORK)
    second_gate = harness.gate(second_id)
    await _flush_notifications(harness)
    pending = await _wait_for_work(harness, second_id, status=SessionReplyWorkStatus.READY_FOR_LLM, attempts=0)
    assert first_attempt.attempt_count == 1
    assert await _count_running_work(harness.factory) == 1
    assert pending.locked_by is None
    await _assert_no_started(harness)

    first_gate.set()
    second_attempt = await _next_started(harness, work_id=second_id)
    assert second_attempt.attempt_count == 1
    first_done = await _wait_for_work(harness, first_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)
    second_running = await _wait_for_work(harness, second_id, status=SessionReplyWorkStatus.RUNNING, attempts=1)
    assert first_done.status == SessionReplyWorkStatus.SUCCEEDED
    assert second_running.locked_by == second_attempt.worker_id
    assert await _count_running_work(harness.factory) == 1

    second_gate.set()
    await _wait_for_work(harness, second_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_limit", ("global", "profile"))
async def test_capacity_increase_notification_dispatches_waiting_agent_work(
    reply_harness: ReplyNotificationHarness,
    changed_limit: str,
):
    harness = reply_harness
    if changed_limit == "profile":
        await _update_global_agent_work_limit(harness, 2)
        await _update_profile_scheduled_limit(harness, 1)
    else:
        await _update_profile_scheduled_limit(harness, 2)
    await _flush_notifications(harness)
    await harness.start_consumer()

    first_id = await _enqueue_work(harness, session_id="s1", source_id=20, dedupe_key=f"reply-notification:{changed_limit}-capacity:first", work_type=SessionReplyWorkType.AGENT_WORK)
    first_gate = harness.gate(first_id)
    first_attempt = await _next_started(harness, work_id=first_id)
    assert first_attempt.attempt_count == 1

    second_id = await _enqueue_work(harness, session_id="s2", source_id=21, dedupe_key=f"reply-notification:{changed_limit}-capacity:second", work_type=SessionReplyWorkType.AGENT_WORK)
    second_gate = harness.gate(second_id)
    await _flush_notifications(harness)
    pending = await _wait_for_work(harness, second_id, status=SessionReplyWorkStatus.READY_FOR_LLM, attempts=0)
    assert pending.locked_by is None
    assert await _count_running_work(harness.factory) == 1
    await _assert_no_started(harness)

    if changed_limit == "global":
        await _update_global_agent_work_limit(harness, 2)
    else:
        await _update_profile_scheduled_limit(harness, 2)
    await _flush_notifications(harness)

    second_attempt = await _next_started(harness, work_id=second_id)
    assert second_attempt.attempt_count == 1
    first_still_running = await _wait_for_work(harness, first_id, status=SessionReplyWorkStatus.RUNNING, attempts=1)
    second_running = await _wait_for_work(harness, second_id, status=SessionReplyWorkStatus.RUNNING, attempts=1)
    assert first_still_running.locked_by == first_attempt.worker_id
    assert second_running.locked_by == second_attempt.worker_id
    assert await _count_running_work(harness.factory) == 2

    first_gate.set()
    second_gate.set()
    await _wait_for_work(harness, first_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)
    await _wait_for_work(harness, second_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)


@pytest.mark.asyncio
async def test_retry_waits_until_available_at_then_runs_second_attempt(
    reply_harness: ReplyNotificationHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = reply_harness
    monkeypatch.setattr(consumer_module, "retry_delay_seconds", lambda _attempt: 1)
    await harness.start_consumer()
    harness.fail_once.add("reply-notification:retry:30")
    work_id = await _enqueue_work(harness, session_id="s1", source_id=30, dedupe_key="reply-notification:retry:30")
    harness.gate(work_id)

    first_attempt = await _next_started(harness, work_id=work_id)
    assert first_attempt.attempt_count == 1
    released = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.READY_FOR_LLM, attempts=1)
    assert released.available_at > int(time.time())

    second_attempt = await _next_started(harness, work_id=work_id)
    assert second_attempt.attempt_count == 2
    assert second_attempt.started_at >= released.available_at
    running = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.RUNNING, attempts=2)
    assert running.locked_by == second_attempt.worker_id

    harness.gate(work_id).set()
    succeeded = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=2)
    assert succeeded.status == SessionReplyWorkStatus.SUCCEEDED
    assert harness.execution_counts[work_id] == 2
    await _assert_no_started(harness)


@pytest.mark.asyncio
async def test_periodic_recovery_finds_work_inserted_without_a_notification(
    reply_harness: ReplyNotificationHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    harness = reply_harness
    monkeypatch.setattr(consumer_module, "SESSION_REPLY_RECOVERY_INTERVAL_SECONDS", 0.1)
    await harness.start_consumer()

    work_id = None
    async with harness.factory() as db:
        await ensure_sqlite_outer_transaction(db)
        db.add(
            SessionReplySequence(
                session_id="s4",
                next_sequence_no=2,
            )
        )
        work = SessionReplyWorkItem(
            uid=UID,
            session_id="s4",
            profile_id=1,
            sequence_no=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="raw-recovery-work-40",
            dedupe_key="reply-notification:raw-recovery:40",
            status=SessionReplyWorkStatus.READY_FOR_LLM,
            execution_state={"work_category": "foreground"},
            available_at=int(time.time()),
        )
        db.add(work)
        await db.commit()
        work_id = work.id
    assert work_id is not None

    started = await _next_started(harness, work_id=work_id)
    assert started.attempt_count == 1
    running = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.RUNNING, attempts=1)
    assert running.locked_by == started.worker_id
    harness.gate(work_id).set()
    await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)


@pytest.mark.asyncio
async def test_cancel_session_commit_cancels_running_executor_without_retry(
    reply_harness: ReplyNotificationHarness,
):
    harness = reply_harness
    await harness.start_consumer()
    work_id = await _enqueue_work(harness, session_id="s1", source_id=50, dedupe_key="reply-notification:cancel:50")
    harness.gate(work_id)
    started = await _next_started(harness, work_id=work_id)

    async with harness.factory() as db:
        cancelled_count = await session_reply_work_item_crud.cancel_session(db, session_id="s1", uid=UID)
    assert cancelled_count == 1
    async with asyncio.timeout(WAIT_SECONDS):
        cancelled_id = await harness.cancelled.get()
    assert cancelled_id == work_id

    cancelled = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.CANCELLED, attempts=1)
    assert cancelled.locked_by is None
    assert started.attempt_count == 1
    await _assert_no_started(harness)
    assert harness.execution_counts[work_id] == 1


@pytest.mark.asyncio
async def test_external_work_wake_commit_notifies_consumer_to_resume(
    reply_harness: ReplyNotificationHarness,
):
    harness = reply_harness
    work_id = await _enqueue_work(harness, session_id="s3", source_id=60, dedupe_key="reply-notification:external-wake:60", initial_status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK)
    await harness.start_consumer()
    waiting = await _read_work(harness.factory, work_id)
    assert waiting is not None
    assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    await _assert_no_started(harness)

    async with harness.factory() as db:
        await ensure_sqlite_outer_transaction(db)
        waiting = await db.get(SessionReplyWorkItem, work_id)
        assert waiting is not None
        woke = await external_work_crud.wake_waiting(db, work=waiting, execution_state={"external_resume_pending": True}, commit=False)
        assert woke
        await db.commit()

    started = await _next_started(harness, work_id=work_id)
    assert started.attempt_count == 1
    running = await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.RUNNING, attempts=1)
    assert running.locked_by == started.worker_id
    harness.gate(work_id).set()
    await _wait_for_work(harness, work_id, status=SessionReplyWorkStatus.SUCCEEDED, attempts=1)


@pytest.mark.asyncio
async def test_idle_consumer_does_not_poll_session_reply_work_table(
    reply_harness: ReplyNotificationHarness,
):
    harness = reply_harness
    statements: list[str] = []
    sync_engine = harness.factory.kw["bind"].sync_engine

    def _record_queue_statement(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.lstrip().upper()
        if "SESSION_REPLY_WORK_ITEM" in normalized and normalized.startswith(("SELECT", "UPDATE")):
            statements.append(normalized)

    event.listen(sync_engine, "after_cursor_execute", _record_queue_statement)
    try:
        await harness.start_consumer()
        baseline = len(statements)
        assert baseline > 0
        async with asyncio.timeout(2.0):
            await asyncio.sleep(1.2)
        assert len(statements) == baseline
        await _assert_no_started(harness, timeout=0.1)
    finally:
        event.remove(sync_engine, "after_cursor_execute", _record_queue_statement)
