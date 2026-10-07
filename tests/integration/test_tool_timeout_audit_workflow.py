import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.service as audit_service_module
import app.core.dispatcher as dispatcher_module
import app.core.dispatchers.background as background_module
import app.core.dispatchers.interactive_tools as interactive_tools_module
from app.core.audit.confirmation_persistence import persist_pending_confirmation_bundle
from app.core.audit.integrity import build_tool_round_integrity_snapshot
from app.core.constants import ERR_TOOL_EXECUTION_TIMEOUT, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.tools.file_tool import FILE_TOOL_SCHEMA
from app.core.tools.file_tool import executor as file_tool_executor_module
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkStatus, SessionReplyWorkType


@pytest_asyncio.fixture
async def tool_timeout_session_factory(
    database_factory,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(foreign_keys=True) as session_factory:
        yield session_factory


def _profile_config(tmp_path: Path) -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": 1,
                            "model_id": "timeout-model",
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                }
            },
            "security": {},
            "tool": {
                "enabled_tools": ["file_tool"],
                "tool_timeout": 0.05,
                "allowed_operation_dirs": [str(tmp_path)],
            },
            "other": {},
            "memory": {"enabled": False},
        }
    )


def _tool_arguments(operation: str) -> dict[str, object]:
    arguments: dict[str, object] = {"operation": operation, "path": "target.txt"}
    if operation == "write":
        arguments["content"] = "new\n"
    elif operation == "edit":
        arguments.update(old_text="old", new_text="new")
    else:
        arguments["patch"] = "@@\n-old\n+new"
    return arguments


async def _seed_pending_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tmp_path: Path,
    operation: str,
    exhausted_budget: bool,
) -> int:
    uid = "timeout-owner"
    session_id = "timeout-session"
    tool_call = InternalToolCall(
        id="original-file-call",
        name="file_tool",
        arguments=_tool_arguments(operation),
    )
    cfg = _profile_config(tmp_path)
    integrity = build_tool_round_integrity_snapshot(
        tool_calls=[{"id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments}],
        uid=uid,
        session_id=session_id,
        working_directory=tmp_path,
    )

    async with session_factory() as db:
        db.add(Profile(id=1, uid=uid, name="timeout profile", configs=cfg.model_dump(mode="json")))
        db.add(
            ChatSession(
                session_id=session_id,
                uid=uid,
                profile_id=1,
                llm_request_metadata={
                    "input_tokens": 10000 if exhausted_budget else 100,
                    "input_tokens_source": "provider",
                    "context_window_tokens": 4000,
                    "max_output_tokens": 512,
                },
            )
        )
        await db.flush()

        source = Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]).model_dump_json(exclude_none=True),
            is_processed=True,
        )
        db.add(source)
        await db.flush()

        record = AuditRecord(
            uid=uid,
            operator_username=uid,
            session_id=session_id,
            source="http",
            language="zh",
            status=AuditRecordStatus.PENDING,
            source_assistant_message_id=source.id,
            working_directory=str(tmp_path),
            round_arguments_hash=integrity.round_sha256,
            tool_count=1,
            intent_summary="file timeout audit",
            pending_at=get_local_time(),
            expires_at=get_local_time() + timedelta(hours=1),
        )
        db.add(record)
        await db.flush()
        db.add(
            AuditToolDetail(
                audit_record_id=record.id,
                original_tool_call_id=tool_call.id,
                turn_index=0,
                tool_name=tool_call.name,
                conclusion=AuditToolConclusion.PENDING,
                score=1,
                reason="file timeout audit",
                arguments_hash=integrity.tool_calls[0].arguments_sha256,
                arguments_summary=json.dumps(tool_call.arguments, ensure_ascii=False),
                file_snapshots=[],
            )
        )
        await db.flush()

        await persist_pending_confirmation_bundle(
            db,
            audit_record_id=record.id,
            uid=uid,
            session_id=session_id,
            profile_id=1,
            tool_results=[
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content=json.dumps(
                        {
                            "status": AuditRecordStatus.PENDING.value,
                            "error": "waiting for confirmation",
                            "reason": "confirmation required",
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
            confirmation_payload={
                "type": "audit_confirmation",
                "audit_record_id": record.id,
                "status": AuditRecordStatus.PENDING.value,
                "confirmation_mode": "standard",
            },
            dedupe_key=f"audit-confirmation:{record.id}",
        )
        assert record.id is not None
        return record.id


async def _seed_unconfirmed_round(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tmp_path: Path,
    exhausted_budget: bool,
):
    uid = "timeout-owner"
    session_id = "timeout-session"
    cfg = _profile_config(tmp_path)
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    cfg.security.audit_threshold = 0
    assistant_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="unconfirmed-file-call",
                name="file_tool",
                arguments=_tool_arguments("write"),
            )
        ],
    )

    async with session_factory() as db:
        profile = Profile(id=1, uid=uid, name="timeout profile", configs=cfg.model_dump(mode="json"))
        db.add(profile)
        db.add(
            ChatSession(
                session_id=session_id,
                uid=uid,
                profile_id=1,
                llm_request_metadata={
                    "input_tokens": 10000 if exhausted_budget else 100,
                    "input_tokens_source": "provider",
                    "context_window_tokens": 4000,
                    "max_output_tokens": 512,
                },
            )
        )
        await db.flush()
        source_message = Message(
            session_id=session_id,
            uid=uid,
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TOOL_CALL,
            content=assistant_message.model_dump_json(exclude_none=True),
            is_processed=True,
        )
        db.add(source_message)
        await db.commit()
        assert profile.id == 1
        assert source_message.id is not None
        assistant_message.id = source_message.id
        return cfg, profile, assistant_message, source_message


