import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

from app.core.constants import ERR_SYSTEM_ACTION_PERMISSION_DENIED, FILE_TOOL_NAME, SKILL_TOOL_NAME
from app.core.i18n import t
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.audit import AuditExecutionRecord, AuditExecutionStatus, AuditRecordStatus, AuditToolDetail
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import Profile
from app.models.session_reply_work_item import SessionReplyWorkStatus
from app.models.user import User
from app.providers.llm.client import LLMClient
from tests.integration.audit.test_system_action_confirmation_workflow import (
    _prepared_runtime,
    _read_state,
    _submit,
)


@pytest.mark.parametrize("audit_case", ["low-risk", "high-risk", "deny"])
@pytest.mark.asyncio
async def test_mixed_system_action_and_file_write_share_confirmation_round(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    audit_case: str,
) -> None:
    score_by_case = {"low-risk": 1, "high-risk": 9, "deny": 1}
    deny_by_case = {"low-risk": False, "high-risk": False, "deny": True}
    expected_status_by_case = {
        "low-risk": AuditRecordStatus.PENDING,
        "high-risk": AuditRecordStatus.PENDING,
        "deny": AuditRecordStatus.BLOCKED,
    }

    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, _retained_package, target_before, _retained_before = packages
        output_file = (tmp_path / "mixed-output.txt").resolve()

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["security"].update(
                {
                    "audit_channel_id": 1,
                    "audit_model_id": runtime.chat_model_id,
                    "audit_threshold": 5,
                    "protect_system_directories": False,
                }
            )
            profile.configs = configs
            await db.commit()

        scoring_requests: list[dict[str, Any]] = []
        summary_requests: list[dict[str, Any]] = []
        original_generate = LLMClient.generate

        async def generate(**kwargs: Any) -> InternalResponse:
            for message in kwargs["messages"]:
                if message.role != MessageRole.USER or not isinstance(message.content, str):
                    continue
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if "confirmation_threshold" in payload and isinstance(payload.get("tool_calls"), list):
                    scoring_requests.append(payload)
                    assert [item.get("tool_name") for item in payload["tool_calls"]] == [FILE_TOOL_NAME]
                    tool_call_id = payload["tool_calls"][0]["tool_call_id"]
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            content=json.dumps(
                                {
                                    "results": [
                                        {
                                            "tool_call_id": tool_call_id,
                                            "score": score_by_case[audit_case],
                                            "reason": "mixed file write risk assessment",
                                            "file_checks": [],
                                            "deny": deny_by_case[audit_case],
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        ),
                        model=runtime.chat_model_id,
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        finish_reason="stop",
                    )
                if "audit_results" in payload:
                    summary_requests.append(payload)
                    assert [item.get("name") for item in payload["tool_calls"]] == [FILE_TOOL_NAME]
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            content="mixed ordinary file write summary",
                        ),
                        model=runtime.chat_model_id,
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        finish_reason="stop",
                    )
            return await original_generate(**kwargs)

        monkeypatch.setattr(LLMClient, "generate", generate)

        write_call = InternalToolCall(
            id="mixed-write-call",
            name=FILE_TOOL_NAME,
            arguments={"operation": "write", "path": str(output_file), "content": "mixed-written"},
        )
        uninstall_call = InternalToolCall(
            id="mixed-uninstall-call",
            name=SKILL_TOOL_NAME,
            arguments={"operation": "uninstall", "skill_id": "target-package"},
        )
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[write_call, uninstall_call],
            )
        )
        if audit_case == "deny":
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The mixed operation was blocked."))

        submitted = await _submit(
            runtime,
            request_id=f"mixed-system-action-{audit_case}-request",
            message="Write the mixed output and remove the target skill package.",
        )
        initial_work = await runtime.run_next()
        assert initial_work is not None
        assert initial_work.id == submitted["work_id"]
        assert initial_work.status == SessionReplyWorkStatus.SUCCEEDED

        state = await _read_state(runtime)
        record = state["record"]
        details = state["details"]
        source_message = state["source_message"]
        assert record is not None
        assert source_message is not None
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
        source_tool_calls = source_internal.tool_calls or []
        assert [tool_call.name for tool_call in source_tool_calls] == [write_call.name, uninstall_call.name]
        assert [tool_call.arguments for tool_call in source_tool_calls] == [write_call.arguments, uninstall_call.arguments]
        persisted_write_call_id = source_tool_calls[0].id
        persisted_uninstall_call_id = source_tool_calls[1].id
        assert record.status is expected_status_by_case[audit_case]
        assert [detail.original_tool_call_id for detail in details] == [persisted_write_call_id, persisted_uninstall_call_id]
        assert [detail.decision_source for detail in details] == ["model_audit", "system_rule"]
        assert details[0].score == score_by_case[audit_case]
        assert details[1].score is None
        assert len(scoring_requests) == 1
        assert [item.get("tool_name") for item in scoring_requests[0]["tool_calls"]] == [FILE_TOOL_NAME]
        assert all(item.get("tool_name") != SKILL_TOOL_NAME for item in scoring_requests[0]["tool_calls"])
        assert scoring_requests[0]["tool_calls"][0]["tool_call_id"] == persisted_write_call_id

        assert target_package.is_dir()
        assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert not output_file.exists()

        if audit_case == "deny":
            assert not state["cards"]
            assert not summary_requests
            assert state["executions"] == []
            return

        confirmation_payload = json.loads(state["cards"][0].content or "{}")
        assert confirmation_payload["status"] == AuditRecordStatus.PENDING.value
        assert confirmation_payload["confirmation_kind"] == "mixed"
        assert confirmation_payload["risk"] == (5 if audit_case == "low-risk" else 9)
        assert confirmation_payload["confirmation_mode"] == ("standard" if audit_case == "low-risk" else "high_risk_override")
        assert len(summary_requests) == 1
        assert [item.get("name") for item in summary_requests[0]["tool_calls"]] == [FILE_TOOL_NAME]
        assert all(item.get("name") != SKILL_TOOL_NAME for item in summary_requests[0]["tool_calls"])
        assert summary_requests[0]["tool_calls"][0]["id"] == persisted_write_call_id

        decision = "approve" if audit_case == "low-risk" else "ignore"
        approved = await _submit(
            runtime,
            request_id=f"mixed-system-action-{audit_case}-decision",
            message=decision,
        )
        assert approved["submission_status"] == "approved"

        provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The mixed operation completed."))
        confirmed_work = await runtime.run_next()
        assert confirmed_work is not None
        assert confirmed_work.id == approved["work_id"]
        assert confirmed_work.status == SessionReplyWorkStatus.SUCCEEDED

        final_state = await _read_state(runtime)
        final_record = final_state["record"]
        assert final_record is not None
        assert final_record.status is AuditRecordStatus.SUCCEEDED
        assert len(final_state["executions"]) == 2
        assert all(execution.status is AuditExecutionStatus.SUCCEEDED for execution in final_state["executions"])

        tool_results = {}
        for message in final_state["tool_results"]:
            internal = InternalMessage.model_validate_json(message.content or "{}")
            assert internal.tool_call_id == message.audit_tool_call_id
            tool_results[message.audit_tool_call_id] = json.loads(internal.content or "{}")
        assert set(tool_results) == {persisted_write_call_id, persisted_uninstall_call_id}
        assert tool_results[persisted_write_call_id]["status"] == "success"
        assert tool_results[persisted_write_call_id]["operation"] == "write"
        assert tool_results[persisted_write_call_id]["path"] == str(output_file)
        assert tool_results[persisted_write_call_id]["confirmation_decision"] == decision
        assert tool_results[persisted_uninstall_call_id]["status"] == "success"
        assert tool_results[persisted_uninstall_call_id]["operation"] == "uninstall"
        assert tool_results[persisted_uninstall_call_id]["skill_id"] == "target-package"
        assert tool_results[persisted_uninstall_call_id]["confirmation_decision"] == decision

        assert output_file.read_text(encoding="utf-8") == "mixed-written"
        assert not target_package.exists()
        continuation_items = runtime.provider_payloads[provider_start]["input"]
        function_call_outputs = [item for item in continuation_items if item.get("type") == "function_call_output"]
        assert len(function_call_outputs) == 2
        continuation_results = [json.loads(item["output"])["tool_result"] for item in function_call_outputs]
        assert {result["operation"] for result in continuation_results} == {"write", "uninstall"}
        assert all(result["status"] == "success" for result in continuation_results)


