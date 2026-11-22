import asyncio
import secrets
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.manager as background_manager_module
import app.core.background_tasks.recovery as recovery_module
import app.core.background_tasks.reply_trigger as reply_trigger_module
import app.core.background_tasks.runner as runner_module
import app.core.background_tasks.scheduler as scheduler_module
import app.core.message_platforms.manager as message_manager_module
import app.core.session_reply_queue.consumer as consumer_module
import app.core.session_reply_queue.executor_audit as executor_audit_module
import app.models  # noqa: F401
import app.workers.background_task as background_worker_module
import app.workers.general as general_module
import app.workers.lease as worker_lease_module
from app.core.constants import STARTUP_CLEANUP_OWNER_KEY
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.worker.lease import worker_lease_crud
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import Notification, NotificationTopic
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.models.worker_lease import WorkerLease
from app.providers.database import ensure_sqlite_outer_transaction
from app.providers.database.client import CancellationSafeAsyncSession
from app.providers.database.time import get_database_timestamp

_WAIT_SECONDS = 5
_POLL_SECONDS = 0.02
_QUIET_SECONDS = 0.2
_UID = "general-notification-user"
_PROFILE_ID = 1
_SESSION_ID = "general-notification-session"
_WORKER_NAMES = ("background_task", "message_platform", "session_reply")


@pytest_asyncio.fixture
async def general_worker_context(database_factory, monkeypatch: pytest.MonkeyPatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
        async with factory() as db:
            profile_config = ProfileConfig().model_dump()
            db.add_all(
                [
                    User(uid=_UID, username="general_notification_user"),
                    Profile(
                        id=_PROFILE_ID,
                        uid=_UID,
                        name="general notification profile",
                        configs=profile_config,
                    ),
                    ChatSession(
                        session_id=_SESSION_ID,
                        uid=_UID,
                        profile_id=_PROFILE_ID,
                    ),
                    SystemSetting(key="agent_work_max_concurrency", value="1"),
                ]
            )
            await db.commit()

        engine = factory.kw["bind"]
        assert engine is not None
        token = secrets.token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        runtime_config: NotificationConfig | None = None
        server_started = False
        context = SimpleNamespace(
            factory=factory,
            engine=engine,
            server=server,
            notification_config=None,
            runtimes=[],
            runtime_calls=[],
            reply_inboxes=[],
            stop_events=[],
            worker_task=None,
            executed=asyncio.Queue(),
        )

        async def _create_test_runtime(
            process_name: str,
            *,
            bind=None,
            session_class: type[Session] = Session,
        ) -> NotificationRuntime:
            context.runtime_calls.append(process_name)
            assert process_name == "general"
            assert runtime_config is not None
            runtime = NotificationRuntime(
                NotificationClient(runtime_config, "general-test"),
                bind=engine.sync_engine,
                session_class=session_class,
            )
            subscribe = runtime.subscribe

            def _tracked_subscribe(topics):
                topic_list = tuple(topics)
                inbox = subscribe(topic_list)
                if NotificationTopic.SESSION_REPLY in topic_list:
                    context.reply_inboxes.append(inbox)
                return inbox

            runtime.subscribe = _tracked_subscribe
            context.runtimes.append(runtime)
            return runtime

        def _capture_shutdown_signal_handlers(stop_event: asyncio.Event) -> None:
            context.stop_events.append(stop_event)

        async def _no_database_bootstrap() -> None:
            return None

        async def _wait_for_worker_shutdown() -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(general_module, "create_database_tables", _no_database_bootstrap)
        monkeypatch.setattr(general_module, "create_notification_runtime", _create_test_runtime)
        monkeypatch.setattr(
            general_module,
            "install_shutdown_signal_handlers",
            _capture_shutdown_signal_handlers,
        )

        for module in (
            worker_lease_module,
            recovery_module,
            background_manager_module,
            runner_module,
            external_work_module,
            reply_trigger_module,
            scheduler_module,
            message_manager_module,
            consumer_module,
            executor_audit_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)

        for cleaner_name in (
            "background_log_cleaner",
            "background_log_file_cleaner",
            "background_temp_cleaner",
            "background_audit_cleaner",
            "background_file_download_token_cleaner",
            "background_context_summary_cleaner",
        ):
            monkeypatch.setattr(
                background_worker_module,
                cleaner_name,
                _wait_for_worker_shutdown,
            )

        async def _controlled_execute_session_reply_work(work_id: int, worker_id: str) -> None:
            async with factory() as db:
                work = await session_reply_work_item_crud.get(db, work_id)
                assert work is not None
                assert work.status == SessionReplyWorkStatus.RUNNING
                assert work.locked_by == worker_id
                assert await session_reply_work_item_crud.mark_terminal(
                    db,
                    work_id=work_id,
                    worker_id=worker_id,
                    status=SessionReplyWorkStatus.SUCCEEDED,
                )
            await context.executed.put(work_id)

        monkeypatch.setattr(
            consumer_module,
            "execute_session_reply_work",
            _controlled_execute_session_reply_work,
        )

        try:
            await asyncio.wait_for(server.start(), timeout=_WAIT_SECONDS)
            server_started = True
            host, port = server.address
            runtime_config = NotificationConfig(host=host, port=port, token=token)
            context.notification_config = runtime_config
            yield context
        finally:
            if context.worker_task is not None and not context.worker_task.done() and context.stop_events:
                context.stop_events[0].set()
            if context.worker_task is not None and not context.worker_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(context.worker_task),
                        timeout=_WAIT_SECONDS,
                    )
                except TimeoutError:
                    context.worker_task.cancel()
            if context.worker_task is not None:
                await asyncio.wait_for(
                    asyncio.gather(context.worker_task, return_exceptions=True),
                    timeout=_WAIT_SECONDS,
                )
            try:
                for runtime in context.runtimes:
                    await asyncio.wait_for(runtime.stop(), timeout=_WAIT_SECONDS)
            finally:
                if server_started:
                    await asyncio.wait_for(server.stop(), timeout=_WAIT_SECONDS)