def _install_blocking_operation(monkeypatch: pytest.MonkeyPatch, operation: str):
    function_name = {
        "write": "write_file",
        "edit": "edit_file",
        "patch": "patch_file",
    }[operation]
    original = getattr(file_tool_executor_module, function_name)
    release = threading.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocking_operation(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(timeout=10):
                raise AssertionError("timed out waiting for file-tool release")
            return original(*args, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(file_tool_executor_module, function_name, blocking_operation)
    return release, started, finished


async def _read_audit_state(session_factory, audit_record_id: int):
    async with session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id).order_by(AuditExecutionRecord.id))).scalars().all())
        confirmation_card = (
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == "timeout-session",
                        Message.uid == "timeout-owner",
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .one()
        )
        tool_messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.audit_record_id == audit_record_id,
                        Message.type == MessageType.TOOL_RESULT,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.version_no))).scalars().all())
        return record, executions, confirmation_card, tool_messages, versions


async def _read_unconfirmed_audit_state(session_factory):
    async with session_factory() as db:
        records = list(
            (
                await db.execute(
                    select(AuditRecord)
                    .where(
                        AuditRecord.uid == "timeout-owner",
                        AuditRecord.session_id == "timeout-session",
                    )
                    .order_by(AuditRecord.id)
                )
            )
            .scalars()
            .all()
        )
        executions = []
        if records and records[0].id is not None:
            executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == records[0].id).order_by(AuditExecutionRecord.id))).scalars().all())
        tool_messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.uid == "timeout-owner",
                        Message.session_id == "timeout-session",
                        Message.type == MessageType.TOOL_RESULT,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        return records, executions, tool_messages


def _install_isolated_auditor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_call_auditor(_db, _cfg, request_payload, _working_directory):
        return (
            {"messages": []},
            {
                "parsed": {
                    "results": [
                        {
                            "tool_call_id": item["tool_call_id"],
                            "score": 0,
                            "reason": "automatic pass",
                            "file_checks": [],
                        }
                        for item in request_payload["tool_calls"]
                    ]
                },
                "file_reads": [],
            },
        )

    original_persist_prepared_audit_round = audit_service_module.persist_prepared_audit_round

    async def persist_prepared_audit_round_in_test_root(db, **kwargs):
        return await original_persist_prepared_audit_round(
            db,
            audit_root=tmp_path / "audit",
            **kwargs,
        )

    monkeypatch.setattr(audit_service_module, "_call_auditor", fake_call_auditor)
    monkeypatch.setattr(audit_service_module, "persist_prepared_audit_round", persist_prepared_audit_round_in_test_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "edit", "patch"])
