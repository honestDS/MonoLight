import json
from copy import deepcopy
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.audit.confirmation_events as audit_confirmation_events_module
import app.core.audit.persistence as audit_persistence_module
import app.core.audit.service as audit_service_module
import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session.notifier as session_notifier_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
from app.core.audit.confirmation import persist_pending_confirmation_bundle
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    END_SESSION_TOOL_NAME,
    FILE_TOOL_NAME,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    WEB_TOOL_NAME,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT, AUDIT_TASK_AUTHORIZATION_PROMPT
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.audit import AuditConfirmationClaim, AuditExecutionRecord, AuditFailureType, AuditRecord, AuditRecordStatus, AuditToolConclusion, AuditToolDetail
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.llm.client import LLMClient
from tests.integration.scheduled_tasks.scheduled_workflow_support import (
    MODEL_ID,
    PROFILE_ID,
    SESSION_ID,
    UID,
    USAGE,
    _create_due_run_once_scheduled_task,
    db_session,
    session_factory,
)

__all__ = ("db_session", "session_factory")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "run_once"),
    [("create", False), ("create", True), ("delete", False)],
    ids=["create-recurring", "create-one-time", "delete"],
)
async def test_scheduled_task_mutation_waits_for_audit_confirmation(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    run_once: bool,
) -> None:
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["security"].update({"audit_channel_id": 1, "audit_model_id": MODEL_ID, "audit_threshold": 5})
    profile = Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-audit-profile", configs=configs)
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-audit-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            profile,
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.flush()

    scheduled_task_snapshot = None
    if operation == "delete":
        protected_task = ScheduledTask(
            id=1,
            name="Protected task",
            uid=UID,
            session_id=SESSION_ID,
            message="Keep this scheduled request",
            interval_seconds=60,
            next_run_at=get_local_time() + timedelta(seconds=60),
        )
        db_session.add(protected_task)
        await db_session.flush()
        await db_session.refresh(protected_task)
        scheduled_task_snapshot = protected_task.model_dump(mode="json")

    persisted_user_message = Message(
        session_id=SESSION_ID,
        uid=UID,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="管理这个计划任务",
        profile_id=PROFILE_ID,
        is_processed=False,
    )
    db_session.add(persisted_user_message)
    await db_session.commit()
    await db_session.refresh(persisted_user_message)
    persisted_initial_message = InternalMessage(
        id=persisted_user_message.id,
        role=MessageRole.USER,
        content=persisted_user_message.content,
        created_at=persisted_user_message.created_at.timestamp(),
    )

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    async def generate(**kwargs: Any) -> InternalResponse:
        prompt = kwargs["messages"][0].content
        if prompt == AUDIT_BATCH_PROMPT:
            content = json.dumps(
                {
                    "results": [
                        {
                            "tool_call_id": "scheduled-audited-call",
                            "score": 9,
                            "deny": False,
                            "reason": "Persistent scheduled action requires confirmation",
                            "file_checks": [],
                        }
                    ]
                }
            )
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        if prompt == AUDIT_SUMMARY_PROMPT.format(audit_report_language="zh"):
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="计划任务操作将在用户确认后执行。",
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )

        if operation == "create":
            arguments = {
                "operation": "create",
                "name": "Pipeline reminder",
                "message": "Review deployment status",
                "interval_seconds": 60,
            }
            if run_once:
                arguments["run_once"] = True
        else:
            arguments = {"operation": "delete", "task_id": 1}
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="scheduled-audited-call",
                        name=MANAGE_SCHEDULED_TASKS_TOOL_NAME,
                        arguments=arguments,
                    )
                ],
            ),
            model=MODEL_ID,
            usage=USAGE,
            finish_reason="tool_calls",
        )

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", generate)
    response = await ChatDispatcher.dispatch(
        db=db_session,
        message=persisted_user_message.content,
        uid=UID,
        session_id=SESSION_ID,
        session_source="http",
        persisted_initial_message=persisted_initial_message,
        frozen_user_message_ids=[persisted_initial_message.id],
        persisted_profile_id=PROFILE_ID,
        show_tool_calls=True,
    )

    confirmation_card = json.loads(response["choices"][0]["message"]["content"])
    assert confirmation_card["type"] == "audit_confirmation"
    assert confirmation_card["status"] == AuditRecordStatus.PENDING

    async with session_factory() as verification_db:
        audit_records = list((await verification_db.execute(select(AuditRecord).where(AuditRecord.uid == UID, AuditRecord.session_id == SESSION_ID))).scalars().all())
        assert len(audit_records) == 1
        audit_record = audit_records[0]
        assert audit_record.status == AuditRecordStatus.PENDING
        assert audit_record.source == "http"
        assert audit_record.source_assistant_message_id is not None

        details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == audit_record.id))).scalars().all())
        assert len(details) == 1
        detail = details[0]
        assert detail.tool_name == MANAGE_SCHEDULED_TASKS_TOOL_NAME
        assert detail.original_tool_call_id == "scheduled-audited-call"
        assert detail.conclusion == AuditToolConclusion.PENDING
        arguments_summary = json.loads(detail.arguments_summary)
        assert arguments_summary["operation"] == operation
        if operation == "create":
            assert arguments_summary.get("run_once", False) is run_once

        confirmation_messages = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(confirmation_messages) == 1
        tool_results = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.TOOL_RESULT,
                        Message.audit_tool_call_id == "scheduled-audited-call",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(tool_results) == 1
        assert tool_results[0].role == MessageRole.TOOL
        tool_result = InternalMessage.model_validate_json(tool_results[0].content or "")
        assert tool_result.tool_call_id == "scheduled-audited-call"
        assert json.loads(tool_result.content or "")["status"] == AuditRecordStatus.PENDING.value

        scheduled_tasks = list(
            (
                await verification_db.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.uid == UID,
                        ScheduledTask.session_id == SESSION_ID,
                    )
                )
            )
            .scalars()
            .all()
        )
        if operation == "create":
            assert scheduled_tasks == []
        else:
            assert len(scheduled_tasks) == 1
            assert scheduled_tasks[0].model_dump(mode="json") == scheduled_task_snapshot


