import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.context_budget import (
    build_read_tool_message,
    fit_file_tool_payload_to_context,
    fit_read_result_to_context,
    get_audit_tool_schemas,
    read_token_budget,
)
from app.core.audit.integrity import create_file_integrity_snapshot
from app.core.channel.router import get_model_entry
from app.core.constants import (
    AUDIT_HIGH_RISK_SCORE,
    ERR_AUDIT_CHANNEL_UNAVAILABLE,
    ERR_AUDIT_CONFIG_MISSING,
    ERR_AUDIT_FILE_CHECKS_INVALID,
    ERR_AUDIT_FILE_EVIDENCE_INSUFFICIENT,
    ERR_AUDIT_FILE_READ_LIMIT_EXCEEDED,
    ERR_AUDIT_FILE_ROUNDS_EXCEEDED,
    ERR_AUDIT_REASON_INVALID,
    ERR_AUDIT_RESPONSE_JSON_INVALID,
    ERR_AUDIT_RESULT_COUNT_MISMATCH,
    ERR_AUDIT_RESULT_INVALID,
    ERR_AUDIT_RESULT_MAPPING_MISMATCH,
    ERR_AUDIT_ROUND_FILE_CONFLICT,
    ERR_AUDIT_SCORE_INVALID,
    ERR_TOOL_SHELL_BLACKLISTED,
    ERR_TOOL_WRITE_PROTECTED_DIRECTORY,
    FILE_TOOL_NAME,
    MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK,
    MSG_AUDIT_ROUND_SKIPPED,
    READ_TEXT_FILE_TOOL_NAME,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    TOOL_WORKFLOW_AUDIT,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import (
    AUDIT_BATCH_PROMPT,
    AUDIT_FORMAT_CORRECTION_PROMPT,
    AUDIT_STANDARD_BATCH_PROMPT,
    AUDIT_SUMMARY_PROMPT,
    AUDIT_TASK_AUTHORIZATION_PROMPT,
)
from app.core.provider_usage import ProviderUsageSource, record_provider_response_usage
from app.core.terminal.manager import terminal_session_manager
from app.core.tools import get_tool_parameters_schema
from app.core.tools.file_tool import resolve_file_tool_target_path
from app.core.tools.read_text_file import read_text_file
from app.core.tools.shell import ShellExecutor
from app.core.utils.background_task_result import serialize_execution_summary
from app.core.utils.dispatcher.helpers import resolve_chat_params
from app.core.utils.dispatcher.process_single_tool import _validate_schema_value
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.operation_directories import get_allowed_operation_dirs, get_write_protected_dirs, is_path_write_protected, normalize_allowed_operation_dirs
from app.models.audit import AuditToolConclusion
from app.models.channel import ModelUsage, resolve_model_protocol
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig
from app.models.provider_usage import ProviderUsageType
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


@dataclass(slots=True)
class AuditReadBudget:
    limit: int
    calls: int = 0
    shell_calls: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.calls)

    def consume(self, *, shell: bool = False) -> bool:
        if self.remaining <= 0:
            return False
        self.calls += 1
        if shell:
            self.shell_calls += 1
        return True


@dataclass(slots=True)
class AuditEvaluation:
    results: dict[str, dict[str, Any]]
    conclusions: dict[str, AuditToolConclusion]
    file_snapshots: dict[str, list[dict[str, Any]]]
    confirmation_reasons: dict[str, list[dict[str, Any]]]
    request_context: dict[str, Any]
    response_context: dict[str, Any]
    terminal_contexts: dict[str, dict[str, Any]]


def is_audit_configured(cfg: ProfileConfig) -> bool:
    channel_id = cfg.security.audit_channel_id
    model_id = cfg.security.audit_model_id
    return isinstance(channel_id, int) and not isinstance(channel_id, bool) and channel_id > 0 and isinstance(model_id, str) and bool(model_id.strip())


