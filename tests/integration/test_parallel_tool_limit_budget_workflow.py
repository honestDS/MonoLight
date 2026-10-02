import json
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import tiktoken
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_tools as interactive_tools
from app.core.dispatchers import background as background_module
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.tools.file_tool import FILE_TOOL_SCHEMA
from app.core.tools.todo import MANAGE_TODO_TOOL_NAME, MANAGE_TODO_TOOL_SCHEMA
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_todo import SessionTodoPlan
from tests.database_support import clone_sqlite_schema

UID = "parallel-budget-owner"
SESSION_ID = "parallel-budget-session"


@pytest_asyncio.fixture
async def parallel_tool_limit_budget_session_factory(
    tmp_path: Path,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "parallel-tool-limit-budget-workflow.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(
        database_path,
        tables=[
            PromptLibrary.__table__,
            Profile.__table__,
            ChatSession.__table__,
            SessionTodoPlan.__table__,
            Message.__table__,
        ],
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _profile_config(tmp_path: Path) -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {},
            "security": {},
            "tool": {
                "enabled_tools": ["file_tool"],
                "max_parallel_tools": 5,
                "executor_max_workers": 5,
                "allowed_operation_dirs": [str(tmp_path)],
            },
            "other": {},
            "memory": {},
        }
    )


def _write_large_text(tmp_path: Path) -> Path:
    path = tmp_path / "parallel-budget-large.txt"
    line_body = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau"
    path.write_text(
        "".join(f"line {line_number:04d}: {line_body}\n" for line_number in range(1, 2401)),
        encoding="utf-8",
    )
    return path


def _build_tool_calls(path: Path) -> list[InternalToolCall]:
    return [
        InternalToolCall(
            id=f"file-read-{index}",
            name="file_tool",
            arguments={"operation": "read", "path": str(path)},
        )
        for index in range(8)
    ]


async def _persist_profile_and_session(
    session_factory: async_sessionmaker[AsyncSession],
    cfg: ProfileConfig,
    assistant_message: InternalMessage,
) -> tuple[Profile, Message]:
    async with session_factory() as db:
        profile = Profile(
            id=1,
            uid=UID,
            name="parallel budget workflow",
            configs=cfg.model_dump(mode="json"),
        )
        db.add(profile)
        await db.flush()
        assert profile.id is not None

        db.add(
            ChatSession(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=profile.id,
                source="http",
            )
        )
        await db.flush()
        source_message = Message(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=profile.id,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=assistant_message.model_dump_json(exclude_none=True),
            is_processed=True,
        )
        db.add(source_message)
        await db.commit()
        return profile, source_message


def _build_interactive_state(
    db: AsyncSession,
    profile: Profile,
    cfg: ProfileConfig,
    assistant_message: InternalMessage,
) -> SimpleNamespace:
    return SimpleNamespace(
        db=db,
        uid=UID,
        session_id=SESSION_ID,
        active_tasks=set(),
        session_source="http",
        final_message_dedupe_key=None,
        context_summary_lifecycle_callback=None,
        context_summary_work_validity_checker=None,
        expose_tool_call_content=True,
        show_tool_calls=True,
        goal_mode=False,
        dispatcher_mode="non_stream",
        request_metadata_callback=None,
        stream_event_callback=None,
        dispatch_logger=None,
        username=UID,
        profile=profile,
        initial_msg=InternalMessage(role=MessageRole.USER, content="read the large file"),
        queue_managed=False,
        additional_user_messages_context=SimpleNamespace(
            db=db,
            session_id=SESSION_ID,
            uid=UID,
            queue_managed=False,
            fetcher=None,
        ),
        execution_resume_state=None,
        checkpoint_state=SimpleNamespace(
            callback=None,
            turn_messages=[assistant_message],
            files_to_user=[],
            upper_message_id=assistant_message.id,
            memory_recall_boundary_message_id=None,
        ),
        turn_messages=[assistant_message],
        files_to_user=[],
        is_first_iter=True,
        latest_llm_request_metadata=None,
        messages=[assistant_message],
        current_turn=0,
        cfg=cfg,
        memory_enabled=False,
        memory_precheck_enabled=False,
        chat_channel=None,
        chat_cursor_key="",
        chat_channel_obj=None,
        model_entry={"model_id": "parallel-budget-model", "protocol": "OPENAI"},
        channel_rule=None,
        chat_params={"context_window_k": 16, "max_tokens": 512},
        tools=[FILE_TOOL_SCHEMA],
        allowed_knowledge_base_ids=[],
    )


