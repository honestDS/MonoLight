import json
import shutil
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

import app.core.paths as paths
from app.core.confirmation.lifecycle import expire_confirmation_by_session
from app.core.constants import (
    ERR_SYSTEM_ACTION_TARGET_CHANGED,
    ERR_TOOL_OPERATION_NOT_FOUND,
    ERR_TOOL_OPERATION_PERMISSION_DENIED,
    SKILL_TOOL_NAME,
)
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.user import User


def _write_skill_packages(skills_root: Path) -> tuple[Path, Path, dict[Path, bytes], dict[Path, bytes]]:
    target_package = skills_root / "target-package"
    retained_package = skills_root / "retained-package"
    target_package.mkdir(parents=True)
    retained_package.mkdir(parents=True)

    target_document = target_package / "SKILL.md"
    target_document.write_text('---\nname: "Target package"\n---\nTarget skill.\n', encoding="utf-8")
    target_resource = target_package / "nested" / "target-resource.txt"
    target_resource.parent.mkdir()
    target_resource.write_bytes(b"target-resource")

    retained_document = retained_package / "SKILL.md"
    retained_document.write_text('---\nname: "Retained package"\n---\nRetained skill.\n', encoding="utf-8")
    retained_resource = retained_package / "retained-resource.txt"
    retained_resource.write_bytes(b"retained-resource")

    target_before = {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()}
    retained_before = {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()}
    return target_package, retained_package, target_before, retained_before


@asynccontextmanager
async def _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path: Path):
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        skills_root = tmp_path / "skills"
        monkeypatch.setattr(paths, "SKILLS_DIR", skills_root)
        packages = _write_skill_packages(skills_root)

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            user = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
            assert profile is not None
            assert user is not None
            configs = deepcopy(profile.configs)
            configs["security"]["audit_threshold"] = 5
            enabled_tools = list(configs["tool"].get("enabled_tools") or [])
            if SKILL_TOOL_NAME not in enabled_tools:
                enabled_tools.append(SKILL_TOOL_NAME)
            configs["tool"]["enabled_tools"] = enabled_tools
            profile.configs = configs
            user.is_superuser = True
            await db.commit()

        yield runtime, packages


async def _submit(runtime, *, request_id: str, message: str) -> dict:
    response = await runtime.client.post(
        "/api/v1/chat/completions",
        json={
            "session_id": runtime.session_id,
            "request_id": request_id,
            "message": message,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["session_id"] == runtime.session_id
    assert data["request_id"] == request_id
    return data


async def _read_state(runtime) -> dict:
    async with runtime.factory() as db:
        records = list((await db.execute(select(AuditRecord).where(AuditRecord.uid == runtime.uid, AuditRecord.session_id == runtime.session_id).order_by(AuditRecord.id))).scalars().all())
        record = records[0] if records else None
        details = []
        tool_results = []
        executions = []
        versions = []
        source_message = None
        if record is not None and record.id is not None:
            details = list((await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == record.id).order_by(AuditToolDetail.turn_index))).scalars().all())
            tool_results = list((await db.execute(select(Message).where(Message.audit_record_id == record.id, Message.audit_tool_call_id.is_not(None), Message.type == MessageType.TOOL_RESULT).order_by(Message.id))).scalars().all())
            executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == record.id).order_by(AuditExecutionRecord.id))).scalars().all())
            versions = list((await db.execute(select(AuditToolResultVersion).where(AuditToolResultVersion.audit_record_id == record.id).order_by(AuditToolResultVersion.original_tool_call_id, AuditToolResultVersion.version_no))).scalars().all())
            source_message = await db.get(Message, record.source_assistant_message_id)
        cards = list((await db.execute(select(Message).where(Message.uid == runtime.uid, Message.session_id == runtime.session_id, Message.type == MessageType.AUDIT_CONFIRMATION).order_by(Message.id))).scalars().all())
        messages = list((await db.execute(select(Message).where(Message.uid == runtime.uid, Message.session_id == runtime.session_id).order_by(Message.id))).scalars().all())
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.uid == runtime.uid, SessionReplyWorkItem.session_id == runtime.session_id).order_by(SessionReplyWorkItem.id))).scalars().all())
    return {
        "records": records,
        "record": record,
        "details": details,
        "tool_results": tool_results,
        "executions": executions,
        "versions": versions,
        "source_message": source_message,
        "cards": cards,
        "messages": messages,
        "works": works,
    }


