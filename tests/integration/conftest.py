from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import Pool
from sqlalchemy.sql.schema import Table
from sqlmodel import SQLModel

from app.core.constants import (
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.models.channel import ModelChannel
from app.models.message import Message
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.system_setting import SystemSetting
from app.models.user import User
from tests.database_support import clone_sqlite_schema


@pytest.fixture
def database_factory(tmp_path: Path):
    @asynccontextmanager
    async def create_database(
        *,
        tables: Iterable[Table] | None = None,
        foreign_keys: bool = False,
        wal: bool = False,
        name: str = "workflow.sqlite3",
        poolclass: type[Pool] | None = None,
        create_schema: bool = True,
        session_class: type[AsyncSession] = AsyncSession,
    ) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
        database_path = tmp_path / name
        if create_schema:
            await clone_sqlite_schema(database_path, tables=tables)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path.as_posix()}",
            connect_args={"timeout": 30},
            poolclass=poolclass,
        )

        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite_connection(
            dbapi_connection: Any,
            _connection_record: Any,
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
                if wal:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        try:
            yield async_sessionmaker(
                engine,
                class_=session_class,
                expire_on_commit=False,
            )
        finally:
            await engine.dispose()

    return create_database


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[Message.__table__, ChatSession.__table__],
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def setup_session_factory(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "setup-api.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    try:
        await clone_sqlite_schema(database_path, tables=[SystemSetting.__table__, User.__table__, ModelChannel.__table__, PromptLibrary.__table__, Profile.__table__])

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add_all(
                [
                    SystemSetting(key=SETUP_STATUS_KEY, value=SETUP_STATUS_PENDING),
                    SystemSetting(key=SETUP_ADMIN_UID_KEY, value=""),
                ]
            )
            await session.commit()

        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture
def image_chat_runtime(
    database_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio
    import base64
    import io
    import json
    from collections import deque
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI
    from PIL import Image
    from sqlalchemy import select

    import app.core.crud.channel.cursor as channel_cursor_module
    import app.core.dispatchers.dispatcher as dispatcher_module
    import app.core.session_reply_queue.consumer as consumer_module
    import app.core.session_reply_queue.executor_audit as executor_audit_module
    import app.core.session_reply_queue.executor_interactive as executor_interactive_module
    import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
    import app.core.session_reply_queue.executor_metadata as executor_metadata_module
    import app.core.tools.send_file_to_user as send_file_to_user_module
    from app.api.v1.chat import router
    from app.core.background_tasks.manager import BackgroundTaskManager
    from app.core.security import get_current_user
    from app.handler import register_handlers
    from app.models.background_task import BackgroundTask, BackgroundTaskStatus
    from app.models.channel import ModelChannel
    from app.models.message import InternalMessage
    from app.models.profile import Profile, ProfileConfig
    from app.models.session import ChatSession
    from app.models.session_reply_work_item import SessionReplyWorkItem
    from app.models.user import User
    from app.providers.database import get_db
    from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

    uid = "http-image-user"
    session_id = "http-image-session"
    model_id = "http-image-model"

    @asynccontextmanager
    async def create_runtime(
        protocol: str = "OPENAI_RESPONSES",
        goal_mode: bool = True,
        max_parallel_tools: int = 5,
        image_gates: dict[str, asyncio.Event] | None = None,
    ):
        import app.api.v1.chat as chat_module
        import app.api.v1.files as files_module
        import app.core.background_tasks.external_work as external_work_module
        import app.core.background_tasks.manager as background_manager_module
        import app.core.background_tasks.runner as background_runner_module
        import app.core.message_platforms.notifier as message_notifier_module
        import app.core.session.notifier as session_notifier_module
        import app.core.utils.llm_request_policy as llm_request_policy_module
        import app.providers.database as database_module
        from app.core.crud.session.reply_work_item import session_reply_work_item_crud
        from app.providers.image_generation import ImageGenerationClient

        monkeypatch.chdir(tmp_path)
        image_buffer = io.BytesIO()
        image = Image.new("RGB", (1, 1), color=(32, 128, 224))
        try:
            image.save(image_buffer, format="PNG")
        finally:
            image.close()
        image_bytes = image_buffer.getvalue()
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        image_requests: list[dict] = []
        image_started: asyncio.Queue[str] = asyncio.Queue()
        provider_payloads: list[dict] = []
        outputs = deque()

        async def fake_generate_image(**kwargs):
            image_requests.append(dict(kwargs))
            prompt = kwargs.get("prompt")
            await image_started.put(prompt)
            if image_gates is not None and prompt in image_gates:
                await image_gates[prompt].wait()
            return {"data": [{"b64_json": image_b64}]}

        async def fake_post_json(_transformer, **kwargs):
            provider_payloads.append(kwargs["payload"])
            if not outputs:
                raise AssertionError("image_chat_runtime.outputs is empty")
            message = outputs.popleft()
            assert isinstance(message, InternalMessage)
            content, tool_calls = message.content if isinstance(message.content, str) else None, message.tool_calls or []
            if protocol == "OPENAI":
                raw_tool_calls = [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))}} for call in tool_calls]
                return {
                    "id": f"workflow-chat-response-{len(provider_payloads)}",
                    "object": "chat.completion",
                    "model": model_id,
                    "status": "completed",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content, "tool_calls": raw_tool_calls or None}, "finish_reason": "tool_calls" if raw_tool_calls else "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            raw_output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": content}]}] if content is not None else []
            raw_output.extend({"type": "function_call", "id": call.id, "call_id": call.id, "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")), "status": "completed"} for call in tool_calls)
            return {
                "id": f"workflow-response-{len(provider_payloads)}",
                "object": "response",
                "model": model_id,
                "status": "completed",
                "output": raw_output,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        async with database_factory(foreign_keys=True, wal=True) as factory:
            async with factory() as db:
                channel = ModelChannel(
                    id=1,
                    name="workflow-image-channel",
                    api_key="enc:v1:stored-test-key",
                    base_url="https://llm.invalid",
                    model_ids=[
                        {
                            "model_id": model_id,
                            "usage": "CHAT",
                            "protocol": protocol,
                            "context_window_k": 32,
                            "max_tokens": 512,
                        },
                        {
                            "model_id": "workflow-image-model",
                            "usage": "IMAGE_GENERATION",
                            "protocol": "OPENAI_IMAGE",
                        },
                    ],
                )
                profile = Profile(
                    id=1,
                    uid=uid,
                    name="workflow image profile",
                    configs=ProfileConfig.model_validate(
                        {
                            "channel": {"chat_channel": {"rules": [{"channel_id": 1, "model_id": model_id, "priority": 1, "weight": 1}]}, "image_generation_channel": {"rules": [{"channel_id": 1, "model_id": "workflow-image-model", "priority": 1, "weight": 1}]}},
                            "security": {"audit_threshold": 0},
                            "tool": {"enabled_tools": ["generate_image", "send_file_to_user", "file_tool"], "allowed_operation_dirs": [str(tmp_path.resolve())], "max_parallel_tools": max_parallel_tools, "background_task_max_concurrency": 2},
                            "other": {},
                            "memory": {"enabled": False, "precheck_enabled": False},
                        }
                    ).model_dump(mode="json"),
                )
                user = User(uid=uid, username="workflow_image_user")
                session = ChatSession(
                    session_id=session_id,
                    uid=uid,
                    profile_id=1,
                    profile_override_id=1,
                    source="http",
                    stream_llm_request=False,
                    goal_mode=goal_mode,
                    max_turns=8,
                    show_tool_calls=False,
                )
                db.add_all([channel, profile, user, session])
                await db.commit()

            modules = (
                database_module,
                chat_module,
                dispatcher_module,
                executor_audit_module,
                executor_interactive_module,
                executor_lifecycle_module,
                executor_metadata_module,
                consumer_module,
                external_work_module,
                background_manager_module,
                background_runner_module,
                channel_cursor_module,
                llm_request_policy_module,
                message_notifier_module,
                session_notifier_module,
            )
            for module in modules:
                monkeypatch.setattr(module, "AsyncSessionLocal", factory)

            manager = BackgroundTaskManager()
            monkeypatch.setattr(background_manager_module, "background_task_manager", manager)
            monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
            monkeypatch.setattr(ImageGenerationClient, "generate_image", fake_generate_image)
            monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"t" * 32)
            monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")

            transformer = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
            monkeypatch.setattr(transformer, "_post_json", fake_post_json)

            app = FastAPI()
            register_handlers(app)
            app.include_router(router, prefix="/api/v1")
            app.include_router(files_module.router, prefix="/api/v1")

            async def override_get_db():
                async with factory() as db:
                    yield db

            auth_user = SimpleNamespace(uid=uid, username="workflow_image_user", is_superuser=False, is_active=True)
            app.dependency_overrides[get_db] = override_get_db
            app.dependency_overrides[get_current_user] = lambda: auth_user
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
            run_count = 0

            async def run_next(*, independent_only: bool | None = None):
                nonlocal run_count
                run_count += 1
                worker_id = f"image-chat-worker-{run_count}"
                async with factory() as db:
                    work = await session_reply_work_item_crud.claim_next(
                        db,
                        worker_id=worker_id,
                        lease_seconds=300,
                        independent_only=independent_only,
                    )
                if work is None or work.id is None:
                    return None
                await consumer_module.SessionReplyConsumer()._run_claimed(work.id, worker_id, work.attempt_count, work.max_attempts)
                async with factory() as db:
                    return await db.get(SessionReplyWorkItem, work.id)

            async def complete_images(task_ids=None):
                async with factory() as db:
                    statement = select(BackgroundTask).where(
                        BackgroundTask.uid == uid,
                        BackgroundTask.session_id == session_id,
                        BackgroundTask.profile_id == 1,
                        BackgroundTask.tool_name == "generate_image",
                        BackgroundTask.status == BackgroundTaskStatus.PENDING,
                    )
                    if task_ids is not None:
                        ids = {task_ids} if isinstance(task_ids, int) else set(task_ids)
                        statement = statement.where(BackgroundTask.id.in_(ids))
                    tasks = (await db.execute(statement.order_by(BackgroundTask.id))).scalars().all()
                for task in tasks:
                    if task.id is not None:
                        await background_runner_module.run_background_task(task.id, worker_id=f"image-task-worker-{task.id}")

            runtime = SimpleNamespace(
                factory=factory,
                client=client,
                profile=profile,
                uid=uid,
                session_id=session_id,
                chat_model_id=model_id,
                manager=manager,
                image_requests=image_requests,
                image_started=image_started,
                provider_payloads=provider_payloads,
                outputs=outputs,
                image_bytes=image_bytes,
                run_next=run_next,
                complete_images=complete_images,
            )
            try:
                yield runtime
            finally:
                await client.aclose()
                await manager.stop()

    return create_runtime