def _tool_payload(tool_calls: list[InternalToolCall]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in tool_calls:
        payload.append({"id": item.id, "name": item.name, "arguments": dict(item.arguments or {})})
    return payload


def _round_conflict_ids(
    tool_calls: list[InternalToolCall],
    working_directory: Path,
    allowed_operation_dirs: list[str],
) -> set[str]:
    writers: dict[str, list[str]] = {}
    for tool_call in tool_calls:
        arguments = tool_call.arguments or {}
        if tool_call.name != FILE_TOOL_NAME or arguments.get("operation") not in {"write", "edit", "patch"}:
            continue
        path = str(arguments.get("path", ""))
        try:
            normalized = str(resolve_file_tool_target_path(path, working_directory, allowed_operation_dirs))
        except (TypeError, ValueError):
            continue
        writers.setdefault(normalized, []).append(tool_call.id)
    conflicts: set[str] = set()
    for call_ids in writers.values():
        if len(call_ids) > 1:
            conflicts.update(call_ids)
    return conflicts


def _collect_local_block_reasons(
    tool_calls: list[InternalToolCall],
    working_directory: Path,
    allowed_operation_dirs: list[str],
    *,
    protect_system_directories: bool = True,
) -> dict[str, str]:
    conflict_ids = _round_conflict_ids(tool_calls, working_directory, allowed_operation_dirs)
    if conflict_ids:
        return {tool_call.id: t(ERR_AUDIT_ROUND_FILE_CONFLICT) for tool_call in tool_calls if tool_call.id in conflict_ids}

    for tool_call in tool_calls:
        if tool_call.name != SHELL_TOOL_NAME:
            continue
        blacklisted = ShellExecutor.check_blacklist(str((tool_call.arguments or {}).get("command", "")))
        if blacklisted:
            return {tool_call.id: t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted)}

    reasons: dict[str, str] = {}
    for tool_call in tool_calls:
        arguments = tool_call.arguments or {}
        if tool_call.name != FILE_TOOL_NAME or arguments.get("operation") not in {"write", "edit", "patch"}:
            continue
        original_path = arguments.get("path")
        try:
            target_path = resolve_file_tool_target_path(original_path, working_directory, allowed_operation_dirs)
        except (TypeError, ValueError):
            continue
        if protect_system_directories and is_path_write_protected(working_directory / Path(original_path), os.getcwd()):
            reasons[tool_call.id] = t(ERR_TOOL_WRITE_PROTECTED_DIRECTORY, path=str(target_path))
    return reasons


def _collect_file_mutation_snapshots(
    tool_calls: list[InternalToolCall],
    working_directory: Path,
    allowed_operation_dirs: list[str],
) -> dict[str, list[dict[str, Any]]]:
    database_snapshots: dict[str, list[dict[str, Any]]] = {}
    for tool_call in tool_calls:
        arguments = tool_call.arguments or {}
        if tool_call.name != FILE_TOOL_NAME or arguments.get("operation") not in {"edit", "patch"}:
            continue
        original_path = arguments.get("path")
        try:
            resolve_file_tool_target_path(original_path, working_directory, allowed_operation_dirs)
        except (TypeError, ValueError):
            continue
        try:
            snapshot = create_file_integrity_snapshot(str(original_path), working_directory=working_directory)
            snapshot_data = snapshot.to_dict()
            readable = snapshot.exists and snapshot.size is not None and snapshot.sha256 is not None
            snapshot_data.update(
                status="ok" if readable else ("missing" if not snapshot.exists else "unreadable"),
                truncated=False,
                error=None if readable or not snapshot.exists else "path is not a regular file",
            )
        except Exception as exc:
            source = Path(str(original_path))
            if not source.is_absolute():
                source = working_directory / source
            absolute_path = Path(os.path.abspath(source))
            try:
                resolved_path = absolute_path.resolve(strict=False)
            except (OSError, RuntimeError):
                resolved_path = absolute_path
            snapshot_data = {
                "original_path": str(original_path),
                "absolute_path": str(absolute_path),
                "resolved_path": str(resolved_path),
                "exists": None,
                "file_type": "unknown",
                "size": None,
                "sha256": None,
                "status": "unreadable",
                "truncated": False,
                "error": str(exc),
            }
        database_snapshots.setdefault(tool_call.id, []).append(snapshot_data)
    return database_snapshots


def _read_for_audit_sync(
    path: str,
    original_tool_call_id: str,
    expected_tool_call_ids: set[str],
    working_directory: Path,
    max_tokens: int,
) -> dict[str, Any]:
    if original_tool_call_id not in expected_tool_call_ids:
        return {"path": path, "tool_call_id": original_tool_call_id, "status": "invalid", "error": "tool_call_id does not belong to this audit round"}
    # 审计 LLM 必须拥有系统内最高且高于其他 LLM 的文件读取权限，否则可能因证据不可达而无法正常审计。
    result = read_text_file(path, working_directory=working_directory, max_tokens=max_tokens)
    return {"path": path, "tool_call_id": original_tool_call_id, **result.to_dict()}