def _read_tool_results(rows: list[Message], tool_calls: list[InternalToolCall]) -> list[InternalMessage]:
    parsed_by_id: dict[str, InternalMessage] = {}
    for row in rows:
        parsed = InternalMessage.model_validate_json(row.content or "")
        assert parsed.role is MessageRole.TOOL
        assert parsed.tool_call_id is not None
        parsed_by_id[parsed.tool_call_id] = parsed

    assert set(parsed_by_id) == {tool_call.id for tool_call in tool_calls}
    return [parsed_by_id[tool_call.id] for tool_call in tool_calls]


def _assert_tool_result_budget(tool_results: list[InternalMessage]) -> None:
    encoding = tiktoken.get_encoding("cl100k_base")
    for tool_result in tool_results[:5]:
        payload = json.loads(tool_result.content or "")
        assert payload["status"] == "success"
        assert payload["operation"] == "read"
        assert isinstance(payload["content"], str)
        token_count = len(encoding.encode(tool_result.content or "", disallowed_special=()))
        assert 1400 <= token_count <= 1600

    for tool_result in tool_results[5:]:
        payload = json.loads(tool_result.content or "{}")
        assert payload["status"] == "failed"
        assert payload["error"] == "parallel_limit_exceeded"
        assert payload["requested"] == 8
        assert payload["limit"] == 5
        assert payload["executed"] is False


@pytest.mark.asyncio
async def test_interactive_parallel_tool_limit_budget_uses_executable_call_count(
    parallel_tool_limit_budget_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _profile_config(tmp_path)
    tool_calls = _build_tool_calls(_write_large_text(tmp_path))
    assistant_message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls)
    profile, saved_assistant_message = await _persist_profile_and_session(
        parallel_tool_limit_budget_session_factory,
        cfg,
        assistant_message,
    )

    async with parallel_tool_limit_budget_session_factory() as db:
        db.add(
            SessionTodoPlan(
                session_id=SESSION_ID,
                uid=UID,
                revision=1,
                todos=[{"content": "existing todo", "status": "pending"}],
            )
        )
        await db.commit()

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", parallel_tool_limit_budget_session_factory)

    async def audit_tool_round(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(interactive_tools, "audit_tool_round", audit_tool_round)
    monkeypatch.setattr(
        interactive_tools,
        "calculate_tool_result_round_budget_tokens",
        lambda **_kwargs: 8000,
    )

    async with parallel_tool_limit_budget_session_factory() as db:
        state = _build_interactive_state(db, profile, cfg, assistant_message)
        await interactive_tools.handle_interactive_tool_round(
            state,
            ai_msg=assistant_message,
            saved_msg=SimpleNamespace(id=saved_assistant_message.id),
            response_id="parallel-budget-response",
        )

    async with parallel_tool_limit_budget_session_factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == SESSION_ID,
                Message.uid == UID,
                Message.type == MessageType.TOOL_RESULT,
            )
            .order_by(Message.id.asc())
        )
        rows = list(result.scalars().all())

    assert len(rows) == 8
    assert sum(row.model_context_suffix is not None for row in rows) == 1
    tool_results = _read_tool_results(rows, tool_calls)
    _assert_tool_result_budget(tool_results)