async def _start_general_worker(context) -> NotificationRuntime:
    context.worker_task = asyncio.create_task(general_module.run_general_worker())
    async with asyncio.timeout(_WAIT_SECONDS):
        while not context.runtimes or not context.stop_events:
            if context.worker_task.done():
                await context.worker_task
                raise AssertionError("general worker stopped before creating its runtime")
            await asyncio.sleep(_POLL_SECONDS)

    assert context.runtime_calls == ["general"]
    assert len(context.runtimes) == 1
    runtime = context.runtimes[0]
    await asyncio.wait_for(runtime.wait_ready(), timeout=_WAIT_SECONDS)
    assert len(context.reply_inboxes) == 1
    return runtime


async def _read_worker_state(context):
    async with context.factory() as db:
        now = await get_database_timestamp(db)
        result = await db.execute(select(WorkerLease).where(WorkerLease.worker_name.in_(_WORKER_NAMES)))
        leases = {lease.worker_name: lease for lease in result.scalars().all()}
        marker_result = await db.execute(select(SystemSetting).where(SystemSetting.key == STARTUP_CLEANUP_OWNER_KEY))
        marker = marker_result.scalars().first()
    return leases, marker, now


async def _wait_for_worker_leases(
    context,
    *,
    expected_session_reply_owner: str | None = None,
    rejected_session_reply_owner: str | None = None,
) -> dict[str, WorkerLease]:
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            leases, marker, now = await _read_worker_state(context)
            ready = set(leases) == set(_WORKER_NAMES) and all(lease.lease_until >= now for lease in leases.values()) and marker is not None and marker.value == leases["background_task"].owner_id
            if ready and expected_session_reply_owner is not None:
                ready = leases["session_reply"].owner_id == expected_session_reply_owner
            if ready and rejected_session_reply_owner is not None:
                ready = leases["session_reply"].owner_id != rejected_session_reply_owner
            if ready:
                return leases
            await asyncio.sleep(_POLL_SECONDS)


async def _wait_for_worker_leases_released(context) -> None:
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            async with context.factory() as db:
                result = await db.execute(select(WorkerLease.worker_name).where(WorkerLease.worker_name.in_(_WORKER_NAMES)))
                if not result.scalars().all():
                    return
            await asyncio.sleep(_POLL_SECONDS)


async def _enqueue_reply_work(context, dedupe_key: str) -> SessionReplyWorkItem:
    async with context.factory() as db:
        await ensure_sqlite_outer_transaction(db)
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=_UID,
            session_id=_SESSION_ID,
            profile_id=_PROFILE_ID,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=dedupe_key,
            dedupe_key=dedupe_key,
        )
    assert created
    assert work.id is not None
    return work


