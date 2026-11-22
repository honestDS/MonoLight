import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.providers.database as database_provider
from app.adapters.chat_web import web_chat_adapter
from app.core.confirmation import events as confirmation_events_module
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_lifecycle as executor_lifecycle_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue import manager_result as manager_result_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal import ShellNonInteractiveCompletedResult
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.request_token_baseline import build_provider_request_usage_metadata
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolResultVersion,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.provider_usage import ProviderUsageDetail, ProviderUsageType
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.integration.audit.confirmation_workflow_support import _confirmation_workflow_session_factory as _confirmation_workflow_session_factory
from tests.integration.audit.confirmation_workflow_support import (
    _seed_pending_confirmation,
    _wait_for_confirmed_work,
)
from tests.unit.response_test_support import llm_response_payload


@pytest.mark.asyncio
async def test_confirmation_workflow_approves_executes_replaces_pending_result_and_completes_reply(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
        include_parallel_limit_exceeded_call=True,
    )

    monkeypatch.setattr(database_provider, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", confirmation_workflow_session_factory)
    monkeypatch.setattr(manager_result_module, "WORK_RESULT_POLL_INTERVAL_SECONDS", 0.01)

    async def resolve_profile(db: AsyncSession, *, uid: str, session_id: str):
        profile = await db.get(Profile, 1)
        assert profile is not None and profile.uid == uid and session_id == "session-confirmation"
        return profile

    async def validate_initial(*_args, **_kwargs) -> None:
        return None

    async def ensure_writable(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.chat_web.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr("app.adapters.chat_web.ChatDispatcher.validate_initial_message_before_save", validate_initial)
    monkeypatch.setattr("app.adapters.chat_web.ensure_web_session_writable", ensure_writable)

    executed_calls: list[InternalToolCall] = []
    execution_messages: list[list[InternalMessage]] = []

    async def execute_tool(tool_call, _db, _profile, _cfg, messages, _username, _session_id, _turn, _uid, **_kwargs):
        executed_calls.append(tool_call.model_copy(deep=True))
        execution_messages.append([message.model_copy(deep=True) for message in messages])
        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=ShellNonInteractiveCompletedResult(
                stdout=f"{tool_call.arguments['command']}\n",
                stderr="",
                exit_code=0,
                system_info="test-system",
            ).model_dump_json(),
            tool_execution_status="succeeded",
        )

    monkeypatch.setattr(executor_confirmed_module, "process_single_tool", execute_tool)

    resumed_histories: list[list[Message]] = []

    async def continue_reply(**kwargs) -> dict:
        db = kwargs["db"]
        result = await db.execute(select(Message).where(Message.session_id == kwargs["session_id"]).order_by(Message.id))
        persisted = list(result.scalars().all())
        resumed_histories.append(persisted)
        pending_rows = [row for row in persisted if row.type == MessageType.TOOL_RESULT and row.audit_record_id == audit_record_id]
        assert [row.audit_tool_call_id for row in pending_rows] == [
            "original-shell-call-1",
            "original-shell-call-2",
        ]
        assert [row.content_revision for row in pending_rows] == [2, 2]
        replacement_payloads = []
        for pending_row in pending_rows:
            pending_internal = InternalMessage.model_validate_json(pending_row.content or "{}")
            assert pending_internal.tool_call_id == pending_row.audit_tool_call_id
            replacement_payloads.append(json.loads(pending_internal.content))
        assert [payload["exit_code"] for payload in replacement_payloads] == [0, 0]
        assert [payload["stderr"] for payload in replacement_payloads] == ["", ""]
        assert [payload["stdout"] for payload in replacement_payloads] == [
            "echo confirmed-1\n",
            "echo confirmed-2\n",
        ]
        parallel_limit_rows = [row for row in persisted if row.type == MessageType.TOOL_RESULT and row.audit_record_id is None]
        assert len(parallel_limit_rows) == 1
        assert parallel_limit_rows[0].audit_tool_call_id is None
        assert parallel_limit_rows[0].content_revision == 0
        parallel_limit_result = InternalMessage.model_validate_json(parallel_limit_rows[0].content or "{}")
        assert parallel_limit_result.tool_call_id == "original-shell-call-over-limit"
        assert json.loads(parallel_limit_result.content or "{}") == {
            "status": "failed",
            "tool_name": "execute_shell",
            "error": "parallel_limit_exceeded",
            "requested": 3,
            "limit": 2,
            "executed": False,
            "message": "Too many parallel tool calls. Requested: 3, Limit: 2.",
        }

        await db.commit()
        metrics = {
            "input_tokens": 120,
            "input_tokens_source": "provider",
            "cached_tokens": 20,
            "output_tokens": 7,
        }
        await kwargs["request_metadata_callback"](
            {
                "type": "llm_request_metadata",
                "turn": 1,
                "response_id": "confirmation-final",
                "input_tokens": metrics["input_tokens"],
                "input_tokens_source": metrics["input_tokens_source"],
                "cached_tokens": metrics["cached_tokens"],
                "output_tokens": metrics["output_tokens"],
                "context_window_tokens": 4096,
                "max_output_tokens": 512,
                "_provider_token_usage": {
                    "input_tokens": 120,
                    "cached_tokens": 20,
                    "output_tokens": 7,
                },
                **build_provider_request_usage_metadata("confirmed-continuation-request", metrics),
            }
        )
        async with confirmation_workflow_session_factory() as metadata_db:
            session = await metadata_db.get(ChatSession, kwargs["session_id"])
            assert session is not None
            latest_llm_request_metadata = session.llm_request_metadata

        await save_message(
            db,
            kwargs["session_id"],
            kwargs["uid"],
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="confirmed tool completed"),
            kwargs["persisted_profile_id"],
            dedupe_key=kwargs["final_message_dedupe_key"],
        )
        response = llm_response_payload(content="confirmed tool completed", history=[])
        response["choices"][0]["finish_reason"] = "stop"
        return {
            **response,
            "response_id": "confirmation-final",
            "llm_request_metadata": latest_llm_request_metadata,
        }

    monkeypatch.setattr(executor_interactive_module.ChatDispatcher, "dispatch", continue_reply)

    delivered_events: list[dict] = []

    async def capture_session_event(_uid: str, _session_id: str, event_payload: dict) -> None:
        delivered_events.append(event_payload)

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_session_event)
    monkeypatch.setattr(confirmation_events_module, "send_session_event", capture_session_event)

    async with confirmation_workflow_session_factory() as web_db:
        response_task = asyncio.create_task(
            web_chat_adapter.chat(
                web_db,
                "approve",
                uid="owner",
                session_id="session-confirmation",
                request_id="confirmation-request",
            )
        )
        created_work = await _wait_for_confirmed_work(confirmation_workflow_session_factory)
        async with confirmation_workflow_session_factory() as worker_db:
            claimed = await session_reply_work_item_crud.claim_next(
                worker_db,
                worker_id="confirmation-worker",
                lease_seconds=300,
            )
        assert claimed is not None
        assert claimed.id == created_work.id
        assert claimed.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
        await executor_lifecycle_module.execute_session_reply_work(claimed.id, "confirmation-worker")
        response = await asyncio.wait_for(response_task, timeout=5)

    assert response["content"] == "confirmed tool completed"
    assert response["work_id"] == created_work.id
    assert len(executed_calls) == 2
    assert [call.name for call in executed_calls] == ["execute_shell", "execute_shell"]
    assert [call.arguments for call in executed_calls] == [
        {"command": "echo confirmed-1", "execution_mode": "non_interactive"},
        {"command": "echo confirmed-2", "execution_mode": "non_interactive"},
    ]
    fresh_call_ids = [call.id for call in executed_calls]
    assert len(set(fresh_call_ids)) == 2
    assert all(call_id.startswith("call_") for call_id in fresh_call_ids)
    assert set(fresh_call_ids).isdisjoint({"original-shell-call-1", "original-shell-call-2"})
    assert [message.role for message in execution_messages[1]] == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert execution_messages[1][0].tool_calls is not None
    assert execution_messages[1][0].tool_calls[0].id == fresh_call_ids[0]
    assert execution_messages[1][1].tool_call_id == fresh_call_ids[0]
    assert resumed_histories

    async with confirmation_workflow_session_factory() as db:
        record = await db.get(AuditRecord, audit_record_id)
        work = await session_reply_work_item_crud.get(db, created_work.id)
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id))).scalars().all())
        versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == audit_record_id).order_by(AuditToolResultVersion.version_no))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.session_id == "session-confirmation").order_by(Message.id))).scalars().all())
        usage = (await db.execute(select(ProviderUsageDetail).where(ProviderUsageDetail.provider_request_id == "confirmed-continuation-request"))).scalars().one_or_none()
        session = await db.get(ChatSession, "session-confirmation")
        source_message = await db.get(Message, record.source_assistant_message_id) if record is not None and record.source_assistant_message_id is not None else None

    assert record is not None
    assert source_message is not None
    source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    assert [tool_call.id for tool_call in source_internal.tool_calls or []] == [
        "original-shell-call-1",
        "original-shell-call-2",
        "original-shell-call-over-limit",
    ]
    assert record.status == AuditRecordStatus.SUCCEEDED
    assert record.decision is not None and record.decision.value == "approve"
    assert record.decision_raw_message == "approve"
    assert record.execution_claim_token is None
    assert work is not None
    assert work.status == SessionReplyWorkStatus.SUCCEEDED
    assert work.result_message_id is not None
    assert len(executions) == 2
    assert [execution.status for execution in executions] == [
        AuditExecutionStatus.SUCCEEDED,
        AuditExecutionStatus.SUCCEEDED,
    ]
    assert [execution.new_tool_call_id for execution in executions] == fresh_call_ids
    versions_by_call: dict[str, list[AuditToolResultVersion]] = {}
    for version in versions:
        versions_by_call.setdefault(version.original_tool_call_id, []).append(version)
    assert set(versions_by_call) == {"original-shell-call-1", "original-shell-call-2"}
    for original_call_id, call_versions in versions_by_call.items():
        assert [version.version_no for version in call_versions] == [0, 1, 2]
        final_payload = json.loads(InternalMessage.model_validate_json(call_versions[-1].content).content)
        assert final_payload["exit_code"] == 0
        assert final_payload["stderr"] == ""
        expected_command = "echo confirmed-1" if original_call_id.endswith("-1") else "echo confirmed-2"
        assert final_payload["stdout"] == f"{expected_command}\n"
    parallel_limit_rows = [message for message in messages if message.type == MessageType.TOOL_RESULT and message.audit_record_id is None]
    assert len(parallel_limit_rows) == 1
    assert parallel_limit_rows[0].audit_tool_call_id is None
    assert parallel_limit_rows[0].content_revision == 0
    parallel_limit_result = InternalMessage.model_validate_json(parallel_limit_rows[0].content or "{}")
    assert parallel_limit_result.tool_call_id == "original-shell-call-over-limit"
    assert json.loads(parallel_limit_result.content or "{}") == {
        "status": "failed",
        "tool_name": "execute_shell",
        "error": "parallel_limit_exceeded",
        "requested": 3,
        "limit": 2,
        "executed": False,
        "message": "Too many parallel tool calls. Requested: 3, Limit: 2.",
    }
    final_message = next(message for message in messages if message.id == work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.content == "confirmed tool completed"
    assert any(event.get("source") == "confirmed_tool_execution" for event in delivered_events)
    assert usage is not None
    assert usage.type == ProviderUsageType.MAIN_DIALOGUE.value
    assert usage.work_id == created_work.id
    assert usage.input_tokens == 120
    assert usage.cached_tokens == 20
    assert usage.output_tokens == 7
    assert session is not None
    assert session.llm_request_metadata["total_input_tokens"] == 1120
    assert session.llm_request_metadata["total_cached_tokens"] == 270
    assert session.llm_request_metadata["total_output_tokens"] == 207
    assert session.llm_request_metadata["cache_hit_rate"] == pytest.approx(270 / 1120)


@pytest.mark.asyncio
async def test_task_confirmation_inherits_source_work_checkpoint(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        source_work, _ = await session_reply_work_item_crud.enqueue(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile_id=1,
            work_type=SessionReplyWorkType.AGENT_WORK,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id="900",
            dedupe_key="scheduled-task-summary:source-checkpoint",
            commit=False,
        )
        assert source_work.work_category == SessionReplyWorkCategory.SCHEDULED_TASK
        source_work.status = SessionReplyWorkStatus.SUCCEEDED
        source_checkpoint = {
            "messages": [
                InternalMessage(
                    id=11,
                    role=MessageRole.USER,
                    content="scheduled-trigger-context",
                ).model_dump(mode="json", exclude_none=True),
                InternalMessage(
                    id=12,
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="original-shell-call-1",
                            name="execute_shell",
                            arguments={"command": "echo confirmed-1", "execution_mode": "non_interactive"},
                        )
                    ],
                ).model_dump(mode="json", exclude_none=True),
                InternalMessage(
                    id=13,
                    role=MessageRole.TOOL,
                    tool_call_id="original-shell-call-1",
                    content="pending-confirmation-result",
                ).model_dump(mode="json", exclude_none=True),
            ],
        }
        frozen_context_messages = [
            InternalMessage(
                id=10,
                role=MessageRole.USER,
                content="base-snapshot-only",
            ).model_dump(mode="json", exclude_none=True),
        ]
        source_work.execution_state = {}
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        record.execution_scope = "task"
        record.source_work_id = source_work.id
        await db.flush()
        assert source_work.id is not None
        frozen_context_message_ids = await session_reply_work_item_crud.save_frozen_context(db, source_work.id, frozen_context_messages)
        source_work.execution_state = {
            "frozen_context_message_ids": frozen_context_message_ids,
        }
        await session_reply_work_item_crud.save_checkpoint(db, source_work.id, source_checkpoint)
        await db.commit()

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, confirmed_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="approve",
            attachments=None,
            source="http",
        )
        confirmed_frozen_context = await session_reply_work_item_crud.get_frozen_context(db, confirmed_work)
        assert confirmed_frozen_context is not None

    assert submission_status == "approved"
    assert confirmed_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert confirmed_work.work_category == SessionReplyWorkCategory.SCHEDULED_TASK
    assert confirmed_work.execution_state["execution_scope"] == "task"
    frozen_messages = [InternalMessage.model_validate(item) for item in confirmed_frozen_context]
    assert [message.content for message in frozen_messages if message.role == MessageRole.USER] == ["scheduled-trigger-context"]
    assert not any(message.content == "base-snapshot-only" for message in frozen_messages)
    assert any(message.role == MessageRole.ASSISTANT and any(tool_call.id == "original-shell-call-1" for tool_call in message.tool_calls or []) for message in frozen_messages)
    assert any(message.role == MessageRole.TOOL and message.tool_call_id == "original-shell-call-1" and message.content == "pending-confirmation-result" for message in frozen_messages)


