from copy import deepcopy
from pathlib import Path

import pytest

from app.core.constants import END_SESSION_SCOPE_TASK, SKILL_TOOL_NAME
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkStatus
from tests.integration.audit.test_system_action_confirmation_workflow import _prepared_runtime, _read_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("threshold", "expected_record_status"),
    [
        pytest.param(0, AuditRecordStatus.SUCCEEDED, id="confirmation-disabled"),
        pytest.param(5, AuditRecordStatus.BLOCKED, id="confirmation-enabled"),
    ],
)
async def test_scheduled_skill_uninstall_uses_profile_confirmation_switch(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    threshold: int,
    expected_record_status: AuditRecordStatus,
) -> None:
    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, retained_package, target_before, retained_before = packages

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["security"]["audit_threshold"] = threshold
            profile.configs = configs

            trigger = Message(
                session_id=runtime.session_id,
                uid=runtime.uid,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="Remove the target skill package on schedule.",
                profile_id=profile.id,
                is_processed=True,
            )
            db.add(trigger)
            await db.flush()
            assert trigger.id is not None

            scheduled_work, created = await session_reply_queue_manager.enqueue_agent_work(
                db,
                uid=runtime.uid,
                session_id=runtime.session_id,
                profile_id=profile.id,
                source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
                source_id=trigger.id,
                dedupe_key="scheduled-system-action-confirmation-test",
                spec=AgentWorkSpec(
                    instruction=trigger.content,
                    instruction_message_id=trigger.id,
                    execution_scope=END_SESSION_SCOPE_TASK,
                    report_target=AgentWorkReportTarget.SESSION,
                ),
                commit=False,
            )
            assert created is True
            assert scheduled_work.id is not None
            trigger.work_id = scheduled_work.id
            db.add(trigger)
            await db.commit()

        uninstall_call = InternalToolCall(
            id="scheduled-system-action-uninstall-call",
            name=SKILL_TOOL_NAME,
            arguments={"operation": "uninstall", "skill_id": "target-package"},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[uninstall_call]))
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The scheduled skill operation finished."))

        completed_work = await runtime.run_next(independent_only=True)
        assert completed_work is not None
        assert completed_work.id == scheduled_work.id
        assert completed_work.status is SessionReplyWorkStatus.SUCCEEDED
        assert not runtime.outputs
        assert len(runtime.provider_payloads) == 2

        state = await _read_state(runtime)
        assert len(state["records"]) == 1
        record = state["record"]
        assert record is not None
        assert record.source_work_id == scheduled_work.id
        assert record.execution_scope == END_SESSION_SCOPE_TASK
        assert record.status is expected_record_status
        assert len(state["details"]) == 1
        detail = state["details"][0]
        assert detail.decision_source == "system_rule"
        assert detail.score is None
        assert state["cards"] == []
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before

        if threshold == 0:
            assert len(state["executions"]) == 1
            assert state["executions"][0].status is AuditExecutionStatus.SUCCEEDED
            assert not target_package.exists()
        else:
            assert state["executions"] == []
            assert target_package.is_dir()
            assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before

        async with runtime.factory() as db:
            persisted_trigger = await db.get(Message, trigger.id)
            assert persisted_trigger is not None
            assert persisted_trigger.work_id == scheduled_work.id