async def _get_work(context, work_id: int) -> SessionReplyWorkItem | None:
    async with context.factory() as db:
        return await session_reply_work_item_crud.get(db, work_id)


async def _wait_for_reply_notification(context, work_id: int) -> Notification:
    inbox = context.reply_inboxes[0]
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    while True:
        batch = inbox.take()
        for notification in batch.notifications:
            resource = notification.resource
            if notification.topic == NotificationTopic.SESSION_REPLY and resource is not None and resource.type == "session_reply_work" and resource.id == str(work_id):
                return notification
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "timed out waiting for the TCP reply notification"
        assert await inbox.wait(remaining), "timed out waiting for the TCP reply notification"


async def _wait_for_worker_stop(context) -> None:
    assert context.worker_task is not None
    assert len(context.stop_events) == 1
    context.stop_events[0].set()
    await asyncio.wait_for(context.worker_task, timeout=_WAIT_SECONDS)
    await _wait_for_worker_leases_released(context)


@pytest.mark.asyncio
async def test_general_worker_delivers_committed_reply_and_releases_all_role_leases(
    general_worker_context,
) -> None:
    context = general_worker_context
    await _start_general_worker(context)
    leases = await _wait_for_worker_leases(context)
    assert len({lease.owner_id for lease in leases.values()}) == 3

    work = await _enqueue_reply_work(context, "general-notification-committed")
    assert await asyncio.wait_for(context.executed.get(), timeout=_WAIT_SECONDS) == work.id
    persisted = await _get_work(context, work.id)
    assert persisted is not None
    assert persisted.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted.attempt_count == 1

    await _wait_for_worker_stop(context)

    assert context.notification_config is not None
    probe = NotificationClient(context.notification_config, "post-shutdown-probe")
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            published = await probe.publish(
                Notification(
                    topic=NotificationTopic.SESSION_REPLY,
                    event="after_shutdown",
                    source="post-shutdown-probe",
                )
            )
            if published.recipient_count == 0:
                break
            await asyncio.sleep(_POLL_SECONDS)
    assert published.recipient_count == 0


@pytest.mark.asyncio
async def test_general_worker_recovers_reply_after_foreign_lease_and_lost_notification(
    general_worker_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = general_worker_context
    monkeypatch.setattr(worker_lease_module, "WORKER_LEASE_ACQUIRE_INTERVAL_SECONDS", 0.05)
    async with context.factory() as db:
        assert await worker_lease_crud.acquire(
            db,
            worker_name="session_reply",
            owner_id="other-owner",
            lease_seconds=300,
        )

    runtime = await _start_general_worker(context)
    blocked_leases = await _wait_for_worker_leases(
        context,
        expected_session_reply_owner="other-owner",
    )
    assert blocked_leases["session_reply"].owner_id == "other-owner"

    # Clear the initial reconciliation wake, then model a non-owner consuming the hint.
    initial_batch = context.reply_inboxes[0].take()
    assert initial_batch.full_scan
    work = await _enqueue_reply_work(context, "general-notification-takeover")
    await asyncio.wait_for(runtime.flush(), timeout=_WAIT_SECONDS)
    notification = await _wait_for_reply_notification(context, work.id)
    assert notification.event == "ready"

    await asyncio.wait_for(asyncio.sleep(_QUIET_SECONDS), timeout=_WAIT_SECONDS)
    waiting = await _get_work(context, work.id)
    assert waiting is not None
    assert waiting.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert waiting.attempt_count == 0
    assert context.executed.empty()

    async with context.factory() as db:
        assert await worker_lease_crud.release(
            db,
            worker_name="session_reply",
            owner_id="other-owner",
        )

    acquired_leases = await _wait_for_worker_leases(
        context,
        rejected_session_reply_owner="other-owner",
    )
    assert len({lease.owner_id for lease in acquired_leases.values()}) == 3
    assert await asyncio.wait_for(context.executed.get(), timeout=_WAIT_SECONDS) == work.id
    persisted = await _get_work(context, work.id)
    assert persisted is not None
    assert persisted.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted.attempt_count == 1

    await _wait_for_worker_stop(context)