@pytest.mark.asyncio
async def test_system_action_confirmation_uninstall_http_workflow_is_idempotent(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, retained_package, target_before, retained_before = packages
        source_call = InternalToolCall(
            id="model-call-id-may-be-rewritten",
            name=SKILL_TOOL_NAME,
            arguments={"operation": "uninstall", "skill_id": "target-package"},
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[source_call]))

        initial = await _submit(runtime, request_id="system-action-initial-request", message="Remove the target skill package.")
        initial_work_id = initial["work_id"]
        initial_work = await runtime.run_next()
        assert initial_work is not None
        assert initial_work.id == initial_work_id
        assert initial_work.status == SessionReplyWorkStatus.SUCCEEDED

        state = await _read_state(runtime)
        assert len(state["records"]) == 1
        record = state["record"]
        detail = state["details"][0]
        source_message = state["source_message"]
        assert record is not None
        assert source_message is not None
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
        source_tool_calls = source_internal.tool_calls or []
        assert len(source_tool_calls) == 1
        original_tool_call_id = source_tool_calls[0].id
        assert record.status is AuditRecordStatus.PENDING
        assert detail.original_tool_call_id == original_tool_call_id
        assert detail.decision_source == "system_rule"
        assert detail.score is None
        assert detail.action_request is not None
        assert detail.action_request["actor_uid"] == runtime.uid
        assert detail.action_request["action_id"] == "skills.uninstall"
        assert detail.action_request["parameters"] == {"skill_id": "target-package"}

        assert len(state["cards"]) == 1
        confirmation_payload = json.loads(state["cards"][0].content or "{}")
        assert confirmation_payload["status"] == AuditRecordStatus.PENDING.value
        assert confirmation_payload["confirmation_kind"] == "system"
        assert confirmation_payload["risk"] is None
        assert len(state["tool_results"]) == 1
        pending_result = state["tool_results"][0]
        pending_result_id = pending_result.id
        assert pending_result_id is not None
        pending_internal = InternalMessage.model_validate_json(pending_result.content or "{}")
        assert pending_result.audit_tool_call_id == original_tool_call_id
        assert pending_internal.tool_call_id == original_tool_call_id
        assert json.loads(pending_internal.content or "{}")["status"] == AuditRecordStatus.PENDING.value
        assert target_package.is_dir()
        assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before
        assert len(runtime.provider_payloads) == 1

        approval_request_id = "system-action-approval-request"
        approved = await _submit(runtime, request_id=approval_request_id, message="approve")
        confirmed_work_id = approved["work_id"]
        assert approved["submission_status"] == "approved"

        approved_state = await _read_state(runtime)
        assert approved_state["record"].status is AuditRecordStatus.EXECUTING
        assert len([work for work in approved_state["works"] if work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION]) == 1
        assert len(approved_state["executions"]) == 0
        executing_result = InternalMessage.model_validate_json(approved_state["tool_results"][0].content or "{}")
        executing_payload = json.loads(executing_result.content or "{}").get("confirmation_decision")
        assert executing_payload == "approve"

        retried = await _submit(runtime, request_id=approval_request_id, message="approve")
        assert retried["work_id"] == confirmed_work_id
        assert retried["submission_status"] == "accepted"
        retried_state = await _read_state(runtime)
        assert len(retried_state["records"]) == 1
        assert len([work for work in retried_state["works"] if work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION]) == 1
        assert len([message for message in retried_state["messages"] if message.type == MessageType.AUDIT_DECISION]) == 1
        assert retried_state["executions"] == []

        provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The target skill was removed."))
        confirmed_done = await runtime.run_next()
        assert confirmed_done is not None
        assert confirmed_done.id == confirmed_work_id
        assert confirmed_done.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads[provider_start:]) == 1
        assert not runtime.outputs

        final_state = await _read_state(runtime)
        final_record = final_state["record"]
        final_execution = final_state["executions"][0]
        final_tool_result = final_state["tool_results"][0]
        assert final_record is not None
        assert final_record.status is AuditRecordStatus.SUCCEEDED
        assert len(final_state["executions"]) == 1
        assert final_execution.status is AuditExecutionStatus.SUCCEEDED
        assert final_execution.new_tool_call_id != original_tool_call_id
        assert final_tool_result.id == pending_result_id
        assert final_tool_result.audit_tool_call_id == original_tool_call_id
        final_internal = InternalMessage.model_validate_json(final_tool_result.content or "{}")
        final_payload = json.loads(final_internal.content or "{}")
        assert final_internal.tool_call_id == original_tool_call_id
        assert final_payload["status"] == "success"
        assert final_payload["operation"] == "uninstall"
        assert final_payload["skill_id"] == "target-package"
        assert final_payload["confirmation_decision"] == "approve"
        assert json.loads(final_state["cards"][0].content or "{}")["status"] == AuditRecordStatus.SUCCEEDED.value
        assert [version.version_no for version in final_state["versions"]] == [0, 1, 2]
        assert all(version.message_id == pending_result_id for version in final_state["versions"])

        continuation_items = runtime.provider_payloads[provider_start]["input"]
        function_call_outputs = [item for item in continuation_items if item.get("type") == "function_call_output"]
        assert len(function_call_outputs) == 1
        assert json.loads(function_call_outputs[0]["output"])["tool_result"]["status"] == "success"
        assert len(runtime.provider_payloads) == 2

        result_response = await runtime.client.get(f"/api/v1/chat/reply-works/{confirmed_work_id}")
        assert result_response.status_code == 200
        result_data = result_response.json()["data"]
        assert result_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert result_data["response"]["content"] == "The target skill was removed."
        assert not target_package.exists()
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before
        assert await runtime.run_next() is None


