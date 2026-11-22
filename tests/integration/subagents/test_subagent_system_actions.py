import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import app.core.paths as paths
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_TOOL_OPERATION_PERMISSION_DENIED,
    SKILL_TOOL_NAME,
)
from app.core.i18n import t
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolDetail,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkCategory,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import SubagentWork
from app.models.user import User


def _write_skill_packages(skills_root: Path) -> tuple[Path, Path, dict[str, bytes], dict[str, bytes]]:
    target_package = skills_root / "target-package"
    retained_package = skills_root / "retained-package"
    target_package.mkdir(parents=True)
    retained_package.mkdir(parents=True)

    (target_package / "SKILL.md").write_text(
        "---\nname: target-package\n---\nTarget skill.\n",
        encoding="utf-8",
    )
    target_resource = target_package / "nested" / "target-resource.txt"
    target_resource.parent.mkdir()
    target_resource.write_bytes(b"target-resource")

    (retained_package / "SKILL.md").write_text(
        "---\nname: retained-package\n---\nRetained skill.\n",
        encoding="utf-8",
    )
    (retained_package / "retained-resource.txt").write_bytes(b"retained-resource")

    def _snapshot(package: Path) -> dict[str, bytes]:
        return {item.relative_to(package).as_posix(): item.read_bytes() for item in package.rglob("*") if item.is_file()}

    return target_package, retained_package, _snapshot(target_package), _snapshot(retained_package)