@pytest.mark.asyncio
async def test_foreground_confirmation_does_not_inherit_live_source_checkpoint(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
    )

    async with confirmation_workflow_session_factory() as db:
        source_work, _ = await session_reply_work_item_crud.enqueue(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile_id=1,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="901",
            dedupe_key="foreground-source-checkpoint",
            commit=False,
        )
        assert source_work.work_category == SessionReplyWorkCategory.FOREGROUND
        source_work.status = SessionReplyWorkStatus.SUCCEEDED
        source_checkpoint = {
            "messages": [
                InternalMessage(
                    id=21,
                    role=MessageRole.USER,
                    content="live-foreground-checkpoint",
                ).model_dump(mode="json", exclude_none=True),
            ],
        }
        source_work.execution_state = {}
        record = await db.get(AuditRecord, audit_record_id)
        assert record is not None
        record.execution_scope = "foreground"
        record.source_work_id = source_work.id
        await db.flush()
        assert source_work.id is not None
        await session_reply_work_item_crud.save_checkpoint(db, source_work.id, source_checkpoint)
        await db.commit()

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        assert profile is not None
        _message, confirmed_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile=profile,
            message="approve",
            attachments=None,
            source="http",
        )
        confirmed_frozen_context = await session_reply_work_item_crud.get_frozen_context(db, confirmed_work)
        confirmed_checkpoint = await session_reply_work_item_crud.get_checkpoint(db, confirmed_work.id)

    assert submission_status == "approved"
    assert confirmed_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
    assert confirmed_work.work_category == SessionReplyWorkCategory.FOREGROUND
    assert confirmed_work.execution_state["execution_scope"] == "foreground"
    assert confirmed_frozen_context is None
    assert "dispatcher_checkpoint" not in confirmed_work.execution_state
    assert confirmed_checkpoint is None