@pytest.mark.parametrize("exhausted_budget", [False, True])
async def test_file_tool_timeout_persists_execution_unknown_audit_state(
    tool_timeout_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    exhausted_budget: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "temp" / "temp_timeout-owner" / "target.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")
    audit_record_id = await _seed_pending_confirmation(
        tool_timeout_session_factory,
        tmp_path=tmp_path,
        operation=operation,
        exhausted_budget=exhausted_budget,
    )

    async def fake_dispatch_interactive_work(*_args, **_kwargs):
        return {"content": "confirmed continuation", "history": [], "files": []}

    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", fake_dispatch_interactive_work)
    release, started, finished = _install_blocking_operation(monkeypatch, operation)
    execution_task: asyncio.Task | None = None

    try:
        async with tool_timeout_session_factory() as db:
            profile = await db.get(Profile, 1)
            assert profile is not None
            _message, submitted_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
                db,
                uid="timeout-owner",
                session_id="timeout-session",
                profile=profile,
                message="approve",
                attachments=None,
                source="http",
            )
        assert submission_status == "approved"
        assert submitted_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION

        async with tool_timeout_session_factory() as db:
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id="timeout-worker",
                lease_seconds=300,
            )
        assert claimed is not None
        assert claimed.status == SessionReplyWorkStatus.RUNNING

        async with tool_timeout_session_factory() as worker_db:
            worker_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed.id, "timeout-worker")
            execution_task = asyncio.create_task(executor_confirmed_module._execute_confirmed_tools(worker_db, claimed, "timeout-worker"))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                await asyncio.wait_for(execution_task, timeout=5)
            except BaseException:
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
                raise

        assert not release.is_set()
        assert not finished.is_set()
        assert target.read_text(encoding="utf-8") == "old\n"

        record, executions, card, tool_messages, versions = await _read_audit_state(
            tool_timeout_session_factory,
            audit_record_id,
        )
        assert record is not None
        assert record.status == AuditRecordStatus.EXECUTION_UNKNOWN
        assert len(executions) == 1
        assert executions[0].attempt_no == 1
        assert executions[0].status == AuditExecutionStatus.EXECUTION_UNKNOWN
        assert json.loads(card.content or "{}")["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
        assert len(tool_messages) == 1
        assert len(versions) == 3

        stored_message = InternalMessage.model_validate_json(tool_messages[0].content or "{}")
        stored_version = InternalMessage.model_validate_json(versions[-1].content)
        assert stored_message.tool_execution_status == "execution_unknown"
        assert stored_version.tool_execution_status == "execution_unknown"
        if not exhausted_budget:
            payload = json.loads(stored_version.content or "{}")
            assert payload["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
            assert payload["error_code"] == ERR_TOOL_EXECUTION_TIMEOUT
            assert payload["confirmation_decision"] == "approve"
    finally:
        release.set()
        if execution_task is not None and not execution_task.done():
            execution_task.cancel()
        if execution_task is not None:
            await asyncio.gather(execution_task, return_exceptions=True)
        if started.is_set() and not finished.is_set():
            await asyncio.wait_for(finished.wait(), timeout=5)

    assert target.read_text(encoding="utf-8") == "new\n"
    record, executions, _card, _tool_messages, _versions = await _read_audit_state(
        tool_timeout_session_factory,
        audit_record_id,
    )
    assert record is not None
    assert record.status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert len(executions) == 1
    assert executions[0].attempt_no == 1
    assert executions[0].status == AuditExecutionStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted_budget", [False, True])
async def test_background_unconfirmed_file_tool_timeout_persists_execution_unknown(
    tool_timeout_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exhausted_budget: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "temp" / "temp_timeout-owner" / "target.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")
    _cfg, profile, assistant_message, _source_message = await _seed_unconfirmed_round(
        tool_timeout_session_factory,
        tmp_path=tmp_path,
        exhausted_budget=exhausted_budget,
    )

    model_entry = {"model_id": "timeout-model", "protocol": "OPENAI"}
    chat_params = {
        "max_tokens": 512,
        "context_window_k": 1 if exhausted_budget else 16,
        "chat_timeout": 30,
    }
    responses = [
        InternalResponse(message=assistant_message, model="timeout-model"),
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="background continuation"),
            model="timeout-model",
        ),
    ]

    async def fake_generate_chat_with_fallback(*_args, **_kwargs):
        return responses.pop(0), SimpleNamespace(id=1), model_entry, None, chat_params

    _install_isolated_auditor(monkeypatch, tmp_path)
    monkeypatch.setattr(background_module, "generate_chat_with_fallback", fake_generate_chat_with_fallback)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", tool_timeout_session_factory)
    release, started, finished = _install_blocking_operation(monkeypatch, "write")
    execution_task: asyncio.Task | None = None

    try:
        async with tool_timeout_session_factory() as db:
            try:
                execution_task = asyncio.create_task(
                    background_module.BackgroundDispatcherMixin._generate_reply_from_history(
                        db,
                        uid="timeout-owner",
                        session_id="timeout-session",
                        profile=profile,
                        call_context="scheduled_task",
                        allow_tools=True,
                        persist_response=False,
                        restrict_tools_to_background_allowlist=False,
                        reply_source="scheduled_task",
                        submission_context=[InternalMessage(role=MessageRole.USER, content="write the file")],
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=5)
                response = await asyncio.wait_for(execution_task, timeout=5)
                final_message, _turn_messages, files = response
                assert final_message.role == MessageRole.ASSISTANT
                assert final_message.content == "background continuation"
                assert final_message.tool_calls is None
                assert files == []
                assert not release.is_set()
                assert not finished.is_set()
                assert target.read_text(encoding="utf-8") == "old\n"

                records, executions, tool_messages = await _read_unconfirmed_audit_state(
                    tool_timeout_session_factory,
                )
                assert len(records) == 1
                assert records[0].status == AuditRecordStatus.EXECUTION_UNKNOWN
                assert len(executions) == 1
                assert executions[0].attempt_no == 1
                assert executions[0].status == AuditExecutionStatus.EXECUTION_UNKNOWN
                assert len(tool_messages) == 1
                tool_result = InternalMessage.model_validate_json(tool_messages[0].content or "{}")
                assert tool_result.tool_execution_status == "execution_unknown"
                if not exhausted_budget:
                    payload = json.loads(tool_result.content or "{}")
                    assert payload["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
                    assert payload["error_code"] == ERR_TOOL_EXECUTION_TIMEOUT
            finally:
                release.set()
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                if execution_task is not None:
                    await asyncio.gather(execution_task, return_exceptions=True)
                if started.is_set() and not finished.is_set():
                    await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        release.set()

    assert target.read_text(encoding="utf-8") == "new\n"
    records, executions, _tool_messages = await _read_unconfirmed_audit_state(tool_timeout_session_factory)
    assert len(records) == 1
    assert records[0].status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert len(executions) == 1
    assert executions[0].attempt_no == 1
    assert executions[0].status == AuditExecutionStatus.EXECUTION_UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted_budget", [False, True])
async def test_interactive_unconfirmed_file_tool_timeout_persists_execution_unknown(
    tool_timeout_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exhausted_budget: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "temp" / "temp_timeout-owner" / "target.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")
    cfg, profile, assistant_message, source_message = await _seed_unconfirmed_round(
        tool_timeout_session_factory,
        tmp_path=tmp_path,
        exhausted_budget=exhausted_budget,
    )

    _install_isolated_auditor(monkeypatch, tmp_path)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", tool_timeout_session_factory)
    release, started, finished = _install_blocking_operation(monkeypatch, "write")
    execution_task: asyncio.Task | None = None

    try:
        async with tool_timeout_session_factory() as db:
            try:
                session = await db.get(ChatSession, "timeout-session")
                assert session is not None
                assert source_message.id is not None
                assert session.llm_request_metadata is not None
                assistant_message.id = source_message.id
                state = SimpleNamespace(
                    db=db,
                    uid="timeout-owner",
                    session_id="timeout-session",
                    active_tasks=set(),
                    session_source="http",
                    final_message_dedupe_key=None,
                    show_tool_calls=True,
                    goal_mode=False,
                    stream_event_callback=None,
                    username="timeout-owner",
                    profile=profile,
                    additional_user_messages_context=SimpleNamespace(
                        db=db,
                        session_id="timeout-session",
                        uid="timeout-owner",
                        queue_managed=False,
                        fetcher=None,
                    ),
                    checkpoint_state=SimpleNamespace(
                        callback=None,
                        turn_messages=[assistant_message],
                        files_to_user=[],
                        upper_message_id=source_message.id,
                        memory_recall_boundary_message_id=None,
                    ),
                    turn_messages=[assistant_message],
                    files_to_user=[],
                    latest_llm_request_metadata=session.llm_request_metadata,
                    messages=[assistant_message],
                    current_turn=0,
                    cfg=cfg,
                    memory_enabled=False,
                    chat_channel_obj=None,
                    model_entry={"model_id": "timeout-model", "protocol": "OPENAI"},
                    chat_params={"context_window_k": 4, "max_tokens": 512},
                    tools=[FILE_TOOL_SCHEMA],
                    allowed_knowledge_base_ids=[],
                )
                execution_task = asyncio.create_task(
                    interactive_tools_module.handle_interactive_tool_round(
                        state,
                        ai_msg=assistant_message,
                        saved_msg=SimpleNamespace(id=source_message.id),
                        response_id="unconfirmed-timeout-response",
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=5)
                await asyncio.wait_for(execution_task, timeout=5)
                assert not release.is_set()
                assert not finished.is_set()
                assert target.read_text(encoding="utf-8") == "old\n"

                records, executions, tool_messages = await _read_unconfirmed_audit_state(
                    tool_timeout_session_factory,
                )
                assert len(records) == 1
                assert records[0].status == AuditRecordStatus.EXECUTION_UNKNOWN
                assert len(executions) == 1
                assert executions[0].attempt_no == 1
                assert executions[0].status == AuditExecutionStatus.EXECUTION_UNKNOWN
                assert len(tool_messages) == 1
                tool_result = InternalMessage.model_validate_json(tool_messages[0].content or "{}")
                assert tool_result.tool_execution_status == "execution_unknown"
                if not exhausted_budget:
                    payload = json.loads(tool_result.content or "{}")
                    assert payload["status"] == AuditRecordStatus.EXECUTION_UNKNOWN.value
                    assert payload["error_code"] == ERR_TOOL_EXECUTION_TIMEOUT
            finally:
                release.set()
                if execution_task is not None and not execution_task.done():
                    execution_task.cancel()
                if execution_task is not None:
                    await asyncio.gather(execution_task, return_exceptions=True)
                if started.is_set() and not finished.is_set():
                    await asyncio.wait_for(finished.wait(), timeout=5)
    finally:
        release.set()

    assert target.read_text(encoding="utf-8") == "new\n"
    records, executions, _tool_messages = await _read_unconfirmed_audit_state(tool_timeout_session_factory)
    assert len(records) == 1
    assert records[0].status == AuditRecordStatus.EXECUTION_UNKNOWN
    assert len(executions) == 1
    assert executions[0].attempt_no == 1
    assert executions[0].status == AuditExecutionStatus.EXECUTION_UNKNOWN