async def _configure_audit_profile_for_workflow(
    db: AsyncSession,
    tmp_path: Path,
    *,
    threshold: int,
) -> ProfileConfig:
    profile = await db.get(Profile, PROFILE_ID)
    assert profile is not None
    configs = ProfileConfig.model_validate(profile.configs).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["security"].update(
        {
            "audit_channel_id": 1,
            "audit_model_id": MODEL_ID,
            "audit_threshold": threshold,
            "audit_report_language": "zh",
        }
    )
    configs["tool"]["enabled_tools"] = ["execute_shell", WEB_TOOL_NAME]
    configs["tool"]["allowed_operation_dirs"] = [str(tmp_path)]
    profile.configs = configs
    db.add_all(
        [
            ModelChannel(
                id=1,
                name="scheduled-task-audit-policy-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            profile,
        ]
    )
    await db.commit()
    return ProfileConfig.model_validate(configs)


async def _persist_main_pending_confirmation(
    db: AsyncSession,
    *,
    cfg: ProfileConfig,
    working_directory: Path,
    tool_call: InternalToolCall,
) -> tuple[InternalMessage, Any]:
    source_message = await save_assistant_message(
        db,
        SESSION_ID,
        UID,
        PROFILE_ID,
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
    )
    assert source_message.id is not None
    pending = await audit_service_module.audit_tool_round(
        db,
        cfg=cfg,
        tool_calls=[tool_call],
        source_assistant_message_id=source_message.id,
        uid=UID,
        operator_username=UID,
        session_id=SESSION_ID,
        source="http",
        language="zh",
        execution_scope="foreground",
        working_directory=working_directory,
    )
    assert pending is not None
    assert pending.status is AuditRecordStatus.PENDING
    assert pending.confirmation_payload is not None
    await persist_pending_confirmation_bundle(
        db,
        audit_record_id=pending.audit_record_id,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        tool_results=pending.tool_results,
        confirmation_payload=pending.confirmation_payload,
        dedupe_key=None,
    )
    return source_message, pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "threshold", "evidence_missing", "deny", "include_web", "model_error", "expected_status"),
    [
        pytest.param(4, 5, False, False, False, False, AuditRecordStatus.PASSED, id="score-below-threshold"),
        pytest.param(5, 5, False, False, True, False, AuditRecordStatus.BLOCKED, id="threshold-mixed-round"),
        pytest.param(9, 5, False, False, False, False, AuditRecordStatus.BLOCKED, id="high-risk-score"),
        pytest.param(7, 0, False, False, False, False, AuditRecordStatus.PASSED, id="threshold-disabled-safe-score"),
        pytest.param(8, 0, False, False, False, False, AuditRecordStatus.BLOCKED, id="high-risk-without-threshold"),
        pytest.param(0, 5, True, False, False, False, AuditRecordStatus.BLOCKED, id="insufficient-file-evidence"),
        pytest.param(0, 5, False, True, False, False, AuditRecordStatus.BLOCKED, id="model-denial"),
        pytest.param(None, 5, False, False, False, True, AuditRecordStatus.AUDIT_FAILED, id="audit-service-error"),
    ],
)
async def test_scheduled_agent_work_audit_policy_isolated_from_main_confirmation(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    score: int | None,
    threshold: int,
    evidence_missing: bool,
    deny: bool,
    include_web: bool,
    model_error: bool,
    expected_status: AuditRecordStatus,
) -> None:
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    cfg = await _configure_audit_profile_for_workflow(db_session, tmp_path, threshold=threshold)
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(session_notifier_module, "AsyncSessionLocal", session_factory)

    async def send_session_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_confirmation_events_module, "send_session_event", send_session_event)
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    main_call = InternalToolCall(
        id="main-pending-call",
        name="execute_shell",
        arguments={"command": "echo main confirmation"},
    )
    scheduled_call = InternalToolCall(
        id="scheduled-audit-call",
        name="execute_shell",
        arguments={"command": "echo scheduled action"},
    )
    web_call = InternalToolCall(
        id="scheduled-web-call",
        name=WEB_TOOL_NAME,
        arguments={"operation": "search", "query": "scheduled status"},
    )

    async def call_auditor(
        _db: AsyncSession,
        _cfg: ProfileConfig,
        request_payload: dict[str, Any],
        _working_directory: Path,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        request_tool_calls = request_payload["tool_calls"]
        request_ids = [item["tool_call_id"] for item in request_tool_calls]
        if request_ids == [main_call.id]:
            result = {
                "tool_call_id": main_call.id,
                "score": 8,
                "deny": False,
                "reason": "main confirmation reason",
                "file_checks": [],
            }
            file_reads: list[dict[str, Any]] = []
        else:
            assert request_ids == [scheduled_call.id]
            if model_error:
                raise RuntimeError("simulated scheduled audit failure")
            result = {
                "tool_call_id": scheduled_call.id,
                "score": score,
                "deny": deny,
                "reason": "scheduled model reason",
                "file_checks": [],
            }
            missing_path = (tmp_path / "missing-evidence.txt").resolve()
            file_reads = (
                [
                    {
                        "tool_call_id": scheduled_call.id,
                        "original_path": "missing-evidence.txt",
                        "absolute_path": str(missing_path),
                        "resolved_path": str(missing_path),
                        "exists": False,
                        "file_type": "missing",
                        "status": "missing",
                        "size": None,
                        "sha256": None,
                        "truncated": False,
                        "bytes_read": 0,
                    }
                ]
                if evidence_missing
                else []
            )
        return {"messages": []}, {"parsed": {"results": [result]}, "file_reads": file_reads, "shell_results": []}

    async def summarize_pending(*_args: Any, **_kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "测试确认摘要", {"request": [], "response": "测试确认摘要", "file_reads": [], "shell_results": []}

    monkeypatch.setattr(audit_service_module, "_call_auditor", call_auditor)
    monkeypatch.setattr(audit_service_module, "_summarize_pending", summarize_pending)

    main_source_message, main_pending = await _persist_main_pending_confirmation(
        db_session,
        cfg=cfg,
        working_directory=tmp_path,
        tool_call=main_call,
    )
    main_record = await db_session.get(AuditRecord, main_pending.audit_record_id)
    assert main_record is not None
    main_claim = (await db_session.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == main_pending.audit_record_id))).scalars().one()
    main_card = (
        (
            await db_session.execute(
                select(Message).where(
                    Message.uid == UID,
                    Message.session_id == SESSION_ID,
                    Message.type == MessageType.AUDIT_CONFIRMATION,
                )
            )
        )
        .scalars()
        .one()
    )
    assert json.loads(main_card.content or "{}")["audit_record_id"] == main_pending.audit_record_id
    main_tool_result = (
        (
            await db_session.execute(
                select(Message).where(
                    Message.audit_record_id == main_pending.audit_record_id,
                    Message.type == MessageType.TOOL_RESULT,
                    Message.audit_tool_call_id == main_call.id,
                )
            )
        )
        .scalars()
        .one()
    )
    main_source_row = await db_session.get(Message, main_source_message.id)
    assert main_source_row is not None
    assert main_source_row.role is MessageRole.ASSISTANT
    assert main_source_row.type is MessageType.TOOL_CALL
    assert main_source_row.work_id is None
    assert json.loads(main_source_row.content or "{}")["tool_calls"][0]["id"] == main_call.id

    def message_snapshot(message: Message) -> dict[str, Any]:
        snapshot = message.model_dump(mode="json", exclude={"content_format"})
        snapshot["content_format"] = message.content_format
        return snapshot

    main_snapshot = {
        "record": main_record.model_dump(mode="json"),
        "claim": main_claim.model_dump(mode="json"),
        "card": message_snapshot(main_card),
        "tool_result": message_snapshot(main_tool_result),
        "source": message_snapshot(main_source_row),
    }

    await scheduler_module.ScheduledTaskScheduler()._dispatch_one_with_db(db_session, scheduled_task)
    scheduled_work = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    SessionReplyWorkItem.session_id == SESSION_ID,
                )
            )
        )
        .scalars()
        .one()
    )
    assert scheduled_work.id is not None
    worker_id = "scheduled-audit-policy-worker"
    claimed_work = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed_work is not None
    assert claimed_work.id == scheduled_work.id
    assert claimed_work.source_type is SessionReplySourceType.SCHEDULED_TASK_RUN
    assert claimed_work.status is SessionReplyWorkStatus.RUNNING
    assert claimed_work.locked_by == worker_id
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (scheduled_work.id, worker_id)

    scheduled_tool_calls = [scheduled_call, web_call] if include_web else [scheduled_call]
    scheduled_source_message = await save_assistant_message(
        db_session,
        SESSION_ID,
        UID,
        PROFILE_ID,
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=scheduled_tool_calls),
    )
    assert scheduled_source_message.id is not None
    scheduled_result = await audit_service_module.audit_tool_round(
        db_session,
        cfg=cfg,
        tool_calls=scheduled_tool_calls,
        source_assistant_message_id=scheduled_source_message.id,
        uid=UID,
        operator_username=UID,
        session_id=SESSION_ID,
        source="scheduled_task",
        language="zh",
        execution_scope=END_SESSION_SCOPE_TASK,
        working_directory=tmp_path,
    )
    assert scheduled_result is not None
    assert scheduled_result.status is expected_status
    assert scheduled_result.may_execute is (expected_status is AuditRecordStatus.PASSED)

    if expected_status is AuditRecordStatus.PASSED:
        assert scheduled_result.tool_results == ()
        assert scheduled_result.confirmation_payload is None
    else:
        assert scheduled_result.confirmation_payload is None
        assert scheduled_result.tool_results
        assert [message.tool_call_id for message in scheduled_result.tool_results] == [call.id for call in scheduled_tool_calls]
        result_payloads = [json.loads(message.content or "{}") for message in scheduled_result.tool_results]
        assert [payload["status"] for payload in result_payloads] == [expected_status.value] * len(scheduled_tool_calls)
        assert all("score" not in payload and "reason" not in payload for payload in result_payloads)
        assert all(payload["status"] not in {"rejected", "userdecision"} for payload in result_payloads)

    async with session_factory() as verification_db:
        records = list((await verification_db.execute(select(AuditRecord).where(AuditRecord.uid == UID, AuditRecord.session_id == SESSION_ID).order_by(AuditRecord.id))).scalars().all())
        assert len(records) == 2
        persisted_main = await verification_db.get(AuditRecord, main_pending.audit_record_id)
        scheduled_record = next(record for record in records if record.source_work_id == scheduled_work.id)
        scheduled_details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == scheduled_record.id).order_by(AuditToolDetail.turn_index))).scalars().all())
        claims = list((await verification_db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.uid == UID, AuditConfirmationClaim.session_id == SESSION_ID))).scalars().all())
        cards = list((await verification_db.execute(select(Message).where(Message.uid == UID, Message.session_id == SESSION_ID, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().all())
        persisted_main_tool_result = (
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.audit_record_id == main_pending.audit_record_id,
                        Message.type == MessageType.TOOL_RESULT,
                        Message.audit_tool_call_id == main_call.id,
                    )
                )
            )
            .scalars()
            .one()
        )
        persisted_main_claim = (await verification_db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == main_pending.audit_record_id))).scalars().one()
        persisted_main_source = await verification_db.get(Message, main_source_message.id)
        scheduled_confirmation_messages = [card for card in cards if json.loads(card.content or "{}").get("audit_record_id") == scheduled_record.id]
        scheduled_tool_result_messages = list(
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.audit_record_id == scheduled_record.id,
                        Message.type == MessageType.TOOL_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
        persisted_scheduled_source = await verification_db.get(Message, scheduled_source_message.id)

        assert persisted_main is not None
        assert persisted_main.model_dump(mode="json") == main_snapshot["record"]
        assert [claim.audit_record_id for claim in claims] == [main_pending.audit_record_id]
        assert persisted_main_claim.model_dump(mode="json") == main_snapshot["claim"]
        assert len(cards) == 1
        assert message_snapshot(cards[0]) == main_snapshot["card"]
        assert message_snapshot(persisted_main_tool_result) == main_snapshot["tool_result"]
        assert persisted_main_source is not None
        assert message_snapshot(persisted_main_source) == main_snapshot["source"]
        assert persisted_scheduled_source is not None
        assert persisted_scheduled_source.work_id == scheduled_work.id
        assert scheduled_confirmation_messages == []
        assert scheduled_tool_result_messages == []
        assert scheduled_record.execution_scope == END_SESSION_SCOPE_TASK
        assert scheduled_record.status is expected_status
        assert scheduled_record.pending_at is None
        assert scheduled_record.expires_at is None
        assert scheduled_record.intent_summary is None
        assert scheduled_record.context_file_path is not None
        assert Path(scheduled_record.context_file_path).is_file()
        assert len(scheduled_details) == len(scheduled_tool_calls)
        assert scheduled_details[0].score == score
        if model_error:
            assert scheduled_record.failure_type is AuditFailureType.AUDIT_SERVICE_FAILED
            assert scheduled_details[0].conclusion is AuditToolConclusion.AUDIT_FAILED
            assert scheduled_details[0].reason == "simulated scheduled audit failure"
        else:
            assert scheduled_details[0].conclusion is (AuditToolConclusion.PASSED if expected_status is AuditRecordStatus.PASSED else AuditToolConclusion.BLOCKED)
            assert scheduled_details[0].reason.startswith("scheduled model reason")
            if evidence_missing:
                assert scheduled_details[0].file_snapshots[0]["status"] == "missing"
        if include_web:
            assert scheduled_details[1].original_tool_call_id == web_call.id
            assert scheduled_details[1].conclusion is AuditToolConclusion.PASSED
            assert scheduled_details[1].score == 0


@pytest.mark.asyncio
async def test_non_scheduled_agent_work_task_scope_keeps_pending_confirmation_and_cancels_old_one(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _create_due_run_once_scheduled_task(db_session)
    cfg = await _configure_audit_profile_for_workflow(db_session, tmp_path, threshold=5)
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)

    async def send_session_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_confirmation_events_module, "send_session_event", send_session_event)
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    main_call = InternalToolCall(id="ordinary-old-pending-call", name="execute_shell", arguments={"command": "echo old"})
    ordinary_call = InternalToolCall(id="ordinary-task-call", name="execute_shell", arguments={"command": "echo ordinary"})

    async def call_auditor(
        _db: AsyncSession,
        _cfg: ProfileConfig,
        request_payload: dict[str, Any],
        _working_directory: Path,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        call_id = request_payload["tool_calls"][0]["tool_call_id"]
        return (
            {"messages": []},
            {
                "parsed": {
                    "results": [
                        {
                            "tool_call_id": call_id,
                            "score": 8 if call_id == main_call.id else 5,
                            "deny": False,
                            "reason": "ordinary model reason",
                            "file_checks": [],
                        }
                    ]
                },
                "file_reads": [],
                "shell_results": [],
            },
        )

    async def summarize_pending(*_args: Any, **_kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "普通工作确认摘要", {"request": [], "response": "普通工作确认摘要", "file_reads": [], "shell_results": []}

    monkeypatch.setattr(audit_service_module, "_call_auditor", call_auditor)
    monkeypatch.setattr(audit_service_module, "_summarize_pending", summarize_pending)
    _main_source_message, main_pending = await _persist_main_pending_confirmation(
        db_session,
        cfg=cfg,
        working_directory=tmp_path,
        tool_call=main_call,
    )

    ordinary_work, created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        source_type=SessionReplySourceType.TOOL_RESULT,
        source_id="ordinary-tool-result-source",
        dedupe_key="ordinary-tool-result-agent-work",
        spec=AgentWorkSpec(
            instruction="普通独立工作",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
    )
    assert created is True
    assert ordinary_work.id is not None
    assert ordinary_work.execution_state["execution_scope"] == END_SESSION_SCOPE_TASK
    ordinary_worker_id = "ordinary-task-audit-worker"
    claimed_work = await session_reply_work_item_crud.claim_next(db_session, worker_id=ordinary_worker_id, lease_seconds=300)
    assert claimed_work is not None
    assert claimed_work.id == ordinary_work.id
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (ordinary_work.id, ordinary_worker_id)
    source_message = await save_assistant_message(
        db_session,
        SESSION_ID,
        UID,
        PROFILE_ID,
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[ordinary_call]),
    )
    assert source_message.id is not None

    result = await audit_service_module.audit_tool_round(
        db_session,
        cfg=cfg,
        tool_calls=[ordinary_call],
        source_assistant_message_id=source_message.id,
        uid=UID,
        operator_username=UID,
        session_id=SESSION_ID,
        source="tool_result",
        language="zh",
        execution_scope=END_SESSION_SCOPE_TASK,
        working_directory=tmp_path,
    )
    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["status"] == AuditRecordStatus.PENDING.value
    assert len(result.tool_results) == 1
    assert json.loads(result.tool_results[0].content or "{}")["status"] == AuditRecordStatus.PENDING.value

    async with session_factory() as verification_db:
        old_record = await verification_db.get(AuditRecord, main_pending.audit_record_id)
        ordinary_record = (await verification_db.execute(select(AuditRecord).where(AuditRecord.source_work_id == ordinary_work.id))).scalars().one()
        old_claims = list((await verification_db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == main_pending.audit_record_id))).scalars().all())
        ordinary_details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == ordinary_record.id))).scalars().all())
        old_card = (
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.uid == UID,
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .one()
        )
        old_tool_result = (
            (
                await verification_db.execute(
                    select(Message).where(
                        Message.audit_record_id == main_pending.audit_record_id,
                        Message.type == MessageType.TOOL_RESULT,
                        Message.audit_tool_call_id == main_call.id,
                    )
                )
            )
            .scalars()
            .one()
        )

        assert old_record is not None
        assert old_record.status is AuditRecordStatus.CANCELLED
        assert old_claims == []
        assert json.loads(old_card.content or "{}")["status"] == AuditRecordStatus.CANCELLED.value
        assert json.loads(InternalMessage.model_validate_json(old_tool_result.content or "").content or "{}")["status"] == AuditRecordStatus.CANCELLED.value
        assert ordinary_record.source_work_id == ordinary_work.id
        assert ordinary_record.execution_scope == END_SESSION_SCOPE_TASK
        assert ordinary_record.status is AuditRecordStatus.PENDING
        assert ordinary_record.pending_at is not None
        assert ordinary_record.expires_at is not None
        assert ordinary_record.intent_summary == "普通工作确认摘要"
        assert len(ordinary_details) == 1
        assert ordinary_details[0].conclusion is AuditToolConclusion.PENDING
        assert ordinary_details[0].score == 5
        assert ordinary_details[0].reason == "ordinary model reason"