async def _execute_audit_read_tool_call(
    tool_call: InternalToolCall,
    *,
    expected_tool_call_ids: set[str],
    working_directory: Path,
    read_budget: AuditReadBudget,
    max_tokens: int,
) -> dict[str, Any]:
    arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
    if not read_budget.consume():
        return {
            "path": arguments.get("path"),
            "tool_call_id": arguments.get("tool_call_id"),
            "status": "limit_exceeded",
            "error": t(ERR_AUDIT_FILE_READ_LIMIT_EXCEEDED),
            **_remaining_evidence_call_fields(read_budget),
        }
    parameters_schema = get_tool_parameters_schema(
        tool_call.name,
        tool_schemas=get_audit_tool_schemas(),
        tool_workflow=TOOL_WORKFLOW_AUDIT,
    )
    if tool_call.name != READ_TEXT_FILE_TOOL_NAME or parameters_schema is None:
        return {"status": "denied", "error": f"only {READ_TEXT_FILE_TOOL_NAME} is available", **_remaining_evidence_call_fields(read_budget)}
    schema_errors = _validate_schema_value(arguments, parameters_schema, "arguments")
    if schema_errors:
        return {"status": "invalid", "error": "; ".join(schema_errors), **_remaining_evidence_call_fields(read_budget)}
    requested_path = arguments.get("path")
    original_tool_call_id = arguments.get("tool_call_id")
    if not isinstance(requested_path, str) or not requested_path or not isinstance(original_tool_call_id, str) or not original_tool_call_id:
        return {"status": "invalid", "error": "path and tool_call_id are required", **_remaining_evidence_call_fields(read_budget)}
    arguments_summary = serialize_execution_summary(tool_call.arguments)
    logger.bind(audit_llm_tool_call=True).info(t("LOG_AUDIT_LLM_TOOL_CALL", tool_name=tool_call.name, args=arguments_summary))
    result = await asyncio.to_thread(
        _read_for_audit_sync,
        requested_path,
        original_tool_call_id,
        expected_tool_call_ids,
        working_directory,
        max_tokens,
    )
    result.update(_remaining_evidence_call_fields(read_budget))
    return result


def _remaining_evidence_call_fields(read_budget: AuditReadBudget) -> dict[str, int]:
    remaining = read_budget.remaining
    return {
        "remaining_file_reads": remaining,
        "remaining_evidence_calls": remaining,
        "remaining_shell_calls": remaining,
    }


