import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.constants as constants
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.providers.database as database_provider
from app.core.audit import confirmation_events as confirmation_events_module
from app.core.audit import service as audit_service_module
from app.core.audit.integrity import create_file_integrity_snapshot
from app.core.constants import (
    FILE_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    TOOL_WORKFLOW_SCHEDULED_TASK,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.session_reply_queue import executor_interactive as executor_interactive_module
from app.core.session_reply_queue import executor_metadata as executor_metadata_module
from app.core.session_reply_queue import executor_replies as executor_replies_module
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.tools import MANAGE_TODO_TOOL_NAME
from app.models.agent_work import AgentWorkSpec
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.user import User
from app.providers.llm.client import LLMClient
from tests.integration.audit.confirmation_workflow_support import _confirmation_workflow_session_factory as _confirmation_workflow_session_factory
from tests.integration.audit.confirmation_workflow_support import _seed_pending_confirmation
from tests.unit.response_test_support import llm_response_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("source_is_scheduled", [True, False], ids=["scheduled-source", "foreground-source"])
async def test_confirmed_execution_uses_source_work_tool_workflow(
    confirmation_workflow_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_is_scheduled: bool,
) -> None:
    target = tmp_path / "confirmed-policy.txt"
    target.write_text("stable", encoding="utf-8")
    original_tool_call_id = "original-file-call"
    audit_record_id = await _seed_pending_confirmation(
        confirmation_workflow_session_factory,
        working_directory=tmp_path,
        confirmed_tool_calls=[
            InternalToolCall(
                id=original_tool_call_id,
                name=FILE_TOOL_NAME,
                arguments={
                    "operation": "write",
                    "path": str(target),
                    "content": "changed\n",
                },
            )
        ],
    )

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        record = await db.get(AuditRecord, audit_record_id)
        assert profile is not None
        assert record is not None
        profile_configs = ProfileConfig.model_validate(profile.configs).model_dump(mode="json")
        profile_configs["tool"]["allowed_operation_dirs"] = [str(tmp_path.resolve())]
        profile.configs = ProfileConfig.model_validate(profile_configs).model_dump(mode="json")
        pending_messages = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.audit_record_id == audit_record_id,
                        Message.audit_tool_call_id == original_tool_call_id,
                        Message.type == MessageType.TOOL_RESULT,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(pending_messages) == 1
        pending_message_id = pending_messages[0].id

        source_work_type = SessionReplyWorkType.AGENT_WORK if source_is_scheduled else SessionReplyWorkType.FOREGROUND_REPLY
        source_type = SessionReplySourceType.SCHEDULED_TASK_RUN if source_is_scheduled else SessionReplySourceType.USER_MESSAGE
        source_work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid="owner",
            session_id="session-confirmation",
            profile_id=1,
            work_type=source_work_type,
            source_type=source_type,
            source_id="source-work",
            dedupe_key=f"confirmed-policy-source:{source_is_scheduled}",
            commit=False,
        )
        assert created
        assert source_work.id is not None
        source_work.status = SessionReplyWorkStatus.SUCCEEDED
        record.source_work_id = source_work.id
        record.execution_scope = "foreground"
        await db.commit()

    monkeypatch.setitem(
        constants.TOOL_WORKFLOW_POLICIES[TOOL_WORKFLOW_SCHEDULED_TASK][FILE_TOOL_NAME],
        "operation",
        ("read", "grep"),
    )

    async def dispatch_interactive_work(*_args, **_kwargs) -> dict:
        return llm_response_payload("确认处理完成", history=[], files=[])

    async def send_session_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
        return None

    monkeypatch.setattr(executor_confirmed_module, "_dispatch_interactive_work", dispatch_interactive_work)
    monkeypatch.setattr(confirmation_events_module, "send_session_event", send_session_event)

    worker_id = f"confirmed-policy-worker-{source_is_scheduled}"
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
        assert submission_status == "approved"
        assert confirmed_work.id is not None
        claimed = await session_reply_work_item_crud.claim_next(
            db,
            worker_id=worker_id,
            lease_seconds=300,
        )
        assert claimed is not None
        assert claimed.id == confirmed_work.id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (claimed.id, worker_id)
        response = await executor_confirmed_module._execute_confirmed_tools(db, claimed, worker_id)

    assert response["content"] == "确认处理完成"
    assert target.read_text(encoding="utf-8") == ("stable" if source_is_scheduled else "changed\n")

    async with confirmation_workflow_session_factory() as db:
        profile = await db.get(Profile, 1)
        record = await db.get(AuditRecord, audit_record_id)
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == audit_record_id).order_by(AuditExecutionRecord.id))).scalars().all())
        tool_results = list(
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
        versions = list(
            (
                await db.execute(
                    select(AuditToolResultVersion)
                    .where(
                        AuditToolResultVersion.audit_record_id == audit_record_id,
                        AuditToolResultVersion.original_tool_call_id == original_tool_call_id,
                    )
                    .order_by(AuditToolResultVersion.version_no)
                )
            )
            .scalars()
            .all()
        )

    assert profile is not None
    assert ProfileConfig.model_validate(profile.configs).tool.allowed_operation_dirs == [str(tmp_path.resolve())]
    assert record is not None
    assert record.source_work_id == source_work.id
    assert record.execution_scope == "foreground"
    assert record.status == (AuditRecordStatus.FAILED if source_is_scheduled else AuditRecordStatus.SUCCEEDED)
    expected_execution_status = AuditExecutionStatus.CANCELLED if source_is_scheduled else AuditExecutionStatus.SUCCEEDED
    assert [execution.status for execution in executions] == [expected_execution_status]
    assert len(tool_results) == 1
    result_message = tool_results[0]
    assert result_message.id == pending_message_id
    assert result_message.audit_tool_call_id == original_tool_call_id
    result = InternalMessage.model_validate_json(result_message.content or "{}")
    assert result.tool_call_id == original_tool_call_id
    result_payload = json.loads(result.content or "{}")
    assert result_payload["status"] == ("failed" if source_is_scheduled else "success")
    assert result_payload["confirmation_decision"] == "approve"
    assert result_message.content_revision == 2
    assert [version.version_no for version in versions] == [0, 1, 2]
    assert all(version.message_id == pending_message_id for version in versions)
    assert all(version.original_tool_call_id == original_tool_call_id for version in versions)
    assert result_payload["status"] != AuditRecordStatus.PENDING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("source_is_scheduled", [False, True], ids=["foreground-source", "scheduled-source"])
async def test_confirmed_file_reaudit_rebinds_source_and_resumes_without_reexecution(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_is_scheduled: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "reaudit-policy.txt"
    target.write_text("stable\n", encoding="utf-8")
    original_tool_call_id = "original-file-call"
    execution_scope = "task" if source_is_scheduled else "foreground"
    worker_id = f"reaudit-worker-{source_is_scheduled}"

    async with database_factory(
        foreign_keys=True,
        wal=True,
        name=f"confirmation-reaudit-{int(source_is_scheduled)}.sqlite3",
    ) as session_factory:
        async with session_factory() as db:
            db.add_all(
                [
                    User(uid="owner", username="owner"),
                    ModelChannel(
                        id=1,
                        name="confirmation-reaudit-channel",
                        api_key="enc:v1:test-api-key",
                        base_url="https://llm.invalid",
                        model_ids=[
                            {
                                "model_id": model_id,
                                "usage": "CHAT",
                                "protocol": "OPENAI",
                                "context_window_k": 32,
                                "max_tokens": 512,
                            }
                            for model_id in ("chat-model", "audit-model")
                        ],
                    ),
                ]
            )
            await db.commit()

        audit_record_id = await _seed_pending_confirmation(
            session_factory,
            working_directory=tmp_path,
            confirmed_tool_calls=[
                InternalToolCall(
                    id=original_tool_call_id,
                    name=FILE_TOOL_NAME,
                    arguments={
                        "operation": "write",
                        "path": str(target),
                        "content": "updated\n",
                    },
                )
            ],
        )

        async with session_factory() as db:
            profile = await db.get(Profile, 1)
            record = await db.get(AuditRecord, audit_record_id)
            assert profile is not None
            assert record is not None and record.source_assistant_message_id is not None
            source_message = await db.get(Message, record.source_assistant_message_id)
            assert source_message is not None

            config = ProfileConfig.model_validate(profile.configs)
            config.security.audit_channel_id = 1
            config.security.audit_model_id = "audit-model"
            config.tool.enabled_tools = [FILE_TOOL_NAME, MANAGE_TODO_TOOL_NAME]
            config.tool.allowed_operation_dirs = [str(tmp_path.resolve())]
            config.memory.precheck_enabled = False
            profile.configs = config.model_dump(mode="json")

            detail = (
                (
                    await db.execute(
                        select(AuditToolDetail).where(
                            AuditToolDetail.audit_record_id == audit_record_id,
                            AuditToolDetail.original_tool_call_id == original_tool_call_id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            detail.file_snapshots = [create_file_integrity_snapshot(str(target), working_directory=tmp_path).to_dict()]

            if source_is_scheduled:
                source_work, created = await session_reply_queue_manager.enqueue_agent_work(
                    db,
                    uid="owner",
                    session_id="session-confirmation",
                    profile_id=1,
                    source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
                    source_id="scheduled-file-update",
                    dedupe_key="reaudit-scheduled-file-source",
                    spec=AgentWorkSpec(instruction="scheduled file update", execution_scope="task"),
                    commit=False,
                )
            else:
                source_work, created = await session_reply_work_item_crud.enqueue(
                    db,
                    uid="owner",
                    session_id="session-confirmation",
                    profile_id=1,
                    work_type=SessionReplyWorkType.FOREGROUND_REPLY,
                    source_type=SessionReplySourceType.USER_MESSAGE,
                    source_id="foreground-file-update",
                    dedupe_key="reaudit-foreground-file-source",
                    commit=False,
                )
            assert created
            assert source_work.id is not None
            source_work.status = SessionReplyWorkStatus.SUCCEEDED
            record.source_work_id = source_work.id
            record.execution_scope = execution_scope
            if source_is_scheduled:
                source_message.work_id = source_work.id
            db.add_all([profile, detail, record, source_work, source_message])
            await db.commit()

        target.write_text("external change\n", encoding="utf-8")

        for module in (
            database_provider,
            dispatcher_module,
            executor_interactive_module,
            executor_metadata_module,
            channel_cursor_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

        async def call_auditor(*_args, **_kwargs):
            return (
                {"messages": []},
                {
                    "parsed": {
                        "results": [
                            {
                                "tool_call_id": original_tool_call_id,
                                "score": 0,
                                "reason": "verified changed file",
                                "deny": False,
                                "file_checks": [],
                            }
                        ]
                    },
                    "file_reads": [],
                    "shell_results": [],
                },
            )

        monkeypatch.setattr(audit_service_module, "_call_auditor", call_auditor)

        actual_tool_names: list[set[str]] = []

        async def generate(**kwargs) -> InternalResponse:
            tools = kwargs.get("tools") or []
            tool_names = {item.get("function", {}).get("name") for item in tools if isinstance(item, dict) and isinstance(item.get("function"), dict)}
            assert FILE_TOOL_NAME in tool_names
            if source_is_scheduled:
                assert MANAGE_TODO_TOOL_NAME not in tool_names
            else:
                assert MANAGE_TODO_TOOL_NAME in tool_names
            actual_tool_names.append(tool_names)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="重审处理完成"),
                model="chat-model",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                finish_reason="stop",
            )

        monkeypatch.setattr(LLMClient, "generate", generate)

        async def send_session_event(_uid: str, _session_id: str, _event_payload: dict) -> None:
            return None

        monkeypatch.setattr(confirmation_events_module, "send_session_event", send_session_event)

        async with session_factory() as db:
            profile = await db.get(Profile, 1)
            assert profile is not None
            _decision_message, confirmed_work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
                db,
                uid="owner",
                session_id="session-confirmation",
                profile=profile,
                message="approve",
                attachments=None,
                source="http",
            )
            assert submission_status == "approved"
            assert confirmed_work.id is not None
            confirmed_work_id = confirmed_work.id
            decision_message_id = confirmed_work.execution_state["decision_message_id"]
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=worker_id,
                lease_seconds=300,
            )
            assert claimed is not None and claimed.id == confirmed_work_id
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (confirmed_work_id, worker_id)
            first_response = await executor_confirmed_module._execute_confirmed_tools(db, claimed, worker_id)

        assert first_response["content"] == "重审处理完成"
        assert target.read_text(encoding="utf-8") == "updated\n"

        async with session_factory() as db:
            records = list((await db.execute(select(AuditRecord).order_by(AuditRecord.id))).scalars().all())
            confirmed_work = await db.get(SessionReplyWorkItem, confirmed_work_id)
            source_work = await db.get(SessionReplyWorkItem, records[0].source_work_id if records[0].source_work_id else -1)
            executions = list((await db.execute(select(AuditExecutionRecord).order_by(AuditExecutionRecord.id))).scalars().all())
            assert confirmed_work is not None
            new_record = next(record for record in records if record.id != audit_record_id)
            result_messages = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.audit_record_id == new_record.id,
                            Message.audit_tool_call_id == original_tool_call_id,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            result_versions = list(
                (
                    await db.execute(
                        select(AuditToolResultVersion).where(
                            AuditToolResultVersion.audit_record_id == new_record.id,
                            AuditToolResultVersion.original_tool_call_id == original_tool_call_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert len(records) == 2
        old_record = next(record for record in records if record.id == audit_record_id)
        assert old_record.status == AuditRecordStatus.CANCELLED
        assert old_record.source_work_id == source_work.id
        assert old_record.decision_message_id == decision_message_id
        assert new_record.status == AuditRecordStatus.SUCCEEDED
        assert new_record.source_work_id == confirmed_work_id
        assert new_record.execution_scope == execution_scope
        assert confirmed_work.source_id == str(new_record.id)
        assert confirmed_work.execution_state["decision_message_id"] == decision_message_id
        assert source_work is not None and source_work.status == SessionReplyWorkStatus.SUCCEEDED
        if source_is_scheduled:
            assert source_work.work_type == SessionReplyWorkType.AGENT_WORK
            assert source_work.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN
            assert source_work.execution_state["frozen_context_message_ids"] == []
            assert source_work.execution_state["agent_work"] == AgentWorkSpec(
                instruction="scheduled file update",
                execution_scope="task",
            ).model_dump(mode="json")
        else:
            assert source_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
            assert source_work.source_type == SessionReplySourceType.USER_MESSAGE
        assert len(result_messages) == 1
        result_message = result_messages[0]
        assert result_message.audit_tool_call_id == original_tool_call_id
        result = InternalMessage.model_validate_json(result_message.content or "{}")
        assert result.tool_call_id == original_tool_call_id
        assert json.loads(result.content or "{}")["status"] == "success"
        assert len(result_versions) == 1
        assert result_versions[0].version_no == 0
        assert len(executions) == 1
        assert executions[0].audit_record_id == new_record.id
        assert executions[0].status == AuditExecutionStatus.SUCCEEDED
        assert executions[0].new_tool_call_id

        async with session_factory() as db:
            resumed_work = await db.get(SessionReplyWorkItem, confirmed_work_id)
            assert resumed_work is not None
            db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (confirmed_work_id, worker_id)
            resumed_response = await executor_replies_module._execute_external_resume(db, resumed_work, worker_id)

        assert resumed_response["choices"][0]["message"]["content"] == "重审处理完成"
        assert target.read_text(encoding="utf-8") == "updated\n"
        assert len(actual_tool_names) == 2

        async with session_factory() as db:
            final_records = list((await db.execute(select(AuditRecord).order_by(AuditRecord.id))).scalars().all())
            final_executions = list((await db.execute(select(AuditExecutionRecord))).scalars().all())
            final_result_messages = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.audit_record_id == new_record.id,
                            Message.audit_tool_call_id == original_tool_call_id,
                            Message.type == MessageType.TOOL_RESULT,
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert [(record.id, record.status, record.source_work_id) for record in final_records] == [(record.id, record.status, record.source_work_id) for record in records]
        assert len(final_executions) == len(executions) == 1
        assert len(final_result_messages) == 1
        assert final_result_messages[0].id == result_message.id