def _tool_schema(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
    for tool in payload.get("tools", []):
        if not isinstance(tool, dict):
            continue
        if tool.get("name") == tool_name:
            return tool
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == tool_name:
            return function
    raise AssertionError(f"{tool_name!r} is not exposed in the provider request")


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_is_admin", [False, True], ids=["ordinary-actor", "admin-actor"])
async def test_subagent_inherits_actual_actor_permissions_for_skill_uninstall_confirmation(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    actor_is_admin: bool,
) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        skills_root = tmp_path / "skills"
        monkeypatch.setattr(paths, "SKILLS_DIR", skills_root)
        target_package, retained_package, target_before, retained_before = _write_skill_packages(skills_root)

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            owner = (await db.execute(select(User).where(User.uid == runtime.uid))).scalars().one()
            assert profile is not None
            assert owner is not None
            configs = deepcopy(profile.configs)
            configs["security"]["audit_threshold"] = 5
            enabled_tools = list(configs["tool"].get("enabled_tools") or [])
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*enabled_tools, AGENT_WORK_TOOL_NAME]))
            profile.configs = ProfileConfig.model_validate(configs).model_dump(mode="json")
            owner.is_superuser = not actor_is_admin
            db.add(
                User(
                    uid="actual-actor",
                    username="actual_actor",
                    is_active=True,
                    is_superuser=actor_is_admin,
                )
            )
            await db.commit()

        async def _submit(message: str, request_id: str):
            async with runtime.factory() as db:
                profile = await db.get(Profile, runtime.profile.id)
                assert profile is not None
                return await session_reply_queue_manager.submit_user_message(
                    db,
                    uid=runtime.uid,
                    session_id=runtime.session_id,
                    profile=profile,
                    message=message,
                    attachments=None,
                    source="http",
                    request_id=request_id,
                    actor_uid="actual-actor",
                    actor_username="actual_actor",
                )

        child_instruction = "独立处理 target-package 技能卸载；若当前 MonoLight 账号权限不足则说明原因，否则完成后报告。"
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="parent-create-subagent-system-action",
                        name=AGENT_WORK_TOOL_NAME,
                        arguments={
                            "operation": "create",
                            "name": "技能卸载权限测试",
                            "message": child_instruction,
                        },
                    )
                ],
            )
        )
        _message, parent_work, submission_status, _events = await _submit(
            "将技能卸载任务交给子代理并等待结果。",
            f"subagent-system-action-parent-{actor_is_admin}",
        )
        assert parent_work.id is not None
        assert submission_status == "accepted"
        parent_work_id = parent_work.id

        parent_done = await runtime.run_next(independent_only=False)
        assert parent_done is not None
        assert parent_done.id == parent_work_id
        assert parent_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            assert parent is not None
            child = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_work_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            assert child.id is not None
            assert child.subagent_id is not None
            assert child.uid == parent.uid
            assert child.session_id == parent.session_id
            assert child.profile_id == parent.profile_id
            assert child.status == SessionReplyWorkStatus.READY_FOR_LLM
            child_work_id = child.id

        child_call = InternalToolCall(
            id="child-skill-uninstall-system-action",
            name=SKILL_TOOL_NAME,
            arguments={"operation": "uninstall", "skill_id": "target-package"},
        )
        child_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[child_call]))
        child_completion = "子代理完成：当前 MonoLight 账号权限不足，无法卸载。"
        if not actor_is_admin:
            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=child_completion))

        child_done = await runtime.run_next(independent_only=True)
        assert child_done is not None
        assert child_done.id == child_work_id
        assert child_done.status == SessionReplyWorkStatus.SUCCEEDED

        child_payloads = runtime.provider_payloads[child_provider_start:]
        assert child_payloads
        child_skill_schema = _tool_schema(child_payloads[0], SKILL_TOOL_NAME)
        child_operation_schema = child_skill_schema["parameters"]["properties"]["operation"]

        if not actor_is_admin:
            assert "uninstall" not in child_operation_schema["enum"]
            permission_error = t(
                ERR_TOOL_OPERATION_PERMISSION_DENIED,
                tool_name=SKILL_TOOL_NAME,
                operation="uninstall",
            )
            assert len(child_payloads) == 2
            assert any(permission_error in json.dumps(payload, ensure_ascii=False) for payload in child_payloads[1:])

            async with runtime.factory() as db:
                records = list(
                    (
                        await db.execute(
                            select(AuditRecord).where(
                                AuditRecord.uid == runtime.uid,
                                AuditRecord.session_id == runtime.session_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                confirmations = list(
                    (
                        await db.execute(
                            select(Message).where(
                                Message.uid == runtime.uid,
                                Message.session_id == runtime.session_id,
                                Message.type == MessageType.AUDIT_CONFIRMATION,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                parent = await db.get(SessionReplyWorkItem, parent_work_id)
                child = await db.get(SessionReplyWorkItem, child_work_id)
                round_row = await db.get(SubagentWork, child_work_id)
            assert records == []
            assert confirmations == []
            assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert child is not None and child.status == SessionReplyWorkStatus.SUCCEEDED
            assert round_row is not None and round_row.result is not None
            assert round_row.result["completed"] is True
            assert round_row.result["status"] == "succeeded"
            assert round_row.result["result"]["content"] == child_completion
            assert target_package.is_dir()
            assert {item.relative_to(target_package).as_posix(): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
            assert {item.relative_to(retained_package).as_posix(): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before
            return

        assert "uninstall" in child_operation_schema["enum"]
        assert len(child_payloads) == 1

        async with runtime.factory() as db:
            records = list(
                (
                    await db.execute(
                        select(AuditRecord).where(
                            AuditRecord.uid == runtime.uid,
                            AuditRecord.session_id == runtime.session_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(records) == 1
            record = records[0]
            assert record.id is not None
            record_id = record.id
            detail = (await db.execute(select(AuditToolDetail).where(AuditToolDetail.audit_record_id == record.id))).scalars().one()
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            round_row = await db.get(SubagentWork, child_work_id)
            confirmation = (
                (
                    await db.execute(
                        select(Message).where(
                            Message.uid == runtime.uid,
                            Message.session_id == runtime.session_id,
                            Message.type == MessageType.AUDIT_CONFIRMATION,
                        )
                    )
                )
                .scalars()
                .one()
            )
        assert record.status == AuditRecordStatus.PENDING
        assert record.source_work_id == child_work_id
        assert record.operator_username == "actual_actor"
        assert detail.decision_source == "system_rule"
        assert detail.action_request is not None
        assert detail.action_request["actor_uid"] == "actual-actor"
        assert detail.action_request["action_id"] == "skills.uninstall"
        assert detail.action_request["parameters"] == {"skill_id": "target-package"}
        assert json.loads(confirmation.content or "{}")["status"] == AuditRecordStatus.PENDING.value
        assert parent is not None and parent.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert child is not None and child.status == SessionReplyWorkStatus.SUCCEEDED
        assert round_row is not None and round_row.result is None
        assert round_row.waiting_confirmation is True
        assert target_package.is_dir()
        assert {item.relative_to(target_package).as_posix(): item.read_bytes() for item in target_package.rglob("*") if item.is_file()} == target_before
        assert {item.relative_to(retained_package).as_posix(): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before

        _message, confirmed_work, submission_status, _events = await _submit(
            "approve",
            "subagent-system-action-approval",
        )
        assert submission_status == "approved"
        assert confirmed_work.id is not None
        assert confirmed_work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION
        assert confirmed_work.work_category == SessionReplyWorkCategory.SUBAGENT
        assert confirmed_work.subagent_id == child.subagent_id
        assert confirmed_work.execution_state["child_work_id"] == child_work_id
        assert confirmed_work.execution_state["actor_uid"] == "actual-actor"
        confirmed_work_id = confirmed_work.id

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="子代理已完成技能卸载。"))
        confirmed_done = await runtime.run_next(independent_only=True)
        assert confirmed_done is not None
        assert confirmed_done.id == confirmed_work_id
        assert confirmed_done.status == SessionReplyWorkStatus.SUCCEEDED

        async with runtime.factory() as db:
            record = await db.get(AuditRecord, record_id)
            assert record is not None
            execution = (
                (
                    await db.execute(
                        select(AuditExecutionRecord).where(
                            AuditExecutionRecord.audit_record_id == record_id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            parent = await db.get(SessionReplyWorkItem, parent_work_id)
            child = await db.get(SessionReplyWorkItem, child_work_id)
            confirmed = await db.get(SessionReplyWorkItem, confirmed_work_id)
            round_row = await db.get(SubagentWork, child_work_id)
        assert record.status == AuditRecordStatus.SUCCEEDED
        assert execution.status == AuditExecutionStatus.SUCCEEDED
        assert parent is not None and parent.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert child is not None and child.status == SessionReplyWorkStatus.SUCCEEDED
        assert confirmed is not None and confirmed.status == SessionReplyWorkStatus.SUCCEEDED
        assert round_row is not None and round_row.result is not None
        assert round_row.current_work_id == confirmed_work_id
        assert round_row.result["completed"] is True
        assert round_row.result["status"] == "succeeded"
        assert round_row.result["result"]["content"] == "子代理已完成技能卸载。"
        assert not target_package.exists()
        assert {item.relative_to(retained_package).as_posix(): item.read_bytes() for item in retained_package.rglob("*") if item.is_file()} == retained_before

        parent_provider_start = len(runtime.provider_payloads)
        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content="父任务已收到子代理结果并完成。"))
        parent_finished = await runtime.run_next(independent_only=False)
        assert parent_finished is not None
        assert parent_finished.id == parent_work_id
        assert parent_finished.status == SessionReplyWorkStatus.SUCCEEDED
        assert any("子代理已完成技能卸载。" in json.dumps(payload, ensure_ascii=False) for payload in runtime.provider_payloads[parent_provider_start:])