async def _execute_audit_shell_tool_call(
    tool_call: InternalToolCall,
    *,
    expected_tool_call_ids: set[str],
    working_directory: Path,
    read_budget: AuditReadBudget,
    cfg: ProfileConfig,
) -> dict[str, Any]:
    consumed = read_budget.consume(shell=True)
    arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
    command = arguments.get("command")
    original_tool_call_id = arguments.get("tool_call_id")

    def evidence_result(status: str, content: str) -> dict[str, Any]:
        content_size = len(content.encode("utf-8"))
        return {
            "tool_name": tool_call.name,
            "tool_call_id": original_tool_call_id,
            "command": command,
            "status": status,
            "content": content,
            "size": content_size,
            "bytes_read": content_size,
            "truncated": False,
            **_remaining_evidence_call_fields(read_budget),
        }

    if not consumed:
        return evidence_result("limit_exceeded", json.dumps({"error": t(ERR_AUDIT_FILE_READ_LIMIT_EXCEEDED)}, ensure_ascii=False))

    parameters_schema = get_tool_parameters_schema(
        tool_call.name,
        tool_schemas=get_audit_tool_schemas(),
        tool_workflow=TOOL_WORKFLOW_AUDIT,
    )
    if tool_call.name != SHELL_TOOL_NAME or parameters_schema is None:
        return evidence_result("denied", json.dumps({"error": f"only {SHELL_TOOL_NAME} is available"}, ensure_ascii=False))
    schema_errors = _validate_schema_value(arguments, parameters_schema, "arguments")
    if schema_errors:
        return evidence_result("invalid", json.dumps({"error": "; ".join(schema_errors)}, ensure_ascii=False))

    allowed_arguments = {"command", "execution_mode", "tool_call_id"}
    if set(arguments) != allowed_arguments:
        error = "arguments must contain only command, execution_mode, and tool_call_id"
    elif not isinstance(command, str) or not command.strip():
        error = "command must be a non-empty string"
    elif not isinstance(original_tool_call_id, str) or original_tool_call_id not in expected_tool_call_ids:
        error = "tool_call_id does not belong to this audit round"
    else:
        error = None
    if error is not None:
        return evidence_result("invalid", json.dumps({"error": error}, ensure_ascii=False))

    arguments_summary = serialize_execution_summary(tool_call.arguments)
    logger.bind(audit_llm_tool_call=True).info(t("LOG_AUDIT_LLM_TOOL_CALL", tool_name=tool_call.name, args=arguments_summary))
    try:
        executor = await asyncio.to_thread(
            ShellExecutor,
            project_root=os.getcwd(),
            working_directory=working_directory,
        )
        executor.set_config(cfg)
        content = await executor.execute(command, execution_mode=arguments["execution_mode"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return evidence_result("failed", json.dumps({"error": str(exc)}, ensure_ascii=False))

    try:
        execution_result = json.loads(content)
    except (TypeError, ValueError):
        status = "failed"
    else:
        status = "ok" if isinstance(execution_result, dict) and execution_result.get("exit_code") == 0 and "error" not in execution_result else "failed"
    return evidence_result(status, content)


def _file_snapshots_from_reads(file_reads: list[dict[str, Any]], expected_tool_call_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    snapshots: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[tuple[Any, ...]]] = {}
    for file_read in file_reads:
        if not isinstance(file_read, dict):
            continue
        tool_call_id = file_read.get("tool_call_id")
        absolute_path = file_read.get("absolute_path")
        resolved_path = file_read.get("resolved_path")
        exists = file_read.get("exists")
        file_type = file_read.get("file_type")
        if tool_call_id not in expected_tool_call_ids or not isinstance(absolute_path, str) or not absolute_path or not isinstance(resolved_path, str) or not resolved_path or not isinstance(exists, bool) or not isinstance(file_type, str):
            continue
        if exists:
            size = file_read.get("size")
            sha256 = file_read.get("sha256")
            if file_type not in {"regular_file", "symlink"} or not isinstance(size, int) or isinstance(size, bool) or not isinstance(sha256, str) or not sha256:
                continue
        elif file_type != "missing" or file_read.get("size") is not None or file_read.get("sha256") is not None:
            continue
        snapshot = dict(file_read)
        identity = tuple(snapshot.get(key) for key in ("absolute_path", "resolved_path", "size", "sha256", "truncated"))
        if identity in seen.setdefault(tool_call_id, set()):
            continue
        seen[tool_call_id].add(identity)
        snapshots.setdefault(tool_call_id, []).append(snapshot)
    return snapshots


def _path_aliases(value: dict[str, Any]) -> set[str]:
    return {str(value[key]) for key in ("path", "original_path", "absolute_path", "resolved_path") if value.get(key)}


def _file_checks_are_sufficient(
    snapshots: list[dict[str, Any]],
    file_reads: list[dict[str, Any]],
    model_file_checks: Any,
) -> bool:
    if not isinstance(model_file_checks, list) or len(model_file_checks) != len(snapshots) or any(not isinstance(item, dict) for item in model_file_checks):
        return False
    remaining_checks = list(model_file_checks)
    for snapshot in snapshots:
        if snapshot.get("status") != "ok":
            return False
        matching_checks = [check for check in remaining_checks if _path_aliases(snapshot) & _path_aliases(check)]
        if len(matching_checks) != 1:
            return False
        check = matching_checks[0]
        remaining_checks.remove(check)
        if check.get("status") != "ok":
            return False
        matching_reads = [
            read for read in file_reads if _path_aliases(snapshot) & _path_aliases(read) and read.get("status") == "ok" and isinstance(read.get("content"), str) and all(read.get(field) == snapshot.get(field) for field in ("original_path", "absolute_path", "resolved_path", "size", "sha256", "truncated", "bytes_read"))
        ]
        if not matching_reads:
            return False
        server_result = matching_reads[0]
        for field in ("original_path", "absolute_path", "resolved_path", "exists", "file_type", "status", "size", "sha256", "truncated", "bytes_read"):
            if field not in check or check[field] != server_result.get(field, snapshot.get(field)):
                return False
    return not remaining_checks


def _extract_json_object(content: str | None) -> dict[str, Any]:
    text = (content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(t(ERR_AUDIT_RESPONSE_JSON_INVALID))
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError(t(ERR_AUDIT_RESPONSE_JSON_INVALID))
    return value


def _parse_results(
    payload: dict[str, Any],
    expected_ids: list[str],
) -> list[dict[str, Any]]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list) or len(raw_results) != len(expected_ids):
        raise ValueError(t(ERR_AUDIT_RESULT_COUNT_MISMATCH))
    parsed: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            raise ValueError(t(ERR_AUDIT_RESULT_INVALID))
        call_id = item.get("tool_call_id")
        score = item.get("score")
        reason = item.get("reason")
        file_checks = item.get("file_checks")
        deny = item.get("deny")
        if not isinstance(deny, bool):
            raise ValueError(t(ERR_AUDIT_RESULT_INVALID))
        if call_id not in expected_ids or call_id in parsed:
            raise ValueError(t(ERR_AUDIT_RESULT_MAPPING_MISMATCH))
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 10:
            raise ValueError(t(ERR_AUDIT_SCORE_INVALID))
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(t(ERR_AUDIT_REASON_INVALID))
        if not isinstance(file_checks, list):
            raise ValueError(t(ERR_AUDIT_FILE_CHECKS_INVALID))
        parsed[call_id] = {
            "tool_call_id": call_id,
            "score": score,
            "reason": reason.strip(),
            "deny": deny,
            "file_checks": file_checks,
        }
    if set(parsed) != set(expected_ids):
        raise ValueError(t(ERR_AUDIT_RESULT_MAPPING_MISMATCH))
    return [parsed[call_id] for call_id in expected_ids]


def _requires_confirmation_from_evidence(
    _tool_call: InternalToolCall,
    snapshots: list[dict[str, Any]],
    file_reads: list[dict[str, Any]],
    model_file_checks: list[Any],
) -> bool:
    if not file_reads:
        return bool(snapshots) or bool(model_file_checks)
    if any(item.get("status") != "ok" or not isinstance(item.get("content"), str) for item in file_reads):
        return True
    if not snapshots:
        return True
    return not _file_checks_are_sufficient(snapshots, file_reads, model_file_checks)


def _evidence_confirmation_reason(
    tool_call: InternalToolCall,
    snapshots: list[dict[str, Any]],
    file_reads: list[dict[str, Any]],
    model_file_checks: Any,
) -> dict[str, Any] | None:
    if not _requires_confirmation_from_evidence(tool_call, snapshots, file_reads, model_file_checks):
        return None
    return {
        "code": "file_evidence_insufficient",
        "message": t(ERR_AUDIT_FILE_EVIDENCE_INSUFFICIENT),
        "details": {
            "snapshot_count": len(snapshots),
            "model_file_check_count": len(model_file_checks) if isinstance(model_file_checks, list) else None,
            "server_file_read_count": len(file_reads),
        },
    }


def classify_audit_score(score: int, threshold: int) -> AuditToolConclusion:
    if score >= AUDIT_HIGH_RISK_SCORE:
        return AuditToolConclusion.PENDING
    if threshold > 0 and score >= threshold:
        return AuditToolConclusion.PENDING
    return AuditToolConclusion.PASSED


async def _run_audit_model(
    db: AsyncSession,
    cfg: ProfileConfig,
    system_prompt: str,
    payload: dict[str, Any],
    working_directory: Path,
    expected_tool_call_ids: set[str],
    read_budget: AuditReadBudget,
    *,
    result_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    channel_id = cfg.security.audit_channel_id
    model_id = cfg.security.audit_model_id
    if not is_audit_configured(cfg):
        raise RuntimeError(t(ERR_AUDIT_CONFIG_MISSING))
    channel = await channel_crud.get(db, channel_id)
    if channel is None or not channel.is_active:
        raise RuntimeError(t(ERR_AUDIT_CHANNEL_UNAVAILABLE))
    model_entry = get_model_entry(channel, model_id, ModelUsage.CHAT.value)
    if model_entry is None:
        raise RuntimeError(t(ERR_AUDIT_CHANNEL_UNAVAILABLE))
    try:
        protocol = resolve_model_protocol(model_entry)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(t(ERR_AUDIT_CHANNEL_UNAVAILABLE)) from exc
    chat_params = resolve_chat_params(model_entry, cfg.channel.chat_channel)
    allowed_operation_dirs = await asyncio.to_thread(
        normalize_allowed_operation_dirs,
        [*get_allowed_operation_dirs(cfg), str(working_directory)],
    )
    directory_policy = {"allowed_operation_dirs": [str(directory) for directory in dict.fromkeys(allowed_operation_dirs)]}
    if cfg.security.protect_system_directories:
        directory_policy["write_protected_directories"] = [str(directory) for directory in await asyncio.to_thread(get_write_protected_dirs)]
    model_payload = {
        **payload,
        "audit_depth": read_budget.limit,
        "remaining_file_reads": read_budget.remaining,
        "remaining_evidence_calls": read_budget.remaining,
        "remaining_shell_calls": read_budget.remaining,
        "directory_policy": directory_policy,
    }
    audit_payload = fit_file_tool_payload_to_context(
        system_prompt,
        model_payload,
        chat_params,
        model_id=model_id,
        protocol=protocol,
    )
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content=system_prompt),
        InternalMessage(role=MessageRole.USER, content=json.dumps(audit_payload, ensure_ascii=False)),
    ]
    read_results: list[dict[str, Any]] = []
    shell_results: list[dict[str, Any]] = []
    max_rounds = read_budget.remaining + (2 if result_ids is not None else 1)
    format_correction_used = False
    if result_ids is None:
        usage_type = ProviderUsageType.AUDIT_AUXILIARY
    elif payload.get("task_instruction") is not None:
        usage_type = ProviderUsageType.TASK_AUTHORIZATION
    else:
        usage_type = ProviderUsageType.SAFETY_AUDIT
    for _round_index in range(max_rounds):
        await ensure_session_reply_work_claim(db)
        await db.commit()
        active_tools = get_audit_tool_schemas() if read_budget.remaining > 0 else None
        request_context_tokens = LLMClient.estimate_request_input_tokens_locally(
            model_id=model_id,
            messages=messages,
            tools=active_tools,
            protocol=protocol,
        )
        response = await LLMClient.generate(
            api_key=channel.get_decrypted_api_key(),
            base_url=channel.base_url,
            model_id=model_id,
            messages=messages,
            temperature=chat_params["temperature"],
            top_p=chat_params["top_p"],
            reasoning_effort=chat_params.get("reasoning_effort"),
            max_tokens=chat_params["max_tokens"],
            timeout=chat_params["chat_timeout"],
            protocol=protocol,
            tools=active_tools,
            request_context_tokens=request_context_tokens,
            http_proxy=get_channel_http_proxy(channel),
            custom_headers=get_model_custom_headers(model_entry),
        )
        await record_provider_response_usage(
            response,
            usage_type=usage_type,
            source=ProviderUsageSource.from_channel(channel, model_id=model_id, protocol=protocol),
        )
        if response.message.tool_calls:
            if read_budget.remaining <= 0:
                raise RuntimeError(t(ERR_AUDIT_FILE_ROUNDS_EXCEEDED))
            messages.append(response.message)
            for tool_call in response.message.tool_calls:
                if tool_call.name == SHELL_TOOL_NAME:
                    result = await _execute_audit_shell_tool_call(
                        tool_call,
                        expected_tool_call_ids=expected_tool_call_ids,
                        working_directory=working_directory,
                        read_budget=read_budget,
                        cfg=cfg,
                    )
                    result_collection = shell_results
                else:
                    read_max_tokens, _ = read_token_budget(
                        messages,
                        chat_params,
                        model_id=model_id,
                        protocol=protocol,
                    )
                    result = await _execute_audit_read_tool_call(
                        tool_call,
                        expected_tool_call_ids=expected_tool_call_ids,
                        working_directory=working_directory,
                        read_budget=read_budget,
                        max_tokens=read_max_tokens,
                    )
                    result_collection = read_results
                result = fit_read_result_to_context(
                    messages,
                    tool_call.id,
                    result,
                    chat_params,
                    model_id=model_id,
                    protocol=protocol,
                )
                result.update(_remaining_evidence_call_fields(read_budget))
                result_collection.append(result)
                messages.append(build_read_tool_message(tool_call.id, result))
            continue
        if result_ids is None and not (response.message.content or "").strip():
            raise RuntimeError(t(ERR_AUDIT_FILE_ROUNDS_EXCEEDED))
        if result_ids is not None:
            try:
                _parse_results(_extract_json_object(response.message.content), result_ids)
            except ValueError as exc:
                if format_correction_used:
                    raise
                format_correction_used = True
                messages.append(response.message)
                correction_payload = {
                    "instruction": AUDIT_FORMAT_CORRECTION_PROMPT,
                    "validation_error": str(exc),
                    "expected_tool_call_ids": result_ids,
                    **_remaining_evidence_call_fields(read_budget),
                }
                messages.append(
                    InternalMessage(
                        role=MessageRole.USER,
                        content=json.dumps(correction_payload, ensure_ascii=False),
                    )
                )
                continue
        return (
            {"messages": [item.model_dump(mode="json", exclude_none=True) for item in messages]},
            {
                "content": response.message.content,
                "file_reads": read_results,
                "shell_results": shell_results,
            },
        )
    raise RuntimeError(t(ERR_AUDIT_FILE_ROUNDS_EXCEEDED))


async def _call_auditor(
    db: AsyncSession,
    cfg: ProfileConfig,
    request_payload: dict[str, Any],
    working_directory: Path,
    *,
    read_budget: AuditReadBudget | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if read_budget is None:
        read_budget = AuditReadBudget(limit=cfg.security.audit_depth)
    expected_tool_call_ids = {item.get("tool_call_id") for item in request_payload.get("tool_calls", []) if isinstance(item, dict) and isinstance(item.get("tool_call_id"), str)}
    result_ids = [item["tool_call_id"] for item in request_payload.get("tool_calls", []) if isinstance(item, dict) and isinstance(item.get("tool_call_id"), str)]
    system_prompt = AUDIT_BATCH_PROMPT if cfg.security.protect_system_directories else AUDIT_STANDARD_BATCH_PROMPT
    if request_payload.get("task_instruction") is not None:
        system_prompt += "\n\n" + AUDIT_TASK_AUTHORIZATION_PROMPT
    request_context, response_context = await _run_audit_model(
        db,
        cfg,
        system_prompt,
        request_payload,
        working_directory,
        expected_tool_call_ids,
        read_budget,
        result_ids=result_ids,
    )
    response_context["parsed"] = _extract_json_object(response_context.get("content"))
    return request_context, response_context


async def _summarize_pending(
    db: AsyncSession,
    cfg: ProfileConfig,
    tool_calls: list[dict[str, Any]],
    server_confirmation_reasons: dict[str, list[dict[str, Any]]] | None = None,
    working_directory: str | Path | None = None,
    *,
    read_budget: AuditReadBudget | None = None,
    audit_results: list[dict[str, Any]] | None = None,
    file_reads: list[dict[str, Any]] | None = None,
    shell_results: list[dict[str, Any]] | None = None,
    task_instruction: str | None = None,
) -> tuple[str, dict[str, Any]]:
    fallback_summary = t(MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK, locale=cfg.security.audit_report_language)
    workdir = Path(working_directory or os.getcwd()).resolve(strict=False)
    summary_payload = {
        "working_directory": str(workdir),
        "tool_calls": tool_calls,
        "server_confirmation_reasons": server_confirmation_reasons or {},
        "audit_results": audit_results or [],
        "file_reads": file_reads or [],
        "shell_results": shell_results or [],
    }
    if task_instruction is not None:
        summary_payload["task_instruction"] = task_instruction
    summary_prompt = AUDIT_SUMMARY_PROMPT.format(audit_report_language=cfg.security.audit_report_language)
    expected_tool_call_ids = {item.get("id") for item in tool_calls if isinstance(item, dict) and isinstance(item.get("id"), str)}
    if read_budget is None:
        read_budget = AuditReadBudget(limit=cfg.security.audit_depth)
    try:
        request_context, response_context = await _run_audit_model(
            db,
            cfg,
            summary_prompt,
            summary_payload,
            workdir,
            expected_tool_call_ids,
            read_budget,
        )
        summary = (response_context.get("content") or "").strip()
        if summary:
            return summary[:500], {
                "request": request_context["messages"],
                "response": summary,
                "file_reads": response_context["file_reads"],
                "shell_results": response_context.get("shell_results", []),
            }
    except Exception as exc:
        return fallback_summary, {"fallback": True, "error": str(exc)}
    return fallback_summary, {"fallback": True}


async def evaluate_tool_calls(
    db: AsyncSession,
    cfg: ProfileConfig,
    tool_calls: list[InternalToolCall],
    working_directory: Path,
    *,
    uid: str,
    session_id: str,
    read_budget: AuditReadBudget,
    turn_indexes: dict[str, int] | None = None,
    task_instruction: str | None = None,
    is_scheduled_task: bool = False,
) -> AuditEvaluation:
    if not tool_calls:
        return AuditEvaluation(
            results={},
            conclusions={},
            file_snapshots={},
            confirmation_reasons={},
            request_context={"messages": []},
            response_context={},
            terminal_contexts={},
        )

    allowed_operation_dirs = get_allowed_operation_dirs(cfg)
    file_mutation_snapshots = await asyncio.to_thread(
        _collect_file_mutation_snapshots,
        tool_calls,
        working_directory,
        allowed_operation_dirs,
    )
    audited_tool_call_ids: list[str] = []
    request_tool_calls: list[dict[str, Any]] = []
    for turn_index, tool_call in enumerate(tool_calls):
        original_index = turn_index
        if turn_indexes is not None:
            original_index = turn_indexes[tool_call.id]
        audited_tool_call_ids.append(tool_call.id)
        request_tool_calls.append(
            {
                "tool_call_id": tool_call.id,
                "turn_index": original_index,
                "tool_name": tool_call.name,
                "arguments": tool_call.arguments,
            }
        )
    request_payload: dict[str, Any] = {
        "confirmation_threshold": cfg.security.audit_threshold,
        "working_directory": str(working_directory),
        "tool_calls": request_tool_calls,
    }
    if task_instruction is not None:
        request_payload["task_instruction"] = task_instruction

    request_context: dict[str, Any] = {"messages": []}
    response_context: dict[str, Any] = {}
    terminal_contexts: dict[str, dict[str, Any]] = {}
    confirmation_reasons: dict[str, list[dict[str, Any]]] = {}
    read_file_snapshots: dict[str, list[dict[str, Any]]] = {}
    file_reads: list[dict[str, Any]] = []
    shell_results: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    conclusions: dict[str, AuditToolConclusion] = {}
    local_block_reasons = await asyncio.to_thread(
        _collect_local_block_reasons,
        tool_calls,
        working_directory,
        allowed_operation_dirs,
        protect_system_directories=cfg.security.protect_system_directories,
    )
    if local_block_reasons:
        response_context = {"local_block": "; ".join(local_block_reasons.values())}
        for tool_call in tool_calls:
            is_blocked = tool_call.id in local_block_reasons
            result = {
                "tool_call_id": tool_call.id,
                "score": 10 if is_blocked else 0,
                "reason": local_block_reasons.get(tool_call.id, t(MSG_AUDIT_ROUND_SKIPPED)),
                "file_checks": [],
                "deny": is_blocked,
            }
            results[tool_call.id] = result
            if is_blocked:
                conclusions[tool_call.id] = AuditToolConclusion.BLOCKED
            else:
                conclusions[tool_call.id] = AuditToolConclusion.PASSED
    else:
        for tool_call_payload in request_tool_calls:
            arguments = tool_call_payload.get("arguments") or {}
            is_terminal_write = tool_call_payload["tool_name"] == TERMINAL_TOOL_NAME and arguments.get("operation") == "write"
            if not is_terminal_write:
                continue
            terminal_session_id = arguments.get("terminal_session_id")
            if terminal_session_id not in terminal_contexts:
                terminal_contexts[terminal_session_id] = await terminal_session_manager.get_audit_context(
                    db,
                    terminal_session_id,
                    uid,
                    session_id,
                )
            tool_call_payload["terminal_context"] = terminal_contexts[terminal_session_id]
        request_context, response_context = await _call_auditor(
            db,
            cfg,
            request_payload,
            working_directory,
            read_budget=read_budget,
        )
        parsed_results = _parse_results(
            response_context["parsed"],
            audited_tool_call_ids,
        )
        parsed_results_by_id: dict[str, dict[str, Any]] = {}
        for result in parsed_results:
            parsed_results_by_id[result["tool_call_id"]] = result
        response_file_reads = response_context.get("file_reads", [])
        if isinstance(response_file_reads, list):
            file_reads = response_file_reads
        response_shell_results = response_context.get("shell_results", [])
        if isinstance(response_shell_results, list):
            shell_results = response_shell_results
        expected_tool_call_ids = set(audited_tool_call_ids)
        read_file_snapshots = _file_snapshots_from_reads(file_reads, expected_tool_call_ids)
        evidence_protocol_failures: list[Any] = []
        for evidence_result in file_reads:
            if not isinstance(evidence_result, dict) or evidence_result.get("status") in {"denied", "invalid"} or evidence_result.get("tool_call_id") not in expected_tool_call_ids:
                evidence_protocol_failures.append(evidence_result)
        for evidence_result in shell_results:
            if not isinstance(evidence_result, dict) or evidence_result.get("status") in {"denied", "invalid"} or evidence_result.get("tool_call_id") not in expected_tool_call_ids:
                evidence_protocol_failures.append(evidence_result)
        for tool_call in tool_calls:
            result = parsed_results_by_id[tool_call.id]
            tool_file_reads: list[dict[str, Any]] = []
            for file_read in file_reads:
                if isinstance(file_read, dict) and file_read.get("tool_call_id") == tool_call.id:
                    tool_file_reads.append(file_read)
            local_reasons = list(confirmation_reasons.get(tool_call.id, []))
            evidence_requires_confirmation = _requires_confirmation_from_evidence(
                tool_call,
                read_file_snapshots.get(tool_call.id, []),
                tool_file_reads,
                result.get("file_checks", []),
            )
            requires_confirmation = bool(evidence_protocol_failures) or evidence_requires_confirmation
            evidence_reason = _evidence_confirmation_reason(
                tool_call,
                read_file_snapshots.get(tool_call.id, []),
                tool_file_reads,
                result.get("file_checks", []),
            )
            if evidence_reason is not None:
                local_reasons.append(evidence_reason)
            if evidence_protocol_failures:
                local_reasons.append(
                    {
                        "code": "file_read_protocol_invalid",
                        "message": t(ERR_AUDIT_FILE_EVIDENCE_INSUFFICIENT),
                        "details": {"failure_count": len(evidence_protocol_failures)},
                    }
                )
            if local_reasons:
                confirmation_reasons[tool_call.id] = local_reasons
                result["reason"] = f"{result['reason']}; {'; '.join(item['message'] for item in local_reasons)}"
            if result.get("deny", False):
                conclusion = AuditToolConclusion.BLOCKED
            elif cfg.security.audit_threshold > 0 and (local_reasons or requires_confirmation):
                conclusion = AuditToolConclusion.PENDING
            else:
                conclusion = classify_audit_score(result["score"], cfg.security.audit_threshold)
            if is_scheduled_task and conclusion == AuditToolConclusion.PENDING:
                conclusion = AuditToolConclusion.BLOCKED
            results[tool_call.id] = result
            conclusions[tool_call.id] = conclusion

    file_snapshots: dict[str, list[dict[str, Any]]] = {}
    for tool_call in tool_calls:
        snapshots: list[dict[str, Any]] = []
        for snapshot in file_mutation_snapshots.get(tool_call.id, []):
            snapshots.append(snapshot)
        for snapshot in read_file_snapshots.get(tool_call.id, []):
            snapshots.append(snapshot)
        file_snapshots[tool_call.id] = snapshots
    return AuditEvaluation(
        results=results,
        conclusions=conclusions,
        file_snapshots=file_snapshots,
        confirmation_reasons=confirmation_reasons,
        request_context=request_context,
        response_context=response_context,
        terminal_contexts=terminal_contexts,
    )


async def summarize_evaluation(
    db: AsyncSession,
    cfg: ProfileConfig,
    evaluation: AuditEvaluation,
    tool_calls: list[InternalToolCall],
    working_directory: Path,
    *,
    read_budget: AuditReadBudget,
    task_instruction: str | None = None,
) -> tuple[str, dict[str, Any]]:
    summary_tool_calls = _tool_payload(tool_calls)
    for tool_call_payload in summary_tool_calls:
        arguments = tool_call_payload.get("arguments") or {}
        is_terminal_write = tool_call_payload["name"] == TERMINAL_TOOL_NAME and arguments.get("operation") == "write"
        if not is_terminal_write:
            continue
        terminal_session_id = arguments.get("terminal_session_id")
        tool_call_payload["terminal_context"] = evaluation.terminal_contexts[terminal_session_id]

    audit_results: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        result = evaluation.results.get(tool_call.id)
        if result is not None:
            audit_results.append(result)
    file_reads = evaluation.response_context.get("file_reads", [])
    shell_results = evaluation.response_context.get("shell_results", [])
    return await _summarize_pending(
        db,
        cfg,
        summary_tool_calls,
        evaluation.confirmation_reasons,
        working_directory=working_directory,
        read_budget=read_budget,
        audit_results=audit_results,
        file_reads=file_reads,
        shell_results=shell_results,
        task_instruction=task_instruction,
    )