@pytest.mark.asyncio
async def test_system_action_errors_reach_model_without_confirmation(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, retained_package, target_before, retained_before = packages
        async with runtime.factory() as db:
            user = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
            user.is_superuser = False
            await db.commit()

        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="permission-denied-uninstall-call",
                        name=SKILL_TOOL_NAME,
                        arguments={"operation": "uninstall", "skill_id": "target-package"},
                    ),
                    InternalToolCall(
                        id="unknown-uninstall-operation-call",
                        name=SKILL_TOOL_NAME,
                        arguments={"operation": "uninstall_typo", "skill_id": "target-package"},
                    ),
                ],
            )
        )
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The requested operations were checked."))

        submitted = await _submit(runtime, request_id="system-action-errors-request", message="Check the requested skill operations.")
        work = await runtime.run_next()
        assert work is not None
        assert work.id == submitted["work_id"]
        assert work.status == SessionReplyWorkStatus.SUCCEEDED
        assert len(runtime.provider_payloads) == 2

        skill_schema = next(tool for tool in runtime.provider_payloads[0]["tools"] if tool["name"] == SKILL_TOOL_NAME)
        operation_schema = skill_schema["parameters"]["properties"]["operation"]
        assert operation_schema["enum"] == ["list", "browse"]
        assert "uninstall" not in skill_schema["description"]
        assert "uninstall" not in operation_schema["description"]

        function_call_outputs = [item for item in runtime.provider_payloads[1]["input"] if item.get("type") == "function_call_output"]
        assert len(function_call_outputs) == 2
        tool_results = [json.loads(item["output"])["tool_result"] for item in function_call_outputs]
        assert [result["status"] for result in tool_results] == ["failed", "failed"]
        assert [result["error"] for result in tool_results] == [
            t(ERR_TOOL_OPERATION_PERMISSION_DENIED, tool_name=SKILL_TOOL_NAME, operation="uninstall"),
            t(ERR_TOOL_OPERATION_NOT_FOUND, tool_name=SKILL_TOOL_NAME, operation="uninstall_typo"),
        ]

        state = await _read_state(runtime)
        assert state["records"] == []
        assert state["cards"] == []
        assert state["executions"] == []
        assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before

        result_response = await runtime.client.get(f"/api/v1/chat/reply-works/{submitted['work_id']}")
        assert result_response.status_code == 200
        result_data = result_response.json()["data"]
        assert result_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert result_data["response"]["choices"][0]["message"]["content"] == "The requested operations were checked."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("close_case", "expected_status"),
    [
        pytest.param("reject", AuditRecordStatus.REJECTED, id="reject"),
        pytest.param("new-message", AuditRecordStatus.CANCELLED, id="new-message"),
        pytest.param("stop", AuditRecordStatus.CANCELLED, id="stop"),
        pytest.param("expire", AuditRecordStatus.EXPIRED, id="expire"),
    ],
)
async def test_system_action_confirmation_closes_without_executing(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    close_case: str,
    expected_status: AuditRecordStatus,
) -> None:
    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, retained_package, target_before, retained_before = packages
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="close-path-uninstall-call",
                        name=SKILL_TOOL_NAME,
                        arguments={"operation": "uninstall", "skill_id": "target-package"},
                    )
                ],
            )
        )

        initial = await _submit(
            runtime,
            request_id=f"system-action-close-{close_case}-initial",
            message="Remove the target skill package.",
        )
        initial_work = await runtime.run_next()
        assert initial_work is not None
        assert initial_work.id == initial["work_id"]
        assert initial_work.status == SessionReplyWorkStatus.SUCCEEDED

        pending_state = await _read_state(runtime)
        assert len(pending_state["records"]) == 1
        pending_record = pending_state["record"]
        assert pending_record is not None
        assert pending_record.status is AuditRecordStatus.PENDING
        assert len(pending_state["cards"]) == 1
        assert len(pending_state["tool_results"]) == 1
        pending_result_id = pending_state["tool_results"][0].id
        assert pending_result_id is not None

        if close_case == "reject":
            closed_submission = await _submit(
                runtime,
                request_id="system-action-close-reject",
                message="reject",
            )
            assert closed_submission["submission_status"] == "rejected"
        elif close_case == "new-message":
            closed_submission = await _submit(
                runtime,
                request_id="system-action-close-new-message",
                message="Keep the skill and change the task.",
            )
            assert closed_submission["submission_status"] == "cancelled"
        elif close_case == "stop":
            stopped = await runtime.client.post(
                "/api/v1/chat/sessions/stop",
                params={"session_id": runtime.session_id},
            )
            assert stopped.status_code == 200
            stopped_body = stopped.json()
            assert stopped_body["code"] == 200
            assert stopped_body["data"]["session_id"] == runtime.session_id
        else:
            async with runtime.factory() as db:
                record = (await db.execute(select(AuditRecord).where(AuditRecord.id == pending_record.id))).scalars().one()
                record.expires_at = get_local_time() - timedelta(seconds=2)
                await db.commit()
                await expire_confirmation_by_session(db, uid=runtime.uid, session_id=runtime.session_id)

        closed_state = await _read_state(runtime)
        closed_record = closed_state["record"]
        assert closed_record is not None
        assert closed_record.id == pending_record.id
        assert closed_record.status is expected_status
        assert len(closed_state["cards"]) == 1
        assert json.loads(closed_state["cards"][0].content or "{}")["status"] == expected_status.value
        assert len(closed_state["tool_results"]) == 1
        assert closed_state["tool_results"][0].id == pending_result_id
        closed_result = InternalMessage.model_validate_json(closed_state["tool_results"][0].content or "{}")
        assert json.loads(closed_result.content or "{}")["status"] == expected_status.value
        assert closed_state["executions"] == []
        assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before

        approved = await _submit(
            runtime,
            request_id=f"system-action-close-{close_case}-stale-approve",
            message="approve",
        )
        if close_case in {"reject", "new-message"}:
            expected_submission_status = "queued"
        else:
            expected_submission_status = "accepted"
        assert approved["submission_status"] == expected_submission_status
        after_approve = await _read_state(runtime)
        assert after_approve["record"].id == pending_record.id
        assert after_approve["record"].status is expected_status
        assert not any(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in after_approve["works"])
        ordinary_work = next(work for work in after_approve["works"] if work.id == approved["work_id"])
        assert ordinary_work.work_type == SessionReplyWorkType.FOREGROUND_REPLY
        assert ordinary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert target_package.exists()
        assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recheck_case",
    [
        pytest.param("permission-before-approve", id="permission-before-approve"),
        pytest.param("permission-after-approve", id="permission-after-approve"),
        pytest.param("target-replaced-after-approve", id="target-replaced-after-approve"),
    ],
)
async def test_system_action_confirmation_rechecks_permission_and_target(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recheck_case: str,
) -> None:
    async with _prepared_runtime(image_chat_runtime, monkeypatch, tmp_path) as (runtime, packages):
        target_package, retained_package, target_before, retained_before = packages
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="recheck-uninstall-call",
                        name=SKILL_TOOL_NAME,
                        arguments={"operation": "uninstall", "skill_id": "target-package"},
                    )
                ],
            )
        )
        initial = await _submit(
            runtime,
            request_id=f"system-action-recheck-{recheck_case}-initial",
            message="Remove the target skill package.",
        )
        initial_work = await runtime.run_next()
        assert initial_work is not None
        assert initial_work.id == initial["work_id"]
        assert initial_work.status == SessionReplyWorkStatus.SUCCEEDED

        pending_state = await _read_state(runtime)
        pending_record = pending_state["record"]
        assert pending_record is not None
        assert pending_record.status is AuditRecordStatus.PENDING
        assert len(pending_state["cards"]) == 1
        assert len(pending_state["tool_results"]) == 1

        if recheck_case == "permission-before-approve":
            async with runtime.factory() as db:
                user = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
                user.is_superuser = False
                await db.commit()

            rejected = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "system-action-recheck-permission-before-approve",
                    "message": "approve",
                },
            )
            assert rejected.status_code == 403
            after_rejection = await _read_state(runtime)
            assert after_rejection["record"].status is AuditRecordStatus.PENDING
            assert not any(work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION for work in after_rejection["works"])
            assert after_rejection["executions"] == []
            assert target_package.exists()
            assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
            assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before
            return

        approved = await _submit(
            runtime,
            request_id=f"system-action-recheck-{recheck_case}-approve",
            message="approve",
        )
        assert approved["submission_status"] == "approved"

        moved_package: Path | None = None
        if recheck_case == "permission-after-approve":
            async with runtime.factory() as db:
                user = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
                user.is_superuser = False
                await db.commit()
            expected_error = t(
                ERR_TOOL_OPERATION_PERMISSION_DENIED,
                tool_name=SKILL_TOOL_NAME,
                operation="uninstall",
            )
        else:
            moved_package = target_package.parent / "target-package-moved"
            target_package.rename(moved_package)
            shutil.copytree(moved_package, target_package)
            expected_error = t(ERR_SYSTEM_ACTION_TARGET_CHANGED)

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="The operation could not be completed."))
        confirmed_work = await runtime.run_next()
        assert confirmed_work is not None
        assert confirmed_work.id == approved["work_id"]

        final_state = await _read_state(runtime)
        final_record = final_state["record"]
        assert final_record is not None
        assert final_record.status is AuditRecordStatus.FAILED
        assert len(final_state["executions"]) == 1
        assert all(execution.status is AuditExecutionStatus.CANCELLED for execution in final_state["executions"])
        final_result = InternalMessage.model_validate_json(final_state["tool_results"][0].content or "{}")
        final_payload = json.loads(final_result.content or "{}")
        assert final_payload["status"] == "failed"
        assert final_payload["error"] == expected_error
        assert json.loads(final_state["cards"][0].content or "{}")["status"] == AuditRecordStatus.FAILED.value
        assert target_package.exists()
        assert {item.relative_to(target_package): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert {item.relative_to(retained_package): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before
        if moved_package is not None:
            assert moved_package.exists()
            assert {item.relative_to(moved_package): item.read_bytes() for item in moved_package.rglob("*") if item.is_file()} == target_before
