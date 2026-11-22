import asyncio
import secrets
from types import SimpleNamespace

import httpx
import pytest

import app.handler as handler_module
import app.models  # noqa: F401
import main
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import Notification, NotificationTopic, ReadyFrame
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.core.security import get_current_user
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db
from app.providers.database.client import CancellationSafeAsyncSession

_WAIT_SECONDS = 5
_UID = "web-notification-user"
_SESSION_ID = "web-notification-session"


@pytest.mark.asyncio
async def test_web_scheduled_task_api_notifies_over_tcp_after_lifespan_startup(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
        async with factory() as db:
            db.add(User(uid=_UID, username=_UID))
            await db.flush()
            db.add(
                Profile(
                    id=1,
                    uid=_UID,
                    name="web notification profile",
                    configs=ProfileConfig().model_dump(),
                    is_default=True,
                )
            )
            await db.flush()
            db.add(ChatSession(session_id=_SESSION_ID, uid=_UID, profile_id=1))
            await db.commit()

        engine = factory.kw["bind"]
        assert engine is not None
        monkeypatch.setattr(main, "AsyncSessionLocal", factory)
        monkeypatch.setattr(handler_module, "AsyncSessionLocal", factory)

        async def _no_database_migrations(*_args, **_kwargs) -> None:
            return None

        async def _no_lifecycle_work(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(main, "init_database_schema", _no_database_migrations)
        monkeypatch.setattr(main.session_notifier, "start", _no_lifecycle_work)
        monkeypatch.setattr(main.session_notifier, "stop", _no_lifecycle_work)
        monkeypatch.setattr(main.log_broadcaster, "start", _no_lifecycle_work)
        monkeypatch.setattr(main.log_broadcaster, "stop", _no_lifecycle_work)
        monkeypatch.setattr(main.LogManager, "setup", lambda **_kwargs: None)
        monkeypatch.setattr(
            main,
            "get_logger",
            lambda _name: SimpleNamespace(info=lambda *_args, **_kwargs: None),
        )

        token = secrets.token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        await asyncio.wait_for(server.start(), timeout=_WAIT_SECONDS)
        host, port = server.address
        config = NotificationConfig(host=host, port=port, token=token)
        listener = NotificationClient(config, "general-probe").listen((NotificationTopic.SCHEDULED_TASK,))
        runtimes: list[NotificationRuntime] = []

        async def _create_web_runtime(
            process_name: str,
            *,
            bind=None,
            session_class=None,
        ) -> NotificationRuntime:
            assert process_name == "web"
            assert bind is not None
            runtime = NotificationRuntime(
                NotificationClient(config, "web-test"),
                bind=engine.sync_engine,
            )
            runtimes.append(runtime)
            return runtime

        monkeypatch.setattr(main, "create_notification_runtime", _create_web_runtime)

        async def _next_notification(timeout: float = _WAIT_SECONDS) -> Notification:
            async with asyncio.timeout(timeout):
                while True:
                    frame = await anext(listener)
                    if isinstance(frame, Notification):
                        return frame

        try:
            async with asyncio.timeout(_WAIT_SECONDS):
                ready = await anext(listener)
            assert isinstance(ready, ReadyFrame)

            async def _override_get_db():
                async with factory() as db:
                    yield db

            app = main.create_app()
            app.dependency_overrides[get_db] = _override_get_db
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                uid=_UID,
                username=_UID,
                is_superuser=False,
                is_active=True,
            )

            async with asyncio.timeout(_WAIT_SECONDS * 3):
                async with main.lifespan(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://test",
                    ) as client:
                        create_response = await client.post(
                            "/api/v1/scheduled-tasks/create",
                            json={
                                "name": "web-notification-task",
                                "session_id": _SESSION_ID,
                                "message": "Run from the web API",
                                "interval_seconds": 60,
                                "run_once": True,
                            },
                        )
                        assert create_response.status_code == 200
                        created_body = create_response.json()
                        assert created_body["code"] == 200
                        task_id = created_body["data"]["id"]

                        created_notification = await _next_notification()
                        async with factory() as reader:
                            created_task = await reader.get(ScheduledTask, task_id)
                        assert created_task is not None
                        assert created_notification.topic == NotificationTopic.SCHEDULED_TASK
                        assert created_notification.event == "changed"
                        assert created_notification.resource is not None
                        assert created_notification.resource.type == "scheduled_task"
                        assert created_notification.resource.id == str(created_task.id)
                        assert created_notification.scope.uid == created_task.uid == _UID
                        assert created_notification.scope.session_id == created_task.session_id == _SESSION_ID

                        delete_response = await client.post(
                            "/api/v1/scheduled-tasks/delete",
                            params={"task_id": task_id},
                        )
                        assert delete_response.status_code == 200
                        assert delete_response.json()["code"] == 200

                        deleted_notification = await _next_notification()
                        async with factory() as reader:
                            deleted_task = await reader.get(ScheduledTask, task_id)
                        assert deleted_task is None
                        assert deleted_notification.topic == NotificationTopic.SCHEDULED_TASK
                        assert deleted_notification.resource is not None
                        assert deleted_notification.resource.type == "scheduled_task"
                        assert deleted_notification.resource.id == str(task_id)
                        assert deleted_notification.scope.uid == _UID
                        assert deleted_notification.scope.session_id == _SESSION_ID

            assert len(runtimes) == 1

            async with factory() as db:
                created_after_shutdown = await scheduled_task_crud.create_scheduled_task(
                    db,
                    name="post-lifespan-direct-task",
                    uid=_UID,
                    session_id=_SESSION_ID,
                    message="Create through CRUD after publisher shutdown",
                    interval_seconds=60,
                    run_once=True,
                )
                task_id = created_after_shutdown.id
            assert task_id is not None

            with pytest.raises(TimeoutError):
                await _next_notification(timeout=0.2)
            async with factory() as reader:
                assert await reader.get(ScheduledTask, task_id) is not None
        finally:
            try:
                await asyncio.wait_for(listener.aclose(), timeout=_WAIT_SECONDS)
            finally:
                await asyncio.wait_for(server.stop(), timeout=_WAIT_SECONDS)
