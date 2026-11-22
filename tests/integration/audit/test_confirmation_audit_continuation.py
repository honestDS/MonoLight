import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.result_delivery as result_delivery_module
import app.core.background_tasks.runner as runner_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.dispatchers.interactive_runtime as interactive_runtime_module
import app.core.dispatchers.memory.request as memory_request_module
import app.providers.database as database_provider
from app.core.confirmation import events as confirmation_events_module
from app.core.constants import (
    END_SESSION_TOOL_NAME,
    ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.i18n import t
from app.core.prompts import LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal import ShellNonInteractiveCompletedResult
from app.core.tools import MANAGE_TODO_TOOL_NAME, TOOL_EXECUTOR_MAP
from app.core.utils.dispatcher import channel_call as channel_call_module
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
)
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.provider_usage import ProviderUsageDetail, ProviderUsageType
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.llm.client import LLMClient
from tests.integration.audit.confirmation_workflow_support import _confirmation_workflow_session_factory as _confirmation_workflow_session_factory
from tests.integration.audit.confirmation_workflow_support import (
    _profile_config,
    _seed_pending_confirmation,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_requested", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("decision", ["同意", "忽略", "拒绝"], ids=["approve", "ignore", "reject"])
async def test_confirmation_audit_decision_continues_without_forced_memory_recall(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_requested: bool,
    decision: str,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
        include_initial_user_message=True,
    )

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        session = await db.get(ChatSession, "session-confirmation")
        assert profile is not None
        assert session is not None
        profile.configs = _profile_config()
        profile.configs["memory"].update({"enabled": True, "precheck_enabled": True})
        session.max_turns = 3
        if decision == "忽略":
            details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record_id).order_by(AuditToolDetail.turn_index))).scalars().all())
            assert details
            for detail in details:
                detail.score = 8
        await db.commit()

    for module in (executor_lifecycle_module, executor_interactive_module, executor_metadata_module):
        monkeypatch.setattr(module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(external_work_module, "AsyncSessionLocal", confirmation_workflow_session_factory)

    fake_channel = SimpleNamespace(
        id=1,
        name="test-channel",
        usage_key="test-channel-key",
        base_url="https://chat.invalid",
        get_decrypted_api_key=lambda: "test-key",
        http_proxy=None,
    )
    fake_model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "is_enabled": True,
        "context_window_k": 32,
        "max_tokens": 256,
    }
    fake_channel_rule = SimpleNamespace(channel_id=1, model_id="chat-model", priority=1, weight=1)

    async def fake_select_channel(_db, _channel_cfg, expected_usage: str, **_kwargs):
        assert expected_usage == "CHAT"
        return fake_channel, dict(fake_model_entry), fake_channel_rule

    for module in (
        dispatcher_module,
        interactive_runtime_module,
        interactive_generation_module,
        memory_request_module,
        channel_call_module,
    ):
        monkeypatch.setattr(module, "select_channel", fake_select_channel)

    commands: list[str] = []

    class FakeShellExecutor:
        requires_audit = True

        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **arguments) -> str:
            command = arguments["command"]
            commands.append(command)
            return ShellNonInteractiveCompletedResult(
                stdout=f"{command}\n",
                stderr="",
                exit_code=0,
                system_info="test-system",
            ).model_dump_json()

    monkeypatch.setitem(TOOL_EXECUTOR_MAP, "execute_shell", FakeShellExecutor)

    confirmation_events: list[dict] = []
    executor_lifecycle_events: list[dict] = []
    executor_interactive_events: list[dict] = []

    async def capture_confirmation_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        confirmation_events.append(event_payload)

    async def capture_lifecycle_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        executor_lifecycle_events.append(event_payload)

    async def capture_interactive_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        executor_interactive_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_confirmation_event)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_lifecycle_event)
    monkeypatch.setattr(executor_interactive_module, "send_session_stream_event", capture_interactive_event)

    captured_requests: list[dict[str, object]] = []
    forced_recall_requests: list[list[InternalMessage]] = []
    normal_turn = 0

    def build_fake_llm_response(messages: list[InternalMessage], tools) -> InternalResponse:
        nonlocal normal_turn
        request_messages = [message.model_copy(deep=True) for message in messages]
        captured_requests.append({"messages": request_messages, "tools": tools})
        first_system_content = next(
            (message.content for message in request_messages if message.role == MessageRole.SYSTEM),
            None,
        )
        if first_system_content == LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT:
            forced_recall_requests.append(request_messages)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="unexpected forced recall"),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                finish_reason="stop",
            )

        if decision == "拒绝":
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="rejected reply"),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                finish_reason="stop",
            )

        if normal_turn == 0:
            assert tools
            assert any(item.get("function", {}).get("name") == MANAGE_TODO_TOOL_NAME for item in tools)
            normal_turn = 1
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="todo-call",
                            name=MANAGE_TODO_TOOL_NAME,
                            arguments={"operation": "read"},
                        )
                    ],
                ),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                finish_reason="tool_calls",
            )

        normal_turn += 1
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="continued reply"),
            model="chat-model",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            finish_reason="stop",
        )

    async def fake_generate(**kwargs) -> InternalResponse:
        return build_fake_llm_response(kwargs["messages"], kwargs.get("tools"))

    async def fake_generate_with_stream_callback(**kwargs) -> InternalResponse:
        response = build_fake_llm_response(kwargs["messages"], kwargs.get("tools"))
        if response.message.content:
            await kwargs["on_content"](response.message.content)
        return response

    monkeypatch.setattr(LLMClient, "generate", fake_generate)
    monkeypatch.setattr(LLMClient, "generate_with_stream_callback", fake_generate_with_stream_callback)

    source = "ws" if stream_requested else "http"
    worker_id = f"confirmation-audit-reply-{source}-{decision}"
    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, submitted_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message=decision,
            attachments=None,
            source=source,
            stream_requested=stream_requested,
            actor_username="owner",
        )
        assert submitted_work.id is not None
        assert submission_status in {"approved", "rejected"}
        submitted_work_id = submitted_work.id

    async with confirmation_workflow_session_factory() as db:
        claimed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert claimed_work is not None
    assert claimed_work.id == submitted_work_id
    await executor_lifecycle_module.execute_session_reply_work(claimed_work.id, worker_id)

    if decision != "拒绝":
        async with confirmation_workflow_session_factory() as db:
            suspended_work = await db.get(SessionReplyWorkItem, submitted_work_id)
            background_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())

        assert suspended_work is not None
        assert suspended_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert len(background_tasks) == 2
        assert [task.status for task in background_tasks] == [BackgroundTaskStatus.PENDING, BackgroundTaskStatus.PENDING]
        assert commands == []

        for task in background_tasks:
            assert task.id is not None
            await runner_module.run_background_task(task.id, worker_id=f"{worker_id}-background-{task.id}")

        async with confirmation_workflow_session_factory() as db:
            resumed_work = await db.get(SessionReplyWorkItem, submitted_work_id)
        assert resumed_work is not None
        assert resumed_work.status == SessionReplyWorkStatus.READY_FOR_LLM

        async with confirmation_workflow_session_factory() as db:
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
            )
        assert claimed_work is not None
        assert claimed_work.id == submitted_work_id
        await executor_lifecycle_module.execute_session_reply_work(claimed_work.id, worker_id)

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        work = await db.get(SessionReplyWorkItem, submitted_work_id)
        decision_messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.session_id == "session-confirmation",
                        Message.type == MessageType.AUDIT_DECISION,
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-confirmation").order_by(Message.id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id).order_by(AuditExecutionRecord.id))).scalars().all())
        checkpoint = await session_reply_work_item_crud.get_checkpoint(db, submitted_work_id) or {}

    assert record is not None
    assert work is not None
    assert work.status == SessionReplyWorkStatus.SUCCEEDED
    assert work.work_type == (SessionReplyWorkType.FOREGROUND_REPLY if decision == "拒绝" else SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION)
    assert len(decision_messages) == 1
    decision_message = decision_messages[0]
    assert decision_message.type == MessageType.AUDIT_DECISION
    assert decision_message.content == decision
    assert record.decision_message_id == decision_message.id
    assert record.decision_raw_message == decision
    expected_decision = {"同意": "approve", "忽略": "approve", "拒绝": "reject"}[decision]
    expected_record_status = AuditRecordStatus.REJECTED if decision == "拒绝" else AuditRecordStatus.SUCCEEDED
    assert record.decision is not None and record.decision.value == expected_decision
    assert record.status == expected_record_status
    assert forced_recall_requests == []
    assert not [message for message in messages if (message.dedupe_key or "").startswith("memory-recall-")]

    assert checkpoint.get("memory_recall_boundary_message_id") != decision_message.id
    final_message = next(message for message in messages if message.id == work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.type == MessageType.TEXT
    assert final_message.content == ("rejected reply" if decision == "拒绝" else "continued reply")

    if decision == "拒绝":
        assert commands == []
        assert executions == []
        assert not any(json.loads(InternalMessage.model_validate_json(message.content or "{}").content or "{}").get("operation") == "read" for message in messages if message.type == MessageType.TOOL_RESULT)
        assert captured_requests[0]["tools"] is None
        assert len(captured_requests) == 1
        captured_messages = captured_requests[0]["messages"]
        assert isinstance(captured_messages, list)
        original_shell_call_ids = {"original-shell-call-1", "original-shell-call-2"}
        original_shell_results = {message.tool_call_id: json.loads(message.content or "{}")["tool_result"] for message in captured_messages if isinstance(message, InternalMessage) and message.tool_call_id in original_shell_call_ids}
        assert set(original_shell_results) == original_shell_call_ids
        for result in original_shell_results.values():
            assert result["status"] == "rejected"
            assert result["reason"] == t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER)
            assert result["error"] == t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER)
            assert result["confirmation_status"] == "reject"
            assert result["confirmation_decision"] == decision
            assert not (
                {
                    "score",
                    "deny",
                    "file_checks",
                    "intent_summary",
                    "arguments_summary",
                    "file_snapshots",
                }
                & result.keys()
            )
    else:
        assert commands == ["echo confirmed-1", "echo confirmed-2"]
        assert [execution.status for execution in executions] == [
            AuditExecutionStatus.SUCCEEDED,
            AuditExecutionStatus.SUCCEEDED,
        ]
        todo_results = []
        for message in messages:
            if message.type != MessageType.TOOL_RESULT:
                continue
            internal = InternalMessage.model_validate_json(message.content or "{}")
            if internal.tool_call_id != "todo-call":
                continue
            todo_results.append(json.loads(internal.content or "{}"))
        assert todo_results
        assert todo_results[0] == {
            "status": "success",
            "operation": "read",
            "revision": 0,
            "todos": [],
        }
        assert len(captured_requests) == 2

    assert executor_lifecycle_events
    if stream_requested and decision != "拒绝":
        assert executor_interactive_events


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_requested", [False, True], ids=["non-stream", "stream"])
@pytest.mark.parametrize("reply_mode", ["summary", "silent"], ids=["summary", "silent"])
async def test_agent_work_confirmation_continuation_preserves_private_work_scope(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_requested: bool,
    reply_mode: str,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
        include_initial_user_message=True,
    )
    private_file = tmp_path / "agent-private.txt"
    private_file.write_text("agent-private-marker\n", encoding="utf-8")

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        session = await db.get(ChatSession, "session-confirmation")
        record = await db.get(AuditRecord, audit_record_id)
        assert profile is not None
        assert session is not None
        assert record is not None and record.source_assistant_message_id is not None
        initial_session_metadata = deepcopy(session.llm_request_metadata)
        initial_metadata_work_sequence_no = session.llm_request_metadata_work_sequence_no
        initial_metadata_event_sequence_no = session.llm_request_metadata_event_sequence_no
        assert isinstance(initial_session_metadata, dict)
        assert initial_session_metadata["input_tokens"] == 1000
        assert initial_session_metadata["total_input_tokens"] == 1000
        assert initial_session_metadata["context_window_tokens"] == 4096
        assert initial_session_metadata["max_output_tokens"] == 512

        profile.configs = _profile_config()
        profile.configs["tool"].update(
            {
                "enabled_tools": ["execute_shell", "file_tool"],
                "allowed_operation_dirs": [str(tmp_path.resolve())],
            }
        )
        session.max_turns = 6
        session.show_tool_calls = True

        source_work, created = await session_reply_queue_manager.enqueue_agent_work(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile_id=1,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id="900",
            dedupe_key="scheduled-agent-confirmation-source",
            spec=AgentWorkSpec(
                instruction="Continue the scheduled task in its own context.",
                execution_scope="task",
                report_target=AgentWorkReportTarget.SESSION,
            ),
            commit=False,
        )
        assert created
        assert source_work.id is not None
        source_work_id = source_work.id

        source_rows = list(
            (
                await db.execute(
                    select(Message)
                    .where(
                        Message.session_id == "session-confirmation",
                        Message.uid == "owner",
                        Message.type.in_([MessageType.TEXT, MessageType.TOOL_CALL, MessageType.TOOL_RESULT]),
                    )
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        source_messages = parse_db_messages_to_internal(source_rows)
        assert source_messages
        checkpoint_messages = [message.model_dump(mode="json", exclude_none=True) for message in source_messages]
        for source_row in source_rows:
            source_row.work_id = source_work.id
            db.add(source_row)

        source_checkpoint = {
            "messages": checkpoint_messages,
            "turn_messages": [],
            "files_to_user": [],
            "current_turn": 1,
            "execution_phase": "running",
        }
        source_work.status = SessionReplyWorkStatus.SUCCEEDED
        source_work.execution_state = {
            **(source_work.execution_state or {}),
            "execution_scope": "task",
            "frozen_context_message_ids": [],
        }
        record.source_work_id = source_work.id
        record.execution_scope = "task"
        db.add(
            Message(
                session_id="session-confirmation",
                uid="owner",
                profile_id=1,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="仅主会话可见的无关内容",
                is_processed=True,
            )
        )
        await db.flush()
        await session_reply_work_item_crud.save_checkpoint(db, source_work.id, source_checkpoint)
        await db.commit()

    for module in (
        executor_lifecycle_module,
        executor_interactive_module,
        executor_metadata_module,
        dispatcher_module,
        runner_module,
        external_work_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(database_provider, "AsyncSessionLocal", confirmation_workflow_session_factory)

    fake_channel = SimpleNamespace(
        id=1,
        name="test-channel",
        usage_key="test-channel-key",
        base_url="https://chat.invalid",
        get_decrypted_api_key=lambda: "test-key",
        http_proxy=None,
    )
    fake_model_entry = {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "is_enabled": True,
        "context_window_k": 32,
        "max_tokens": 256,
    }
    fake_channel_rule = SimpleNamespace(channel_id=1, model_id="chat-model", priority=1, weight=1)

    async def fake_select_channel(_db, _channel_cfg, expected_usage: str, **_kwargs):
        assert expected_usage == "CHAT"
        return fake_channel, dict(fake_model_entry), fake_channel_rule

    for module in (
        dispatcher_module,
        interactive_runtime_module,
        interactive_generation_module,
        memory_request_module,
        channel_call_module,
    ):
        monkeypatch.setattr(module, "select_channel", fake_select_channel)

    commands: list[str] = []

    class FakeShellExecutor:
        requires_audit = True

        def __init__(self, **_kwargs) -> None:
            pass

        async def execute(self, **arguments) -> str:
            command = arguments["command"]
            commands.append(command)
            return ShellNonInteractiveCompletedResult(
                stdout=f"{command}\n",
                stderr="",
                exit_code=0,
                system_info="test-system",
            ).model_dump_json()

    monkeypatch.setitem(TOOL_EXECUTOR_MAP, "execute_shell", FakeShellExecutor)

    confirmation_events: list[dict] = []
    lifecycle_events: list[dict] = []
    stream_events: list[dict] = []

    async def capture_confirmation_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        confirmation_events.append(event_payload)

    async def capture_lifecycle_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        lifecycle_events.append(event_payload)

    async def capture_stream_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        stream_events.append(event_payload)

    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_confirmation_event)
    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_lifecycle_event)
    monkeypatch.setattr(executor_interactive_module, "send_session_stream_event", capture_stream_event)

    captured_requests: list[list[InternalMessage]] = []
    forced_recall_requests: list[list[InternalMessage]] = []
    llm_call_count = 0

    def build_fake_llm_response(messages: list[InternalMessage], tools) -> InternalResponse:
        nonlocal llm_call_count
        request_messages = [message.model_copy(deep=True) for message in messages]
        captured_requests.append(request_messages)
        if any(message.role == MessageRole.SYSTEM and message.content == LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT for message in request_messages):
            forced_recall_requests.append(request_messages)
        llm_call_count += 1
        if llm_call_count == 1:
            assert tools
            assert any(item.get("function", {}).get("name") == "file_tool" for item in tools)
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="continuation-file-call",
                            name="file_tool",
                            arguments={"operation": "read", "path": str(private_file)},
                        )
                    ],
                ),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                finish_reason="tool_calls",
            )
        if llm_call_count == 2:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="continuation-end-session",
                            name=END_SESSION_TOOL_NAME,
                            arguments={"skip_reply": reply_mode == "silent"},
                        )
                    ],
                ),
                model="chat-model",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                finish_reason="tool_calls",
            )
        assert reply_mode == "summary"
        assert llm_call_count == 3
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="agent continuation summary"),
            model="chat-model",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            finish_reason="stop",
        )

    async def fake_generate(**kwargs) -> InternalResponse:
        return build_fake_llm_response(kwargs["messages"], kwargs.get("tools"))

    async def fake_generate_with_stream_callback(**kwargs) -> InternalResponse:
        response = build_fake_llm_response(kwargs["messages"], kwargs.get("tools"))
        if response.message.content:
            await kwargs["on_content"](response.message.content)
        return response

    monkeypatch.setattr(LLMClient, "generate", fake_generate)
    monkeypatch.setattr(LLMClient, "generate_with_stream_callback", fake_generate_with_stream_callback)

    source = "ws" if stream_requested else "http"
    worker_id = f"agent-confirmation-{source}-{reply_mode}"
    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, submitted_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="同意",
            attachments=None,
            source=source,
            stream_requested=stream_requested,
            actor_username="owner",
        )
        assert submitted_work.id is not None
        assert submission_status == "approved"
        assert submitted_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
        assert submitted_work.execution_state["execution_scope"] == "task"
        submitted_frozen_context = await session_reply_work_item_crud.get_frozen_context(db, submitted_work)
        assert submitted_frozen_context
        submitted_work_id = submitted_work.id

    async with confirmation_workflow_session_factory() as db:
        claimed_work = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert claimed_work is not None
    assert claimed_work.id == submitted_work_id
    await executor_lifecycle_module.execute_session_reply_work(claimed_work.id, worker_id)

    async with confirmation_workflow_session_factory() as db:
        suspended_work = await db.get(SessionReplyWorkItem, submitted_work_id)
        background_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
    assert suspended_work is not None
    assert suspended_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert len(background_tasks) == 2
    assert [task.status for task in background_tasks] == [BackgroundTaskStatus.PENDING, BackgroundTaskStatus.PENDING]
    assert commands == []

    task_ids = [task.id for task in background_tasks]
    assert all(task_id is not None for task_id in task_ids)
    original_resume_external_work = runner_module.resume_external_work_for_task

    async def defer_confirmation_resume(task_id: int) -> bool:
        if task_id in task_ids:
            return False
        return await original_resume_external_work(task_id)

    monkeypatch.setattr(runner_module, "resume_external_work_for_task", defer_confirmation_resume)
    for task_id in task_ids:
        assert task_id is not None
        await runner_module.run_background_task(task_id, worker_id=f"{worker_id}-background-{task_id}")

    async with confirmation_workflow_session_factory() as db:
        waiting_work = await db.get(SessionReplyWorkItem, submitted_work_id)
        completed_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
        pre_resume_markers = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.session_id == "session-confirmation",
                        Message.type == MessageType.BACKGROUND_TASK_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert waiting_work is not None
    assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    assert [task.status for task in completed_tasks] == [BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.SUCCEEDED]
    assert pre_resume_markers == []

    foreground_worker = f"{worker_id}-foreground-probe"
    async with confirmation_workflow_session_factory() as foreground_db:
        foreground_work, created = await session_reply_work_item_crud.enqueue(
            foreground_db,
            uid="owner",
            session_id="session-confirmation",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="foreground-probe",
            dedupe_key=f"foreground-probe:{submitted_work_id}",
            commit=False,
        )
        assert created
        await foreground_db.commit()
        assert foreground_work.id is not None
        claimed_foreground = await session_reply_work_item_crud.claim_next(
            foreground_db,
            worker_id=foreground_worker,
            lease_seconds=300,
        )
        assert claimed_foreground is not None
        assert claimed_foreground.id == foreground_work.id
        foreground_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (foreground_work.id, foreground_worker)

        assert task_ids[0] is not None
        assert await external_work_module.resume_external_work_for_task(task_ids[0])
        received_markers = await result_delivery_module.receive_pending_external_results(
            foreground_db,
            uid="owner",
            session_id="session-confirmation",
            profile_id=1,
        )
        assert received_markers == []
        cancelled = await session_reply_work_item_crud.update_claimed(
            foreground_db,
            work_id=foreground_work.id,
            worker_id=foreground_worker,
            values={
                "status": SessionReplyWorkStatus.CANCELLED,
                "locked_by": None,
                "lock_until": None,
            },
            commit=True,
        )
        assert cancelled

    async with confirmation_workflow_session_factory() as db:
        ready_work = await db.get(SessionReplyWorkItem, submitted_work_id)
        cancelled_foreground = await db.get(SessionReplyWorkItem, foreground_work.id)
    assert ready_work is not None
    assert ready_work.status == SessionReplyWorkStatus.READY_FOR_LLM
    assert ready_work.execution_state["external_resume_pending"] is True
    assert cancelled_foreground is not None
    assert cancelled_foreground.status == SessionReplyWorkStatus.CANCELLED

    async with confirmation_workflow_session_factory() as db:
        original_source_work = await db.get(SessionReplyWorkItem, source_work_id)
        assert original_source_work is not None
        await db.delete(original_source_work)
        await db.commit()

    async with confirmation_workflow_session_factory() as db:
        resumed_claim = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
    assert resumed_claim is not None
    assert resumed_claim.id == submitted_work_id
    await executor_lifecycle_module.execute_session_reply_work(resumed_claim.id, worker_id)

    async with confirmation_workflow_session_factory() as verification_db:
        record = await verification_db.get(AuditRecord, audit_record_id)
        source_work = await verification_db.get(SessionReplyWorkItem, record.source_work_id if record is not None else -1)
        confirmed_work = await verification_db.get(SessionReplyWorkItem, submitted_work_id)
        foreground_work = await verification_db.get(SessionReplyWorkItem, foreground_work.id)
        messages = list((await verification_db.execute(select(Message).where(Message.session_id == "session-confirmation").order_by(Message.id))).scalars().all())
        executions = list((await verification_db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id).order_by(AuditExecutionRecord.id))).scalars().all())
        provider_usages = list((await verification_db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.work_id == submitted_work_id, ProviderUsageDetail.type != ProviderUsageType.SCHEDULED_TASK.value).order_by(ProviderUsageDetail.created_at, ProviderUsageDetail.provider_request_id))).scalars().all())
        scheduled_task_provider_usages = list(
            (await verification_db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.work_id == submitted_work_id, ProviderUsageDetail.type == ProviderUsageType.SCHEDULED_TASK.value).order_by(ProviderUsageDetail.created_at, ProviderUsageDetail.provider_request_id))).scalars().all()
        )
        tasks = list((await verification_db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == "session-confirmation").order_by(SessionEvent.id))).scalars().all())
        stream_rows = list((await verification_db.execute(select(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id == submitted_work_id))).scalars().all())
        session = await verification_db.get(ChatSession, "session-confirmation")
        final_message = await verification_db.get(Message, confirmed_work.result_message_id) if confirmed_work and confirmed_work.result_message_id else None
        assert confirmed_work is not None
        background_markers = [message for message in messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
        marker_ids = [message.id for message in background_markers]
        stored_marker_ids = (confirmed_work.execution_state or {}).get("external_tool_result_message_ids")
        assert isinstance(stored_marker_ids, list)
        assert all(isinstance(marker_id, int) and marker_id > 0 for marker_id in marker_ids)
        assert set(stored_marker_ids) == set(marker_ids)
        execution_state_before_status = deepcopy(confirmed_work.execution_state or {})
        work_status = await session_reply_queue_manager.get_work_status(
            verification_db,
            submitted_work_id,
            uid="owner",
        )
        await verification_db.refresh(confirmed_work)
        execution_state_after_status = deepcopy(confirmed_work.execution_state or {})
        model_history = await message_crud.get_history_backward_by_id(
            verification_db,
            session_id="session-confirmation",
            uid="owner",
            limit=100,
        )
        ui_history = await message_crud.get_history_paged(
            verification_db,
            session_id="session-confirmation",
            uid="owner",
            limit=100,
            include_tool_messages=True,
        )

    assert record is not None
    assert source_work is None
    assert record.source_work_id == source_work_id
    assert record.execution_scope == "task"
    assert record.status == AuditRecordStatus.SUCCEEDED
    assert record.decision is not None and record.decision.value == "approve"
    assert record.execution_claim_token is None
    assert confirmed_work is not None
    assert confirmed_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert confirmed_work.execution_state["response"]["history"] == []
    assert confirmed_work.event_sent is (reply_mode == "summary")
    assert foreground_work is not None and foreground_work.status == SessionReplyWorkStatus.CANCELLED
    assert [task.status for task in tasks] == [BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.SUCCEEDED]
    assert all((task.extra or {}).get("origin_work_id") == submitted_work_id for task in tasks)
    assert sorted(commands) == ["echo confirmed-1", "echo confirmed-2"]
    assert len(executions) == 2
    assert [execution.status for execution in executions] == [AuditExecutionStatus.SUCCEEDED, AuditExecutionStatus.SUCCEEDED]
    assert len({execution.new_tool_call_id for execution in executions}) == 2
    assert all(execution.new_tool_call_id.startswith("call_") for execution in executions)

    expected_request_count = 3 if reply_mode == "summary" else 2
    expected_output_increment = 7 if reply_mode == "summary" else 5
    assert not provider_usages
    assert len(scheduled_task_provider_usages) == expected_request_count
    assert all(usage.work_id == submitted_work_id for usage in scheduled_task_provider_usages)
    assert all(usage.session_id == "session-confirmation" and usage.uid == "owner" for usage in scheduled_task_provider_usages)
    assert all(usage.type == ProviderUsageType.SCHEDULED_TASK.value for usage in scheduled_task_provider_usages)
    assert sorted((usage.input_tokens, usage.cached_tokens, usage.output_tokens) for usage in scheduled_task_provider_usages) == sorted([(10, None, 3), *([(10, None, 2)] * (expected_request_count - 1))])
    assert all(usage.cached_tokens is None for usage in scheduled_task_provider_usages)
    usage_input_tokens = sum(usage.input_tokens for usage in scheduled_task_provider_usages)
    usage_output_tokens = sum(usage.output_tokens for usage in scheduled_task_provider_usages)
    assert (usage_input_tokens, usage_output_tokens) == (expected_request_count * 10, expected_output_increment)
    assert session is not None
    assert session.llm_request_metadata == initial_session_metadata
    assert session.llm_request_metadata_work_sequence_no == initial_metadata_work_sequence_no
    assert session.llm_request_metadata_event_sequence_no == initial_metadata_event_sequence_no

    source_message = next(message for message in messages if message.id == record.source_assistant_message_id)
    assert source_message.work_id == source_work_id
    original_user_message = next(message for message in messages if message.content == "Run the proposed shell commands.")
    assert original_user_message.work_id == source_work_id
    original_results = [message for message in messages if message.type == MessageType.TOOL_RESULT and message.audit_record_id == audit_record_id and message.audit_tool_call_id in {"original-shell-call-1", "original-shell-call-2"}]
    assert len(original_results) == 2
    assert all(message.work_id == source_work_id for message in original_results)

    assert len(background_markers) == 2
    assert all(message.work_id == submitted_work_id for message in background_markers)
    assert sorted(json.loads(message.content or "{}")["task_id"] for message in background_markers) == sorted(task_ids)

    continuation_file_calls = []
    continuation_file_results = []
    end_session_calls = []
    end_session_results = []
    for message in messages:
        if message.type == MessageType.TOOL_CALL:
            internal = InternalMessage.model_validate_json(message.content or "{}")
            tool_calls = internal.tool_calls or []
            if any(tool_call.id == "continuation-file-call" for tool_call in tool_calls):
                continuation_file_calls.append(message)
            if isinstance(message.provider_metadata, dict) and message.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME:
                end_session_calls.append(message)
        elif message.type == MessageType.TOOL_RESULT:
            internal = InternalMessage.model_validate_json(message.content or "{}")
            if internal.tool_call_id == "continuation-file-call":
                continuation_file_results.append(message)
            if isinstance(message.provider_metadata, dict) and message.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME:
                end_session_results.append(message)

    assert len(continuation_file_calls) == 1
    assert continuation_file_calls[0].work_id == submitted_work_id
    assert len(continuation_file_results) == 1
    assert continuation_file_results[0].work_id == submitted_work_id
    file_result_payload = json.loads(InternalMessage.model_validate_json(continuation_file_results[0].content or "{}").content or "{}")
    assert file_result_payload["status"] == "success"
    assert "agent-private-marker" in file_result_payload["content"]
    assert len(end_session_calls) == 1
    assert len(end_session_results) == 1
    assert end_session_calls[0].work_id == submitted_work_id
    assert end_session_results[0].work_id == submitted_work_id
    end_session_payload = json.loads(InternalMessage.model_validate_json(end_session_results[0].content or "{}").content or "{}")
    assert end_session_payload["skip_reply"] is (reply_mode == "silent")

    confirmation_card = next(message for message in messages if message.type == MessageType.AUDIT_CONFIRMATION)
    decision_message = next(message for message in messages if message.type == MessageType.AUDIT_DECISION)
    assert confirmation_card.work_id is None
    assert decision_message.work_id is None
    for history in (ui_history, model_history):
        assert all(message.work_id is None for message in history)
        assert all(message.type not in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT, MessageType.BACKGROUND_TASK_RESULT} for message in history)
        assert all(not (isinstance(message.provider_metadata, dict) and message.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME) for message in history)
        assert any(message.content == "仅主会话可见的无关内容" for message in history)
    if reply_mode == "summary":
        assert final_message is not None
        assert final_message.type == MessageType.TEXT
        assert final_message.content == "agent continuation summary"
        assert final_message.work_id is None
        assert any(message.id == final_message.id for message in ui_history)
        assert any(message.id == final_message.id for message in model_history)
        assert len(lifecycle_events) == 1
        assert lifecycle_events[0]["work_id"] == submitted_work_id
        assert lifecycle_events[0]["history"] == []
        assert lifecycle_events[0]["content"] == "agent continuation summary"
        assert "llm_request_metadata" not in confirmed_work.execution_state["response"]
        assert "llm_request_metadata" not in lifecycle_events[0]
    else:
        assert final_message is None
        assert lifecycle_events == []
    assert stream_events == []
    assert not any(event.event.get("type") in {"agent_loop_start", "agent_loop_output", "content", "tool_start", "tool_end", "turn_end"} for event in stream_rows)
    assert not any(event.event.get("type") == "tool_results_update" for event in session_events)
    assert execution_state_after_status == execution_state_before_status
    assert work_status["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert work_status.get("session_events", []) == []
    status_response = work_status["response"]
    assert status_response["history"] == []
    assert all("echo confirmed-" not in json.dumps(item, ensure_ascii=False) for item in status_response["history"])
    assert "llm_request_metadata" not in status_response
    if reply_mode == "summary":
        assert status_response["choices"][0]["message"]["content"] == "agent continuation summary"
    else:
        assert status_response["skip_reply"] is True
    assert forced_recall_requests == []
    assert llm_call_count == expected_request_count

    result_response = await session_reply_queue_manager.wait_for_result(submitted_work_id)
    assert "llm_request_metadata" not in result_response
    completed_stream = [stream_event async for stream_event in session_reply_queue_manager.wait_for_stream(submitted_work_id)]
    resumed_stream = [stream_event async for stream_event in session_reply_queue_manager.wait_for_stream(submitted_work_id, resume_mode=True)]
    for stream_response in (
        next(stream_event["response"] for stream_event in completed_stream if stream_event.get("type") == "done"),
        next(stream_event["response"] for stream_event in resumed_stream if stream_event.get("type") == "done"),
    ):
        assert "llm_request_metadata" not in stream_response
    assert completed_stream[-1]["type"] == "done"
    assert resumed_stream[-1]["type"] == "done"
    assert all(message.content != "仅主会话可见的无关内容" for request in captured_requests for message in request)
    expected_external_results_by_command = {
        "echo confirmed-1": ("original-shell-call-1", "echo confirmed-1\n"),
        "echo confirmed-2": ("original-shell-call-2", "echo confirmed-2\n"),
    }
    expected_external_results = {task.id: expected_external_results_by_command[task.arguments["command"]] for task in tasks if task.id is not None}
    assert set(expected_external_results) == set(task_ids)
    external_tasks_by_id: dict[int, list[dict]] = {}
    for request in captured_requests:
        request_tasks: list[dict] = []
        for message in request:
            if message.role != MessageRole.USER or not isinstance(message.content, str):
                continue
            try:
                outer_payload = json.loads(message.content)
                user_message = outer_payload.get("user_message") if isinstance(outer_payload, dict) else None
                inner_payload = json.loads(user_message) if isinstance(user_message, str) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(inner_payload, dict) or inner_payload.get("type") != "external_work_resume":
                continue
            tasks_payload = inner_payload.get("tasks")
            assert isinstance(tasks_payload, list)
            request_tasks.extend(task for task in tasks_payload if isinstance(task, dict))
        assert request_tasks
        request_task_ids = set()
        for task_entry in request_tasks:
            task_id = task_entry.get("task_id")
            assert isinstance(task_id, int) and task_id in expected_external_results
            request_task_ids.add(task_id)
            external_tasks_by_id.setdefault(task_id, []).append(task_entry)
        assert request_task_ids == set(expected_external_results)

    assert set(external_tasks_by_id) == set(expected_external_results)
    for task_id, task_entries in external_tasks_by_id.items():
        expected_tool_call_id, expected_stdout = expected_external_results[task_id]
        for task_entry in task_entries:
            assert task_entry["execution_status"] == "succeeded"
            assert task_entry["tool_call"]["id"] == expected_tool_call_id
            task_payload = task_entry["task"]
            assert isinstance(task_payload, dict)
            assert task_payload["stdout"] == expected_stdout
            assert task_payload["exit_code"] == 0