@pytest.mark.parametrize(
    "revoke_before_reaudit",
    [
        pytest.param(True, id="original-operator-revoked"),
        pytest.param(False, id="original-operator-still-authorized"),
    ],
)
@pytest.mark.asyncio
async def test_mixed_round_reaudit_preserves_original_system_action_operator(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    revoke_before_reaudit: bool,
) -> None:
    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, _retained_package, _target_before, _retained_before = packages
        output_file = (tmp_path / "reaudit-original.txt").resolve()
        output_file.write_text("original", encoding="utf-8")

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["security"].update(
                {
                    "audit_channel_id": 1,
                    "audit_model_id": runtime.chat_model_id,
                    "audit_threshold": 5,
                    "protect_system_directories": False,
                }
            )
            profile.configs = configs
            await db.commit()

        scoring_requests: list[dict[str, Any]] = []
        summary_requests: list[dict[str, Any]] = []
        original_generate = LLMClient.generate

        async def generate(**kwargs: Any) -> InternalResponse:
            for message in kwargs["messages"]:
                if message.role != MessageRole.USER or not isinstance(message.content, str):
                    continue
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if "confirmation_threshold" in payload and isinstance(payload.get("tool_calls"), list):
                    scoring_requests.append(payload)
                    assert [item.get("tool_name") for item in payload["tool_calls"]] == [FILE_TOOL_NAME]
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            content=json.dumps(
                                {
                                    "results": [
                                        {
                                            "tool_call_id": item["tool_call_id"],
                                            "score": 1,
                                            "reason": "mixed file write risk assessment",
                                            "file_checks": [],
                                            "deny": False,
                                        }
                                        for item in payload["tool_calls"]
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        ),
                        model=runtime.chat_model_id,
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        finish_reason="stop",
                    )
                if "audit_results" in payload:
                    summary_requests.append(payload)
                    assert [item.get("name") for item in payload["tool_calls"]] == [FILE_TOOL_NAME]
                    return InternalResponse(
                        message=InternalMessage(
                            role=MessageRole.ASSISTANT,
                            content="mixed file write re-audit summary",
                        ),
                        model=runtime.chat_model_id,
                        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        finish_reason="stop",
                    )
            return await original_generate(**kwargs)

        monkeypatch.setattr(LLMClient, "generate", generate)

        write_call = InternalToolCall(
            id="reaudit-mixed-write-call",
            name=FILE_TOOL_NAME,
            arguments={"operation": "edit", "path": str(output_file), "old_text": "original", "new_text": "authorized-write"},
        )
        uninstall_call = InternalToolCall(
            id="reaudit-mixed-uninstall-call",
            name=SKILL_TOOL_NAME,
            arguments={"operation": "uninstall", "skill_id": "target-package"},
        )
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[write_call, uninstall_call],
            )
        )

        submitted = await _submit(
            runtime,
            request_id="mixed-reaudit-original-operator-request",
            message="Write the output file and remove the target skill package.",
        )
        initial_work = await runtime.run_next()
        assert initial_work is not None
        assert initial_work.id == submitted["work_id"]
        assert initial_work.status == SessionReplyWorkStatus.SUCCEEDED

        initial_state = await _read_state(runtime)
        assert len(initial_state["records"]) == 1
        original_record = initial_state["record"]
        assert original_record is not None
        assert original_record.id is not None
        assert original_record.status is AuditRecordStatus.PENDING
        assert output_file.read_text(encoding="utf-8") == "original"
        assert target_package.is_dir()
        original_file_detail = next(detail for detail in initial_state["details"] if detail.decision_source == "model_audit")
        assert original_file_detail.file_snapshots
        original_system_detail = next(detail for detail in initial_state["details"] if detail.decision_source == "system_rule")
        original_request = original_system_detail.action_request
        assert original_request is not None
        assert original_request["actor_uid"] == runtime.uid
        assert len(scoring_requests) == 1
        assert len(summary_requests) == 1

        async with runtime.factory() as db:
            db.add(
                User(
                    uid="system-approver",
                    username="system_approver",
                    is_superuser=True,
                    is_active=True,
                )
            )
            await db.commit()

        async def _approve_as_system_approver() -> tuple[int, str]:
            async with runtime.factory() as db:
                profile = await db.get(Profile, runtime.profile.id)
                assert profile is not None
                _message, work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    profile=profile,
                    message="approve",
                    attachments=None,
                    source="http",
                    actor_uid="system-approver",
                    actor_username="system_approver",
                )
                assert work.id is not None
                return work.id, submission_status

        approval_work_id, submission_status = await _approve_as_system_approver()
        assert submission_status == "approved"
        output_file.write_text("changed-before-reaudit", encoding="utf-8")

        if revoke_before_reaudit:
            async with runtime.factory() as db:
                original_operator = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
                original_operator.is_superuser = False
                await db.commit()
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The mixed operation was checked."))

        reaudit_work = await runtime.run_next()
        assert reaudit_work is not None
        assert reaudit_work.id == approval_work_id
        assert reaudit_work.status == SessionReplyWorkStatus.SUCCEEDED

        reaudit_state = await _read_state(runtime)
        assert len(reaudit_state["records"]) == 2
        records_by_id = {record.id: record for record in reaudit_state["records"]}
        assert original_record.id in records_by_id
        assert records_by_id[original_record.id].status is AuditRecordStatus.CANCELLED
        new_record = next(record for record in reaudit_state["records"] if record.id != original_record.id)
        assert new_record.id is not None
        card_statuses = {}
        for card in reaudit_state["cards"]:
            payload = json.loads(card.content or "{}")
            card_statuses[payload["audit_record_id"]] = payload["status"]
        assert card_statuses[original_record.id] == AuditRecordStatus.CANCELLED.value
        assert output_file.read_text(encoding="utf-8") == "changed-before-reaudit"
        assert target_package.is_dir()

        if revoke_before_reaudit:
            assert new_record.status is AuditRecordStatus.BLOCKED
            assert not any(record.status is AuditRecordStatus.PENDING for record in reaudit_state["records"])
            function_call_outputs = [item for item in runtime.provider_payloads[-1]["input"] if item.get("type") == "function_call_output"]
            tool_results = {}
            for item in function_call_outputs:
                tool_results[item["call_id"]] = json.loads(item["output"])["tool_result"]
            skill_result = tool_results[original_system_detail.original_tool_call_id]
            assert skill_result["status"] == "failed"
            assert skill_result["error"] == t(ERR_SYSTEM_ACTION_PERMISSION_DENIED)
            file_result = tool_results[original_file_detail.original_tool_call_id]
            assert file_result["status"] == "blocked"
            assert "mixed file write risk assessment" not in file_result.get("error", "")
            return

        assert new_record.status is AuditRecordStatus.PENDING
        assert len([record for record in reaudit_state["records"] if record.status is AuditRecordStatus.PENDING]) == 1
        async with runtime.factory() as db:
            new_details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == new_record.id).order_by(AuditToolDetail.turn_index))).scalars().all())
        new_file_detail = next(detail for detail in new_details if detail.decision_source == "model_audit")
        new_system_detail = next(detail for detail in new_details if detail.decision_source == "system_rule")
        assert new_system_detail.action_request is not None
        assert new_system_detail.action_request == original_request
        assert new_system_detail.action_request["actor_uid"] == runtime.uid
        assert new_file_detail.file_snapshots != original_file_detail.file_snapshots
        reaudit_file_snapshots = deepcopy(new_file_detail.file_snapshots)

        async with runtime.factory() as db:
            original_operator = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
            original_operator.is_superuser = False
            await db.commit()

        second_approval_work_id, second_submission_status = await _approve_as_system_approver()
        assert second_submission_status == "approved"
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The mixed operation completed."))
        confirmed_work = await runtime.run_next()
        assert confirmed_work is not None
        assert confirmed_work.id == second_approval_work_id
        assert confirmed_work.status == SessionReplyWorkStatus.SUCCEEDED

        final_state = await _read_state(runtime)
        final_records_by_id = {record.id: record for record in final_state["records"]}
        assert final_records_by_id[original_record.id].status is AuditRecordStatus.CANCELLED
        assert final_records_by_id[new_record.id].status is AuditRecordStatus.FAILED
        final_card_statuses = {}
        for card in final_state["cards"]:
            payload = json.loads(card.content or "{}")
            final_card_statuses[payload["audit_record_id"]] = payload["status"]
        assert final_card_statuses == {
            original_record.id: AuditRecordStatus.CANCELLED.value,
            new_record.id: AuditRecordStatus.FAILED.value,
        }
        async with runtime.factory() as db:
            executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == new_record.id).order_by(AuditExecutionRecord.id))).scalars().all())
            final_details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == new_record.id).order_by(AuditToolDetail.turn_index))).scalars().all())
        assert len(executions) == 2
        assert all(execution.status is AuditExecutionStatus.CANCELLED for execution in executions)
        final_file_detail = next(detail for detail in final_details if detail.decision_source == "model_audit")
        final_system_detail = next(detail for detail in final_details if detail.decision_source == "system_rule")
        assert final_file_detail.file_snapshots == reaudit_file_snapshots
        assert final_system_detail.action_request == original_request
        assert output_file.read_text(encoding="utf-8") == "changed-before-reaudit"
        assert target_package.is_dir()