@pytest.mark.asyncio
async def test_interactive_tool_round_budget_ignores_openai_responses_image_output(
    parallel_tool_limit_budget_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _profile_config(tmp_path)
    todos = [{"content": "complete regression step", "status": "completed"}]
    assistant_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        provider_metadata={
            "protocol": "openai_responses",
            "output": [
                {"type": "reasoning", "encrypted_content": "encrypted reasoning"},
                {"type": "image_generation_call", "result": "YQ==" * 200000},
            ],
        },
        tool_calls=[
            InternalToolCall(
                id="manage-todo-call",
                name=MANAGE_TODO_TOOL_NAME,
                arguments={
                    "operation": "write",
                    "expected_revision": 0,
                    "todos": todos,
                },
            )
        ],
    )
    profile, saved_assistant_message = await _persist_profile_and_session(
        parallel_tool_limit_budget_session_factory,
        cfg,
        assistant_message,
    )

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", parallel_tool_limit_budget_session_factory)

    async def audit_tool_round(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(interactive_tools, "audit_tool_round", audit_tool_round)

    async with parallel_tool_limit_budget_session_factory() as db:
        state = _build_interactive_state(db, profile, cfg, assistant_message)
        state.model_entry = {"model_id": "gpt-5.6-luna", "protocol": "OPENAI_RESPONSES"}
        state.latest_llm_request_metadata = {
            "input_tokens": 408566,
            "input_tokens_source": "provider",
        }
        state.chat_params = {"context_window_k": 700, "max_tokens": 20480}
        state.tools = [MANAGE_TODO_TOOL_SCHEMA]
        await interactive_tools.handle_interactive_tool_round(
            state,
            ai_msg=assistant_message,
            saved_msg=SimpleNamespace(id=saved_assistant_message.id),
            response_id="todo-budget-response",
        )

    state_tool_results = [message for message in state.messages if message.role is MessageRole.TOOL]
    assert len(state_tool_results) == 1
    state_payload = json.loads((state_tool_results[0].content or "").split("\n\n<current_session_todo_snapshot>", 1)[0])
    assert state_payload == {
        "status": "success",
        "operation": "write",
        "revision": 1,
        "todos": todos,
    }
    assert state_payload != {}

    async with parallel_tool_limit_budget_session_factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == SESSION_ID,
                Message.uid == UID,
                Message.type == MessageType.TOOL_RESULT,
            )
            .order_by(Message.id.asc())
        )
        rows = list(result.scalars().all())
        plan = await db.get(SessionTodoPlan, SESSION_ID)

    assert len(rows) == 1
    persisted_tool_result = InternalMessage.model_validate_json(rows[0].content or "")
    persisted_payload = json.loads(persisted_tool_result.content or "{}")
    assert persisted_payload == state_payload
    assert persisted_payload != {}
    assert plan is not None
    assert plan.revision == 1
    assert plan.todos == todos


@pytest.mark.asyncio
async def test_background_parallel_tool_limit_budget_uses_executable_call_count(
    parallel_tool_limit_budget_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _profile_config(tmp_path)
    tool_calls = _build_tool_calls(_write_large_text(tmp_path))
    assistant_message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls)
    profile, saved_assistant_message = await _persist_profile_and_session(
        parallel_tool_limit_budget_session_factory,
        cfg,
        assistant_message,
    )
    assistant_message.id = saved_assistant_message.id

    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", parallel_tool_limit_budget_session_factory)

    async def get_user(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(username=UID)

    async def validate_profile_and_cfg(*_args: Any, **_kwargs: Any) -> ProfileConfig:
        return cfg

    async def get_tools_for_profile(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], list[int]]:
        return [FILE_TOOL_SCHEMA], []

    async def audit_tool_round(*_args: Any, **_kwargs: Any) -> None:
        return None

    responses = [
        InternalResponse(message=assistant_message, model="parallel-budget-model"),
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="background complete"),
            model="parallel-budget-model",
        ),
    ]

    async def generate_chat_with_fallback(*_args: Any, **_kwargs: Any) -> tuple[Any, None, dict[str, Any], None, dict[str, int]]:
        return (
            responses.pop(0),
            None,
            {"model_id": "parallel-budget-model", "protocol": "OPENAI"},
            None,
            {"context_window_k": 16, "max_tokens": 512, "chat_timeout": 30},
        )

    monkeypatch.setattr(background_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(background_module, "validate_profile_and_cfg", validate_profile_and_cfg)
    monkeypatch.setattr(background_module, "get_tools_for_profile", get_tools_for_profile)
    monkeypatch.setattr(background_module, "audit_tool_round", audit_tool_round)
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", generate_chat_with_fallback)
    monkeypatch.setattr(
        background_module,
        "calculate_tool_result_round_budget_tokens",
        lambda **_kwargs: 8000,
    )

    async with parallel_tool_limit_budget_session_factory() as db:
        final_message, _turn_messages, files = await BackgroundDispatcherMixin._generate_reply_from_history(
            db,
            uid=UID,
            session_id=SESSION_ID,
            profile=profile,
            call_context="scheduled_task",
            allow_tools=True,
            persist_response=False,
            restrict_tools_to_background_allowlist=False,
            reply_source="scheduled_task",
            submission_context=[assistant_message],
        )

    assert final_message.content == "background complete"
    assert files == []
    assert responses == []

    async with parallel_tool_limit_budget_session_factory() as db:
        result = await db.execute(
            select(Message)
            .where(
                Message.session_id == SESSION_ID,
                Message.uid == UID,
                Message.type == MessageType.TOOL_RESULT,
            )
            .order_by(Message.id.asc())
        )
        rows = list(result.scalars().all())

    assert len(rows) == 8
    tool_results = _read_tool_results(rows, tool_calls)
    _assert_tool_result_budget(tool_results)
