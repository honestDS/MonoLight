from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

import app.core.audit.service as audit_service
from app.core.action_control.contracts import PreparedAction
from app.core.action_control.service import describe_action, prepare_action, requires_action_confirmation, validate_prepared_action
from app.core.action_control.tools import get_tool_action_request
from app.core.audit.integrity import build_tool_round_integrity_snapshot, serialize_tool_arguments
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.audit.visibility import obscure_audit_tool_result
from app.core.confirmation.lifecycle import cancel_confirmation_by_session
from app.core.confirmation.projection import update_confirmation_message_status
from app.core.constants import (
    AUDIT_HIGH_RISK_SCORE,
    END_SESSION_SCOPE_FOREGROUND,
    END_SESSION_SCOPE_TASK,
    ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED,
    ERR_SYSTEM_ACTION_PERMISSION_DENIED,
    ERR_TOOL_SHELL_BLACKLISTED,
    MSG_AUDIT_CONFIRMATION_IM,
    MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK,
    MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM,
    MSG_AUDIT_ROUND_SKIPPED,
    MSG_AUDIT_STATUS_CANCELLED,
    MSG_SYSTEM_ACTION_CONFIRMATION_IM,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SHELL_TOOL_NAME,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.exceptions import BaseBusinessException, ForbiddenException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.tools import tool_requires_audit
from app.core.tools.shell import ShellExecutor
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkSpec
from app.models.audit import AuditFailureType, AuditRecordStatus, AuditToolConclusion
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkType

logger = get_logger(__name__)

__all__ = ["ToolRoundResult", "prepare_tool_round"]


@dataclass(frozen=True, slots=True)
class ToolRoundResult:
    audit_record_id: int
    status: AuditRecordStatus
    tool_results: tuple[InternalMessage, ...]
    confirmation_payload: dict[str, Any] | None = None

    @property
    def may_execute(self) -> bool:
        return self.status == AuditRecordStatus.PASSED


async def _resolve_work_context(db: AsyncSession, *, uid: str, session_id: str, execution_scope: str) -> tuple[int | None, str | None, bool]:
    scheduled = execution_scope == END_SESSION_SCOPE_TASK
    claim = (getattr(db, "info", None) or {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
    if claim is None:
        return None, None, scheduled
    if not isinstance(claim, (tuple, list)) or len(claim) != 2 or not isinstance(claim[0], int) or isinstance(claim[0], bool) or claim[0] <= 0:
        return None, None, scheduled
    current = await session_reply_work_item_crud.get(db, claim[0])
    if current is None or current.id != claim[0] or current.uid != uid or current.session_id != session_id:
        return None, None, scheduled
    scheduled = False
    profile_id, source_work_id = current.profile_id, current.id
    task_instruction: str | None = None
    visited: set[int] = set()
    while current is not None:
        if not isinstance(current.id, int) or isinstance(current.id, bool) or current.id <= 0 or current.id in visited:
            break
        if current.uid != uid or current.session_id != session_id or current.profile_id != profile_id:
            break
        visited.add(current.id)
        parent_work_id: int | None = None
        if current.work_type == SessionReplyWorkType.AGENT_WORK:
            state = current.execution_state if isinstance(current.execution_state, dict) else {}
            try:
                spec = AgentWorkSpec.model_validate(state.get("agent_work"))
            except Exception:
                break
            if task_instruction is None:
                task_instruction = spec.instruction
            if current.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN:
                scheduled = True
            if current.source_type == SessionReplySourceType.PARENT_WORK:
                parent_work_id = spec.parent_work_id
        elif current.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION and current.source_type == SessionReplySourceType.AUDIT_RECORD:
            try:
                audit_record_id = int(current.source_id)
            except (TypeError, ValueError):
                break
            record = await audit_crud.get_record(db, audit_record_id)
            if record is None or record.uid != uid or record.session_id != session_id:
                break
            parent_work_id = record.source_work_id
        else:
            break
        if not isinstance(parent_work_id, int) or isinstance(parent_work_id, bool) or parent_work_id <= 0:
            break
        current = await session_reply_work_item_crud.get(db, parent_work_id)
    return source_work_id, task_instruction, scheduled


async def _prepare_system_actions(
    db: AsyncSession,
    cfg: ProfileConfig,
    requests: list[tuple[InternalToolCall, str, dict[str, Any]]],
    results: dict[str, dict[str, Any]],
    *,
    uid: str,
    actor_uid: str | None,
    language: str,
    scheduled: bool,
    prepared_action_requests: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    for tool_call, action_id, parameters in requests:
        try:
            if prepared_action_requests is None:
                prepared = await prepare_action(db, action_id=action_id, parameters=parameters, uid=uid, actor_uid=actor_uid)
            else:
                prepared = PreparedAction.from_dict(prepared_action_requests[tool_call.id])
            if prepared.action_id != action_id or prepared.parameters != parameters:
                raise ForbiddenException(ERR_SYSTEM_ACTION_CONFIRMATION_REQUIRED)
            if prepared_action_requests is not None:
                await validate_prepared_action(db, prepared=prepared, uid=uid, actor_uid=actor_uid)
            reason = describe_action(prepared, locale=language)
            confirmation_required = requires_action_confirmation(action_id, cfg)
            if scheduled and confirmation_required:
                conclusion = AuditToolConclusion.BLOCKED
            elif confirmation_required:
                conclusion = AuditToolConclusion.PENDING
            else:
                conclusion = AuditToolConclusion.PASSED
            results[tool_call.id].update(score=None, reason=reason, decision_source="system_rule", action_request=prepared.to_dict(), conclusion=conclusion)
        except Exception as exc:
            if isinstance(exc, BaseBusinessException):
                reason = exc.render_message()
            else:
                reason = str(exc) or exc.__class__.__name__
            public_error = None
            if isinstance(exc, ForbiddenException) and exc.message == ERR_SYSTEM_ACTION_PERMISSION_DENIED:
                public_error = reason
            logger.error("System action preparation failed", tool_call_id=tool_call.id, action_id=action_id, parameters=parameters, error=reason)
            results[tool_call.id].update(score=None, reason=reason, decision_source="system_rule", action_request={"action_id": action_id, "parameters": dict(parameters), "error": reason}, conclusion=AuditToolConclusion.BLOCKED, public_error=public_error)
    return results


def _aggregate(results: dict[str, dict[str, Any]]) -> AuditRecordStatus:
    conclusions = {item["conclusion"] for item in results.values()}
    if AuditToolConclusion.BLOCKED in conclusions:
        return AuditRecordStatus.BLOCKED
    if AuditToolConclusion.AUDIT_FAILED in conclusions:
        return AuditRecordStatus.AUDIT_FAILED
    if AuditToolConclusion.PENDING in conclusions:
        return AuditRecordStatus.PENDING
    return AuditRecordStatus.PASSED


def _blocked_shell_error(tool_call: InternalToolCall) -> str | None:
    if tool_call.name != SHELL_TOOL_NAME:
        return None
    command = str((tool_call.arguments or {}).get("command", ""))
    blacklisted = ShellExecutor.check_blacklist(command)
    if blacklisted is None:
        return None
    return t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted)


def _result_message(tool_call: InternalToolCall, status: AuditRecordStatus, *, public_error: str | None = None) -> InternalMessage:
    payload: dict[str, Any] = {"status": status.value}
    shell_error = None
    if status == AuditRecordStatus.BLOCKED:
        shell_error = _blocked_shell_error(tool_call)
    if shell_error is not None:
        payload["error"] = shell_error
    if public_error is not None:
        payload = {"status": "failed", "tool_name": tool_call.name, "error": public_error}
    return InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=json.dumps(obscure_audit_tool_result(payload), ensure_ascii=False))


async def _pending_summary(
    db: AsyncSession,
    cfg: ProfileConfig,
    *,
    system_requests: list[tuple[InternalToolCall, str, dict[str, Any]]],
    ordinary_calls: list[InternalToolCall],
    results: dict[str, dict[str, Any]],
    evaluation: Any,
    workdir: Path,
    read_budget: Any,
    task_instruction: str | None,
) -> tuple[str, dict[str, Any] | None]:
    system_summary = "\n".join(results[call.id]["reason"] for call, _, _ in system_requests)
    if not ordinary_calls:
        return system_summary, None
    try:
        ordinary_summary, summary_context = await audit_service.summarize_evaluation(
            db,
            cfg,
            evaluation,
            ordinary_calls,
            workdir,
            read_budget=read_budget,
            task_instruction=task_instruction,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ordinary_summary = t(
            MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK,
            locale=cfg.security.audit_report_language,
        )
        summary_context = {"fallback": True, "error": str(exc) or exc.__class__.__name__}
    return "\n".join(part for part in (system_summary, ordinary_summary) if part), summary_context


def _confirmation_payload(
    cfg: ProfileConfig,
    *,
    audit_record_id: int,
    summary: str,
    status: AuditRecordStatus,
    expires_at,
    system_requests: list[tuple[InternalToolCall, str, dict[str, Any]]],
    ordinary_calls: list[InternalToolCall],
    results: dict[str, dict[str, Any]],
    language: str,
) -> dict[str, Any] | None:
    if status != AuditRecordStatus.PENDING:
        return None
    if system_requests and ordinary_calls:
        confirmation_kind = "mixed"
    elif system_requests:
        confirmation_kind = "system"
    else:
        confirmation_kind = "audit"
    scores = [results[call.id]["score"] for call in ordinary_calls if isinstance(results[call.id]["score"], int) and not isinstance(results[call.id]["score"], bool)]
    if system_requests and not ordinary_calls:
        risk = None
    else:
        risk = max([cfg.security.audit_threshold, *scores])
    if risk is not None and risk >= AUDIT_HIGH_RISK_SCORE:
        confirmation_mode = "high_risk_override"
    else:
        confirmation_mode = "standard"
    timeout_seconds = cfg.security.audit_confirmation_timeout_seconds
    payload: dict[str, Any] = {
        "type": "audit_confirmation",
        "audit_record_id": audit_record_id,
        "summary": summary,
        "risk": risk,
        "status": status.value,
        "confirmation_mode": confirmation_mode,
        "confirmation_kind": confirmation_kind,
        "expires_at": expires_at.isoformat(),
    }
    if confirmation_kind == "system":
        message_key = MSG_SYSTEM_ACTION_CONFIRMATION_IM
    elif confirmation_mode == "high_risk_override":
        message_key = MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM
    else:
        message_key = MSG_AUDIT_CONFIRMATION_IM
    payload["plain_text"] = t(message_key, locale=language, summary=summary, score=risk, expires_in_seconds=timeout_seconds)
    return payload


async def prepare_tool_round(
    db: AsyncSession,
    *,
    cfg: ProfileConfig,
    tool_calls: list[InternalToolCall],
    source_assistant_message_id: int,
    uid: str,
    operator_username: str,
    session_id: str,
    source: str,
    language: str,
    execution_scope: str = END_SESSION_SCOPE_FOREGROUND,
    working_directory: str | Path | None = None,
    actor_uid: str | None = None,
    prepared_action_requests: dict[str, Any] | None = None,
) -> ToolRoundResult | None:
    audit_configured = audit_service.is_audit_configured(cfg)
    system_requests: list[tuple[InternalToolCall, str, dict[str, Any]]] = []
    ordinary_calls: list[InternalToolCall] = []
    for tool_call in tool_calls:
        mapped = get_tool_action_request(tool_call.name, dict(tool_call.arguments or {}))
        if mapped is not None:
            system_requests.append((tool_call, mapped[0], mapped[1]))
        elif audit_configured and tool_requires_audit(tool_call.name, dict(tool_call.arguments or {})):
            ordinary_calls.append(tool_call)
    if not system_requests and not ordinary_calls:
        return None

    source_work_id, task_instruction, scheduled = await _resolve_work_context(
        db,
        uid=uid,
        session_id=session_id,
        execution_scope=execution_scope,
    )
    if not scheduled:
        await cancel_confirmation_by_session(db, uid=uid, session_id=session_id, locale=language)
    workdir = Path(working_directory or get_user_temp_dir(os.getcwd(), uid)).resolve(strict=False)
    payload_calls = [{"id": call.id, "name": call.name, "arguments": dict(call.arguments or {})} for call in tool_calls]
    round_snapshot = build_tool_round_integrity_snapshot(
        tool_calls=payload_calls,
        uid=uid,
        session_id=session_id,
        working_directory=workdir,
    )
    record = await audit_crud.create_preparing(
        db,
        uid=uid,
        operator_username=operator_username,
        session_id=session_id,
        source=source,
        language=language,
        execution_scope=execution_scope,
        source_work_id=source_work_id,
        source_assistant_message_id=source_assistant_message_id,
        working_directory=str(workdir),
        round_arguments_hash=round_snapshot.round_sha256,
        tool_count=len(tool_calls),
    )
    audit_record_id = record.id
    try:
        logger.bind(
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            source=source,
            model_id=cfg.security.audit_model_id or "-",
            tool_count=len(tool_calls),
        ).info(
            t(
                "LOG_AUDIT_ROUND_STARTED",
                audit_record_id=audit_record_id,
                model_id=cfg.security.audit_model_id or "-",
                tool_count=len(tool_calls),
                source=source,
            )
        )
        skipped_reason = t(MSG_AUDIT_ROUND_SKIPPED)
        results: dict[str, dict[str, Any]] = {
            call.id: {
                "score": 0,
                "reason": skipped_reason,
                "decision_source": "none",
                "action_request": None,
                "conclusion": AuditToolConclusion.PASSED,
                "file_snapshots": [],
            }
            for call in tool_calls
        }
        results = await _prepare_system_actions(
            db,
            cfg,
            system_requests,
            results,
            uid=uid,
            actor_uid=actor_uid,
            language=language,
            scheduled=scheduled,
            prepared_action_requests=prepared_action_requests,
        )
        system_blocked = any(results[call.id]["conclusion"] == AuditToolConclusion.BLOCKED for call, _, _ in system_requests)
        read_budget = audit_service.AuditReadBudget(limit=cfg.security.audit_depth)
        evaluation = None
        request_context: dict[str, Any] = {"messages": []}
        response_context: dict[str, Any] = {}
        failure_type: AuditFailureType | None = None
        error_reason: str | None = None
        if system_blocked:
            response_context = {"system_action_blocked": True}
        elif ordinary_calls:
            try:
                evaluation = await audit_service.evaluate_tool_calls(
                    db,
                    cfg,
                    ordinary_calls,
                    workdir,
                    uid=uid,
                    session_id=session_id,
                    read_budget=read_budget,
                    task_instruction=task_instruction,
                    is_scheduled_task=scheduled,
                    turn_indexes={call.id: index for index, call in enumerate(tool_calls)},
                )
                request_context, response_context = evaluation.request_context, evaluation.response_context
                for call in ordinary_calls:
                    evaluated = evaluation.results[call.id]
                    results[call.id] = {
                        "score": evaluated.get("score"),
                        "reason": str(evaluated.get("reason") or ""),
                        "decision_source": "model_audit",
                        "action_request": None,
                        "conclusion": evaluation.conclusions[call.id],
                        "file_snapshots": evaluation.file_snapshots.get(call.id, []),
                    }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure_type, error_reason = AuditFailureType.AUDIT_SERVICE_FAILED, str(exc) or exc.__class__.__name__
                response_context = {"error": error_reason}
                for item in results.values():
                    item.update(score=None, reason=error_reason, conclusion=AuditToolConclusion.AUDIT_FAILED)
                for call in ordinary_calls:
                    results[call.id]["decision_source"] = "model_audit"

        status = _aggregate(results)
        intent_summary: str | None = None
        summary_context: dict[str, Any] | None = None
        expires_at = None
        if status == AuditRecordStatus.PENDING:
            intent_summary, summary_context = await _pending_summary(
                db,
                cfg,
                system_requests=system_requests,
                ordinary_calls=ordinary_calls,
                results=results,
                evaluation=evaluation,
                workdir=workdir,
                read_budget=read_budget,
                task_instruction=task_instruction,
            )
            expires_at = get_local_time() + timedelta(seconds=cfg.security.audit_confirmation_timeout_seconds)

        if evaluation is not None:
            server_confirmation_reasons: dict[str, list[dict[str, Any]]] = dict(evaluation.confirmation_reasons)
        else:
            server_confirmation_reasons = {}
        for call, _, _ in system_requests:
            if results[call.id]["conclusion"] == AuditToolConclusion.PENDING:
                server_confirmation_reasons[call.id] = [{"code": "system_action", "message": results[call.id]["reason"]}]
        details: list[dict[str, Any]] = []
        for snapshot in round_snapshot.tool_calls:
            result = results[snapshot.tool_call_id]
            details.append(
                {
                    "original_tool_call_id": snapshot.tool_call_id,
                    "turn_index": snapshot.turn_index,
                    "tool_name": snapshot.tool_name,
                    "conclusion": result["conclusion"].value,
                    "score": result["score"],
                    "reason": result["reason"],
                    "arguments_hash": snapshot.arguments_sha256,
                    "arguments_summary": serialize_tool_arguments(snapshot.arguments),
                    "file_snapshots": result["file_snapshots"],
                    "decision_source": result["decision_source"],
                    "action_request": result["action_request"],
                    "server_confirmation_reasons": server_confirmation_reasons.get(snapshot.tool_call_id, []),
                }
            )
        context_payload: dict[str, Any] = {
            "audit_record_id": audit_record_id,
            "source_assistant_message_id": source_assistant_message_id,
            "source_work_id": source_work_id,
            "round_arguments_hash": round_snapshot.round_sha256,
            "tool_calls": payload_calls,
            "scoring_request": request_context,
            "scoring_response": response_context,
            "results": details,
            "audited_tool_call_ids": [call.id for call in ordinary_calls],
            "audit_depth": read_budget.limit,
            "evidence_calls": read_budget.calls,
            "file_read_calls": read_budget.calls - read_budget.shell_calls,
            "shell_calls": read_budget.shell_calls,
            "server_confirmation_reasons": server_confirmation_reasons,
        }
        if summary_context is not None:
            context_payload["summary"] = summary_context
        if task_instruction is not None:
            context_payload["task_instruction"] = task_instruction
        if system_requests:
            context_payload["extra"] = {
                "system_actions": [
                    {
                        "tool_call_id": call.id,
                        "action_id": action_id,
                        "parameters": parameters,
                        "action_request": results[call.id]["action_request"],
                        "description": results[call.id]["reason"],
                    }
                    for call, action_id, parameters in system_requests
                ]
            }

        await ensure_session_reply_work_claim(db, commit=True)
        persisted = await persist_prepared_audit_round(
            db,
            audit_record_id=audit_record_id,
            uid=uid,
            status=status,
            context_payload=context_payload,
            tool_details=details,
            intent_summary=intent_summary,
            failure_type=failure_type,
            error_reason=error_reason,
            expires_at=expires_at,
            create_confirmation_claim=status != AuditRecordStatus.PENDING,
        )
        if not persisted:
            status, error_reason = AuditRecordStatus.AUDIT_FAILED, "audit persistence failed"
        max_score = max((item["score"] for item in results.values() if isinstance(item["score"], int)), default="-")
        logger.bind(
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            source=source,
            status=status.value,
            tool_scores={call.id: results[call.id]["score"] for call in tool_calls},
            max_score=max_score,
            summary=intent_summary or "-",
        ).info(
            t(
                "LOG_AUDIT_ROUND_COMPLETED",
                audit_record_id=audit_record_id,
                status=status.value,
                max_score=max_score,
                summary=intent_summary or "-",
            )
        )
        confirmation_payload = _confirmation_payload(
            cfg,
            audit_record_id=audit_record_id,
            summary=intent_summary or "",
            status=status,
            expires_at=expires_at,
            system_requests=system_requests,
            ordinary_calls=ordinary_calls,
            results=results,
            language=language,
        )
        if status == AuditRecordStatus.PASSED:
            tool_results: tuple[InternalMessage, ...] = ()
        else:
            tool_results = tuple(_result_message(call, status, public_error=results[call.id].get("public_error")) for call in tool_calls)
        return ToolRoundResult(audit_record_id, status, tool_results, confirmation_payload)
    except asyncio.CancelledError:
        await db.rollback()
        await audit_crud.cancel_unexecuted(
            db,
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            error_reason=t(MSG_AUDIT_STATUS_CANCELLED, locale=language),
        )
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
        raise