@pytest.mark.asyncio
async def test_scheduled_task_audit_block_then_safe_read_finishes_with_original_notification(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "scheduled-audit-input.txt"
    output_file = tmp_path / "scheduled-audit-output.txt"
    input_file.write_text("SAFE_READ_MARKER", encoding="utf-8")
    assert not output_file.exists()

    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    scheduled_task_instruction = scheduled_task.message
    await _configure_audit_profile_for_workflow(db_session, tmp_path, threshold=5)
    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    profile_configs = deepcopy(profile.configs)
    profile_configs["tool"].update(
        {
            "enabled_tools": [FILE_TOOL_NAME],
            "allowed_operation_dirs": [str(tmp_path)],
        }
    )
    profile.configs = profile_configs
    db_session.add(profile)
    await db_session.commit()

    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    write_call = InternalToolCall(
        id="scheduled-write-call",
        name=FILE_TOOL_NAME,
        arguments={
            "operation": "write",
            "path": str(output_file),
            "content": "THIS_WRITE_MUST_NOT_RUN",
        },
    )
    first_read_call = InternalToolCall(
        id="scheduled-first-read-call",
        name=FILE_TOOL_NAME,
        arguments={"operation": "read", "path": str(input_file)},
    )
    second_read_call = InternalToolCall(
        id="scheduled-second-read-call",
        name=FILE_TOOL_NAME,
        arguments={"operation": "read", "path": str(input_file)},
    )
    end_call = InternalToolCall(
        id="scheduled-end-call",
        name=END_SESSION_TOOL_NAME,
        arguments={"skip_reply": False},
    )
    final_text = "AI总结：写入未执行，只读核查已完成。"
    audit_calls: list[dict[str, Any]] = []
    business_calls: list[dict[str, Any]] = []
    summary_calls = 0

    def tool_result_payload(messages: list[InternalMessage], tool_call_id: str) -> dict[str, Any]:
        matching = [message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id == tool_call_id]
        assert len(matching) == 1
        payload = json.loads(matching[0].content or "{}")["tool_result"]
        assert isinstance(payload, dict)
        return payload

    def assert_blocked_feedback(payload: dict[str, Any]) -> None:
        assert payload["status"] == AuditRecordStatus.BLOCKED.value
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        assert "score" not in payload
        assert "reason" not in payload
        assert not any(term in serialized for term in ("pending", "rejected", "userdecision", "等待确认", "用户拒绝", "评分依据"))

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        nonlocal summary_calls
        messages: list[InternalMessage] = kwargs["messages"]
        system_messages = [message for message in messages if message.role == MessageRole.SYSTEM]
        first_system = system_messages[0].content if system_messages else None
        if first_system == AUDIT_BATCH_PROMPT + "\n\n" + AUDIT_TASK_AUTHORIZATION_PROMPT:
            audit_calls.append(kwargs)
            audit_payload = json.loads(next(message.content for message in messages if message.role == MessageRole.USER))
            assert audit_payload["task_instruction"] == scheduled_task_instruction
            audited_call_ids = [item["tool_call_id"] for item in audit_payload["tool_calls"]]
            if audited_call_ids == [write_call.id, first_read_call.id]:
                results = [
                    {
                        "tool_call_id": write_call.id,
                        "score": 5,
                        "deny": False,
                        "reason": "writing a target file requires confirmation",
                        "file_checks": [],
                    },
                    {
                        "tool_call_id": first_read_call.id,
                        "score": 0,
                        "deny": False,
                        "reason": "read-only access is safe",
                        "file_checks": [],
                    },
                ]
            elif audited_call_ids == [second_read_call.id]:
                results = [
                    {
                        "tool_call_id": second_read_call.id,
                        "score": 0,
                        "deny": False,
                        "reason": "read-only access is safe",
                        "file_checks": [],
                    }
                ]
            else:
                raise AssertionError(f"Unexpected audited tool calls: {audited_call_ids}")
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content=json.dumps({"results": results}),
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        if first_system == AUDIT_SUMMARY_PROMPT.format(audit_report_language="zh"):
            summary_calls += 1
            raise AssertionError("scheduled blocked audit must not invoke the pending summary model")

        business_calls.append(kwargs)
        turn = len(business_calls)
        if turn == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[write_call, first_read_call],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 2:
            write_result = tool_result_payload(messages, write_call.id)
            first_read_result = tool_result_payload(messages, first_read_call.id)
            assert_blocked_feedback(write_result)
            assert_blocked_feedback(first_read_result)
            assert not output_file.exists()
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[second_read_call]),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 3:
            second_read_result = tool_result_payload(messages, second_read_call.id)
            assert second_read_result["status"] == "success"
            assert "SAFE_READ_MARKER" in json.dumps(second_read_result, ensure_ascii=False)
            assert not output_file.exists()
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[end_call]),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 4:
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content=final_text),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected business model call {turn}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    assert scheduled_task.id is not None
    await scheduler_module.ScheduledTaskScheduler()._dispatch_one(scheduled_task.id)
    worker_id = "scheduled-audit-chain-worker"
    async with session_factory() as claim_db:
        work = (
            (
                await claim_db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == SESSION_ID,
                        SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    )
                )
            )
            .scalars()
            .one()
        )
        assert work.id is not None
        work_id = work.id
        claimed = await session_reply_work_item_crud.claim_next(claim_db, worker_id=worker_id, lease_seconds=300)
        assert claimed is not None
        assert claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, worker_id)

    async with session_factory() as verification_db:
        persisted_work = await verification_db.get(SessionReplyWorkItem, work_id)
        assert persisted_work is not None
        assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert persisted_work.result_message_id is not None
        final_message = await verification_db.get(Message, persisted_work.result_message_id)
        assert final_message is not None
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())
        audit_records = list((await verification_db.execute(select(AuditRecord).where(AuditRecord.source_work_id == work_id).order_by(AuditRecord.id.asc()))).scalars().all())
        assert len(audit_records) == 2
        audit_claims = list((await verification_db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.uid == UID, AuditConfirmationClaim.session_id == SESSION_ID))).scalars().all())
        confirmation_cards = list((await verification_db.execute(select(Message).where(Message.uid == UID, Message.session_id == SESSION_ID, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().all())
        details = list((await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id.in_([record.id for record in audit_records])).order_by(AuditToolDetail.audit_record_id, AuditToolDetail.turn_index))).scalars().all())
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=SESSION_ID, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=SESSION_ID, uid=UID)
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())
        scheduled_task_row = await verification_db.get(ScheduledTask, scheduled_task.id)

    assert len(audit_calls) == 2
    assert summary_calls == 0
    assert len(business_calls) == 4
    assert final_message.content == final_text
    assert final_message.type == MessageType.TEXT
    assert final_message.work_id is None
    assert not output_file.exists()
    assert input_file.read_text(encoding="utf-8") == "SAFE_READ_MARKER"

    assert len(audit_records) == 2
    first_audit, second_audit = audit_records
    assert first_audit.status == AuditRecordStatus.BLOCKED
    assert second_audit.status == AuditRecordStatus.SUCCEEDED
    assert all(record.execution_scope == END_SESSION_SCOPE_TASK for record in audit_records)
    assert all(record.source_work_id == work_id for record in audit_records)
    assert all(record.intent_summary is None for record in audit_records)
    assert all(record.context_file_path is not None and Path(record.context_file_path).is_file() for record in audit_records)
    first_details = [detail for detail in details if detail.audit_record_id == first_audit.id]
    second_details = [detail for detail in details if detail.audit_record_id == second_audit.id]
    assert len(first_details) == 2
    assert len(second_details) == 1
    first_details_by_call_id = {detail.original_tool_call_id: detail for detail in first_details}
    assert first_details_by_call_id[write_call.id].conclusion == AuditToolConclusion.BLOCKED
    assert first_details_by_call_id[write_call.id].score == 5
    assert first_details_by_call_id[first_read_call.id].conclusion == AuditToolConclusion.PASSED
    assert first_details_by_call_id[first_read_call.id].score == 0
    assert second_details[0].original_tool_call_id == second_read_call.id
    assert second_details[0].conclusion == AuditToolConclusion.PASSED
    assert second_details[0].score == 0
    assert audit_claims == []
    assert confirmation_cards == []

    tool_messages = [message for message in persisted_messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]
    assert tool_messages
    assert all(message.work_id == work_id for message in tool_messages)
    control_messages = [message for message in persisted_messages if isinstance(message.provider_metadata, dict) and message.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME]
    assert len(control_messages) == 2
    control_call = next(message for message in control_messages if message.type == MessageType.TOOL_CALL)
    control_result = next(message for message in control_messages if message.type == MessageType.TOOL_RESULT)
    control_call_payload = InternalMessage.model_validate_json(control_call.content or "{}")
    assert control_call_payload.tool_calls is not None
    assert control_call_payload.tool_calls[0].id == end_call.id
    assert control_call_payload.tool_calls[0].arguments == {"skip_reply": False}
    control_result_payload = json.loads(InternalMessage.model_validate_json(control_result.content or "{}").content or "{}")
    assert control_result_payload == {
        "status": "completed",
        "skip_reply": False,
        "received_result_task_ids": [],
    }
    assert [message.content for message in reversed(model_history)] == [final_text]
    assert [message.content for message in reversed(ui_history)] == [final_text]
    assert all(message.type not in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT} for message in [*model_history, *ui_history])

    assert scheduled_task_row is not None
    assert scheduled_task_row.status == ScheduledTaskStatus.DISABLED
    assert scheduled_task_row.run_count == 1
    assert len(session_events) == 1
    final_event = session_events[0].event
    assert final_event["type"] == "proactive_reply"
    assert final_event["work_id"] == work_id
    assert final_event["content"] == final_text
    assert final_event["message_id"] == final_message.id
    assert final_event["history"] == []
    assert final_event["files"] == []
    assert all(event.event.get("type") != "tool_results_update" for event in session_events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root_source_type",
    [
        pytest.param(SessionReplySourceType.SCHEDULED_TASK_RUN, id="scheduled-root"),
        pytest.param(SessionReplySourceType.TOOL_RESULT, id="ordinary-root"),
    ],
)
async def test_confirmation_continuation_reuses_root_agent_work_audit_policy(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_source_type: SessionReplySourceType,
) -> None:
    await _create_due_run_once_scheduled_task(db_session)
    cfg = await _configure_audit_profile_for_workflow(db_session, tmp_path, threshold=5)
    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    async def send_session_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(audit_confirmation_events_module, "send_session_event", send_session_event)
    monkeypatch.setattr(
        audit_service_module,
        "persist_prepared_audit_round",
        partial(audit_persistence_module.persist_prepared_audit_round, audit_root=tmp_path / "audit"),
    )

    original_call = InternalToolCall(
        id="legacy-root-audit-call",
        name="execute_shell",
        arguments={"command": "echo legacy-root", "execution_mode": "non_interactive"},
    )
    probe_call = InternalToolCall(
        id="continuation-isolation-probe-call",
        name="execute_shell",
        arguments={"command": "echo probe", "execution_mode": "non_interactive"},
    )
    followup_call = InternalToolCall(
        id="continuation-followup-call",
        name="execute_shell",
        arguments={"command": "echo followup", "execution_mode": "non_interactive"},
    )
    root_instruction = "Write two complete scripts without running them: scripts/prepare_report.py must read input.json, normalize each record, and write report.json; scripts/validate_report.py must read report.json, validate required fields, and write validation.json. Do not execute either script."
    summary_calls = 0

    async def call_auditor(
        _db: AsyncSession,
        _cfg: ProfileConfig,
        request_payload: dict[str, Any],
        _working_directory: Path,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        call_ids = [item["tool_call_id"] for item in request_payload["tool_calls"]]
        assert len(call_ids) == 1
        call_id = call_ids[0]
        assert call_id in {original_call.id, probe_call.id, followup_call.id}
        if call_id in {original_call.id, probe_call.id}:
            assert "task_instruction" not in request_payload
        else:
            assert request_payload["task_instruction"] == root_instruction
        return (
            {"messages": []},
            {
                "parsed": {
                    "results": [
                        {
                            "tool_call_id": call_id,
                            "score": 5,
                            "deny": False,
                            "reason": "requires confirmation",
                            "file_checks": [],
                        }
                    ]
                },
                "file_reads": [],
                "shell_results": [],
            },
        )

    async def summarize_pending(
        _db: AsyncSession,
        _cfg: ProfileConfig,
        tool_calls: list[dict[str, Any]],
        *_args: Any,
        task_instruction: str | None = None,
        **_kwargs: Any,
    ) -> tuple[str, dict[str, Any]]:
        nonlocal summary_calls
        summary_calls += 1
        assert len(tool_calls) == 1
        call_id = tool_calls[0]["id"]
        assert call_id in {original_call.id, probe_call.id, followup_call.id}
        if call_id in {original_call.id, probe_call.id}:
            assert task_instruction is None
        else:
            assert root_source_type == SessionReplySourceType.TOOL_RESULT
            assert task_instruction == root_instruction
        return "待确认摘要", {"request": [], "response": "待确认摘要", "file_reads": [], "shell_results": []}

    monkeypatch.setattr(audit_service_module, "_call_auditor", call_auditor)
    monkeypatch.setattr(audit_service_module, "_summarize_pending", summarize_pending)

    original_source_message, original_pending = await _persist_main_pending_confirmation(
        db_session,
        cfg=cfg,
        working_directory=tmp_path,
        tool_call=original_call,
    )
    assert original_source_message.id is not None
    original_record = await db_session.get(AuditRecord, original_pending.audit_record_id)
    assert original_record is not None
    original_source_row = await db_session.get(Message, original_source_message.id)
    assert original_source_row is not None
    original_tool_result_row = (
        (
            await db_session.execute(
                select(Message).where(
                    Message.audit_record_id == original_record.id,
                    Message.audit_tool_call_id == original_call.id,
                    Message.type == MessageType.TOOL_RESULT,
                )
            )
        )
        .scalars()
        .one()
    )
    original_detail = (await db_session.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == original_record.id))).scalars().one()
    original_round_hash = original_record.round_arguments_hash
    original_arguments_hash = original_detail.arguments_hash
    original_reason = original_detail.reason
    original_source_message_id = original_record.source_assistant_message_id
    source_internal = InternalMessage.model_validate_json(original_source_row.content or "{}")
    tool_result_internal = InternalMessage.model_validate_json(original_tool_result_row.content or "{}")

    root_work, created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile_id=PROFILE_ID,
        source_type=root_source_type,
        source_id=f"legacy-root:{root_source_type.value}",
        dedupe_key=f"legacy-confirmation-root:{root_source_type.value}",
        spec=AgentWorkSpec(
            instruction=root_instruction,
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    assert created is True
    assert root_work.id is not None
    checkpoint = {
        "messages": [
            source_internal.model_dump(mode="json", exclude_none=True),
            tool_result_internal.model_dump(mode="json", exclude_none=True),
        ],
        "turn_messages": [
            source_internal.model_dump(mode="json", exclude_none=True),
            tool_result_internal.model_dump(mode="json", exclude_none=True),
        ],
        "current_turn": 1,
        "reload_history": False,
    }
    root_work.execution_state = {
        **(root_work.execution_state or {}),
        "frozen_context_message_ids": [],
    }
    root_work.status = SessionReplyWorkStatus.SUCCEEDED
    root_work.locked_by = None
    root_work.lock_until = None
    original_record.source_work_id = root_work.id
    original_record.execution_scope = END_SESSION_SCOPE_TASK
    original_source_row.work_id = root_work.id
    original_tool_result_row.work_id = root_work.id
    db_session.add_all([root_work, original_record, original_source_row, original_tool_result_row])
    await db_session.flush()
    await session_reply_work_item_crud.save_checkpoint(db_session, root_work.id, checkpoint)
    await db_session.commit()

    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    _decision_message, confirmed_work, submission_status, _events = await scheduler_module.session_reply_queue_manager.submit_user_message(
        db_session,
        uid=UID,
        session_id=SESSION_ID,
        profile=profile,
        message="同意",
        attachments=None,
        source="http",
        stream_requested=False,
        context_summary_events_requested=False,
    )
    assert submission_status == "approved"
    assert confirmed_work.id is not None
    assert confirmed_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert confirmed_work.source_type == SessionReplySourceType.AUDIT_RECORD
    assert confirmed_work.source_id == str(original_record.id)
    assert confirmed_work.execution_state["execution_scope"] == END_SESSION_SCOPE_TASK
    confirmed_work_id = confirmed_work.id

    original_record = await db_session.get(AuditRecord, original_record.id)
    assert original_record is not None
    await db_session.refresh(original_record)
    assert original_record.status == AuditRecordStatus.EXECUTING
    original_tool_result_row = await db_session.get(Message, original_tool_result_row.id)
    assert original_tool_result_row is not None
    await db_session.refresh(original_tool_result_row)
    approved_tool_result_payload = json.loads(InternalMessage.model_validate_json(original_tool_result_row.content or "{}").content or "{}")
    assert approved_tool_result_payload["status"] == AuditRecordStatus.EXECUTING.value

    _probe_source_message, probe_pending = await _persist_main_pending_confirmation(
        db_session,
        cfg=cfg,
        working_directory=tmp_path,
        tool_call=probe_call,
    )

    worker_id = f"continuation-audit-worker-{root_source_type.value}"
    claimed_work = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed_work is not None
    assert claimed_work.id == confirmed_work_id
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (confirmed_work_id, worker_id)
    followup_source_message = await save_assistant_message(
        db_session,
        SESSION_ID,
        UID,
        PROFILE_ID,
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[followup_call]),
    )
    assert followup_source_message.id is not None
    followup_source_row = await db_session.get(Message, followup_source_message.id)
    assert followup_source_row is not None
    assert followup_source_row.work_id == confirmed_work_id
    followup_result = await audit_service_module.audit_tool_round(
        db_session,
        cfg=cfg,
        tool_calls=[followup_call],
        source_assistant_message_id=followup_source_message.id,
        uid=UID,
        operator_username=UID,
        session_id=SESSION_ID,
        source="http",
        language="zh",
        execution_scope=END_SESSION_SCOPE_TASK,
        working_directory=tmp_path,
    )
    assert followup_result is not None
    expected_status = AuditRecordStatus.BLOCKED if root_source_type == SessionReplySourceType.SCHEDULED_TASK_RUN else AuditRecordStatus.PENDING
    assert followup_result.status == expected_status
    assert followup_result.may_execute is False
    if expected_status == AuditRecordStatus.BLOCKED:
        assert followup_result.confirmation_payload is None
        assert json.loads(followup_result.tool_results[0].content or "{}")["status"] == AuditRecordStatus.BLOCKED.value
    else:
        assert followup_result.confirmation_payload is not None
        assert followup_result.confirmation_payload["status"] == AuditRecordStatus.PENDING.value

    async with session_factory() as verification_db:
        root_after = await verification_db.get(SessionReplyWorkItem, root_work.id)
        confirmed_after = await verification_db.get(SessionReplyWorkItem, confirmed_work_id)
        original_after = await verification_db.get(AuditRecord, original_record.id)
        followup_record = (await verification_db.execute(select(AuditRecord).where(AuditRecord.source_work_id == confirmed_work_id))).scalars().one()
        probe_after = await verification_db.get(AuditRecord, probe_pending.audit_record_id)
        original_detail_after = (await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == original_record.id))).scalars().one()
        followup_detail = (await verification_db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == followup_record.id))).scalars().one()
        original_source_after = await verification_db.get(Message, original_source_message.id)
        original_result_after = await verification_db.get(Message, original_tool_result_row.id)
        root_checkpoint = await session_reply_work_item_crud.get_checkpoint(verification_db, root_work.id)
        claims = list((await verification_db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.uid == UID, AuditConfirmationClaim.session_id == SESSION_ID))).scalars().all())
        cards = list((await verification_db.execute(select(Message).where(Message.uid == UID, Message.session_id == SESSION_ID, Message.type == MessageType.AUDIT_CONFIRMATION))).scalars().all())
        executions = list((await verification_db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id.in_([original_record.id, followup_record.id])))).scalars().all())

    assert summary_calls == 2 + int(root_source_type == SessionReplySourceType.TOOL_RESULT)
    assert root_after is not None
    assert root_after.source_type == root_source_type
    assert root_after.status == SessionReplyWorkStatus.SUCCEEDED
    assert root_after.execution_state["frozen_context_message_ids"] == []
    assert isinstance(root_checkpoint, dict)
    assert confirmed_after is not None
    assert confirmed_after.status == SessionReplyWorkStatus.RUNNING
    assert original_after is not None
    assert original_after.status == AuditRecordStatus.EXECUTING
    assert original_after.source_work_id == root_work.id
    assert original_after.execution_scope == END_SESSION_SCOPE_TASK
    assert original_after.round_arguments_hash == original_round_hash
    assert original_after.source_assistant_message_id == original_source_message_id
    assert original_detail_after.conclusion == AuditToolConclusion.PENDING
    assert original_detail_after.score == 5
    assert original_detail_after.arguments_hash == original_arguments_hash
    assert original_detail_after.reason == original_reason
    assert original_source_after is not None and original_source_after.work_id == root_work.id
    assert original_result_after is not None and original_result_after.work_id == root_work.id
    assert json.loads(InternalMessage.model_validate_json(original_result_after.content or "{}").content or "{}") == approved_tool_result_payload
    assert followup_record.source_work_id == confirmed_work_id
    assert followup_record.execution_scope == END_SESSION_SCOPE_TASK
    assert followup_record.status == expected_status
    assert followup_detail.original_tool_call_id == followup_call.id
    assert followup_detail.score == 5
    assert followup_detail.conclusion == (AuditToolConclusion.BLOCKED if expected_status == AuditRecordStatus.BLOCKED else AuditToolConclusion.PENDING)
    assert executions == []

    card_payloads = {json.loads(card.content or "{}")["audit_record_id"]: json.loads(card.content or "{}") for card in cards}
    probe_payload = card_payloads[probe_pending.audit_record_id]
    if root_source_type == SessionReplySourceType.SCHEDULED_TASK_RUN:
        assert probe_after is not None and probe_after.status == AuditRecordStatus.PENDING
        assert claims and [claim.audit_record_id for claim in claims] == [probe_pending.audit_record_id]
        assert probe_payload["status"] == AuditRecordStatus.PENDING.value
    else:
        assert probe_after is not None and probe_after.status == AuditRecordStatus.CANCELLED
        assert claims == []
        assert probe_payload["status"] == AuditRecordStatus.CANCELLED.value
