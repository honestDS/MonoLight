import asyncio
import copy
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import cancel_confirmation_by_session
from app.core.audit.integrity import build_tool_round_integrity_snapshot, create_file_integrity_snapshot, serialize_tool_arguments
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.channel_router import get_model_entry
from app.core.constants import (
    AUDIT_HIGH_RISK_SCORE,
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_AUDIT_CHANNEL_UNAVAILABLE,
    ERR_AUDIT_CONFIG_MISSING,
    ERR_AUDIT_FILE_CHECKS_INVALID,
    ERR_AUDIT_FILE_EVIDENCE_INSUFFICIENT,
    ERR_AUDIT_FILE_ROUNDS_EXCEEDED,
    ERR_AUDIT_PERSISTENCE_FAILED_STOP,
    ERR_AUDIT_REASON_INVALID,
    ERR_AUDIT_RESPONSE_JSON_INVALID,
    ERR_AUDIT_RESULT_COUNT_MISMATCH,
    ERR_AUDIT_RESULT_INVALID,
    ERR_AUDIT_RESULT_MAPPING_MISMATCH,
    ERR_AUDIT_ROUND_BLOCKED,
    ERR_AUDIT_ROUND_FILE_CONFLICT,
    ERR_AUDIT_SCORE_INVALID,
    ERR_AUDIT_SERVICE_FAILED_STOP,
    ERR_TOOL_SHELL_BLACKLISTED,
    MSG_AUDIT_CONFIRMATION_IM,
    MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK,
    MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM,
    MSG_AUDIT_ROUND_SKIPPED,
    MSG_AUDIT_WAITING_CONFIRMATION,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.channel.channel import channel_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT
from app.core.tools import tool_requires_audit
from app.core.tools.file_writer import resolve_file_writer_target_path
from app.core.tools.read_text_file import READ_TEXT_FILE_TOOL_SCHEMA, read_text_file
from app.core.tools.shell import ShellExecutor
from app.core.utils.background_task_result import serialize_execution_summary
from app.core.utils.dispatcher.helpers import resolve_chat_params
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.time import get_local_time
from app.core.utils.tokenizer import estimate_tokens, truncate_text_to_tokens
from app.models.audit import AuditFailureType, AuditRecordStatus, AuditToolConclusion
from app.models.channel import ModelUsage, resolve_model_protocol
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig
from app.providers.llm.client import LLMClient, estimate_request_context_tokens

logger = get_logger(__name__)

AUDIT_FILE_MAX_CALLS = 10
AUDIT_FILE_MAX_ROUNDS = 4
AUDIT_CONTEXT_SAFETY_RATIO_PERCENT = 10
AUDIT_CONTEXT_SAFETY_MIN_TOKENS = 256
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA = copy.deepcopy(READ_TEXT_FILE_TOOL_SCHEMA)
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA["function"]["description"] = "Read any UTF-8 text file as evidence for one tool call in this audit round."
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA["function"]["parameters"]["properties"]["tool_call_id"] = {
    "type": "string",
    "description": "The original tool_call_id whose assessment needs this file evidence.",
}
AUDIT_READ_TEXT_FILE_TOOL_SCHEMA["function"]["parameters"]["required"].append("tool_call_id")


@dataclass(frozen=True, slots=True)
class AuditRoundResult:
    audit_record_id: int
    status: AuditRecordStatus
    tool_results: tuple[InternalMessage, ...]
    confirmation_payload: dict[str, Any] | None = None

    @property
    def may_execute(self) -> bool:
        return self.status == AuditRecordStatus.PASSED


def is_audit_configured(cfg: ProfileConfig) -> bool:
    channel_id = cfg.security.audit_channel_id
    model_id = cfg.security.audit_model_id
    return isinstance(channel_id, int) and not isinstance(channel_id, bool) and channel_id > 0 and isinstance(model_id, str) and bool(model_id.strip())


def _tool_payload(tool_calls: list[InternalToolCall]) -> list[dict[str, Any]]:
    return [{"id": item.id, "name": item.name, "arguments": dict(item.arguments or {})} for item in tool_calls]


def _round_conflict_ids(tool_calls: list[InternalToolCall], working_directory: Path) -> set[str]:
    writers: dict[str, list[str]] = {}
    for tool_call in tool_calls:
        if tool_call.name != "write_file":
            continue
        path = str((tool_call.arguments or {}).get("file_path", ""))
        try:
            normalized = str(resolve_file_writer_target_path(path, working_directory))
        except (TypeError, ValueError):
            continue
        writers.setdefault(normalized, []).append(tool_call.id)
    conflicts: set[str] = set()
    for call_ids in writers.values():
        if len(call_ids) > 1:
            conflicts.update(call_ids)
    return conflicts


def _collect_append_file_snapshots(
    tool_calls: list[InternalToolCall],
    working_directory: Path,
) -> dict[str, list[dict[str, Any]]]:
    database_snapshots: dict[str, list[dict[str, Any]]] = {}
    for tool_call in tool_calls:
        if tool_call.name != "write_file" or not bool((tool_call.arguments or {}).get("append")):
            continue
        original_path = (tool_call.arguments or {}).get("file_path")
        try:
            resolve_file_writer_target_path(original_path, working_directory)
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


def _audit_max_input_tokens(chat_params: dict[str, Any]) -> int:
    try:
        context_window_k = max(1, int(chat_params["context_window_k"]))
    except (KeyError, TypeError, ValueError):
        context_window_k = 4
    try:
        max_output_tokens = max(0, int(chat_params["max_tokens"]))
    except (KeyError, TypeError, ValueError):
        max_output_tokens = 0
    context_window_tokens = context_window_k * CONTEXT_WINDOW_TOKENS_PER_K
    safety_tokens = max(
        AUDIT_CONTEXT_SAFETY_MIN_TOKENS,
        context_window_tokens * AUDIT_CONTEXT_SAFETY_RATIO_PERCENT // 100,
    )
    return max(
        0,
        context_window_tokens - max_output_tokens - safety_tokens,
    )


def _fit_audit_write_file_payload_to_context(
    system_prompt: str,
    payload: dict[str, Any],
    chat_params: dict[str, Any],
) -> dict[str, Any]:
    adapted_payload = copy.deepcopy(payload)
    max_input_tokens = _audit_max_input_tokens(chat_params)

    def request_context_tokens(candidate_payload: dict[str, Any]) -> int:
        messages = [
            InternalMessage(role=MessageRole.SYSTEM, content=system_prompt),
            InternalMessage(role=MessageRole.USER, content=json.dumps(candidate_payload, ensure_ascii=False)),
        ]
        return estimate_request_context_tokens(messages, [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA])

    if request_context_tokens(adapted_payload) <= max_input_tokens:
        return adapted_payload

    tool_calls = adapted_payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return adapted_payload
    write_contents: list[tuple[int, str, int, str]] = []
    for index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict) or tool_call.get("tool_name", tool_call.get("name")) != "write_file":
            continue
        arguments = tool_call.get("arguments")
        if isinstance(arguments, dict) and isinstance(arguments.get("content"), str):
            content = arguments["content"]
            content_bytes = content.encode("utf-8")
            write_contents.append((index, content, len(content_bytes), hashlib.sha256(content_bytes).hexdigest()))
    if not write_contents:
        return adapted_payload

    def payload_with_content_limit(content_limit: int) -> dict[str, Any]:
        candidate_payload = copy.deepcopy(adapted_payload)
        candidate_calls = candidate_payload["tool_calls"]
        for index, original_content, original_size, original_sha256 in write_contents:
            content_prefix, truncated = truncate_text_to_tokens(original_content, content_limit)
            tool_call = candidate_calls[index]
            arguments = tool_call["arguments"]
            arguments["content"] = content_prefix
            existing_evidence = tool_call.get("argument_evidence")
            argument_evidence = dict(existing_evidence) if isinstance(existing_evidence, dict) else {}
            argument_evidence["content"] = {
                "status": "ok",
                "size": original_size,
                "sha256": original_sha256,
                "truncated": truncated,
                "bytes_read": len(content_prefix.encode("utf-8")),
            }
            tool_call["argument_evidence"] = argument_evidence
        return candidate_payload

    empty_payload = payload_with_content_limit(0)
    if request_context_tokens(empty_payload) > max_input_tokens:
        return empty_payload

    low = 0
    high = max(max(estimate_tokens(content), 1) for _, content, _, _ in write_contents)
    fitted_payload = empty_payload
    while low < high:
        candidate_limit = (low + high + 1) // 2
        candidate_payload = payload_with_content_limit(candidate_limit)
        if request_context_tokens(candidate_payload) <= max_input_tokens:
            low = candidate_limit
            fitted_payload = candidate_payload
        else:
            high = candidate_limit - 1
    return fitted_payload


def _audit_read_token_budget(messages: list[InternalMessage], chat_params: dict[str, Any]) -> tuple[int, int]:
    request_context_tokens = estimate_request_context_tokens(messages, [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA])
    return max(0, _audit_max_input_tokens(chat_params) - request_context_tokens), request_context_tokens


def _audit_read_tool_message(tool_call_id: str, read_result: dict[str, Any]) -> InternalMessage:
    return InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call_id,
        content=json.dumps(read_result, ensure_ascii=False),
    )


def _fit_audit_read_result_to_context(
    messages: list[InternalMessage],
    tool_call_id: str,
    read_result: dict[str, Any],
    chat_params: dict[str, Any],
) -> dict[str, Any]:
    content = read_result.get("content")
    if read_result.get("status") != "ok" or not isinstance(content, str):
        return read_result

    max_input_tokens = _audit_max_input_tokens(chat_params)
    tools = [AUDIT_READ_TEXT_FILE_TOOL_SCHEMA]
    if (
        estimate_request_context_tokens(
            [*messages, _audit_read_tool_message(tool_call_id, read_result)],
            tools,
        )
        <= max_input_tokens
    ):
        return read_result

    low = 0
    high = max(estimate_tokens(content), 1)
    fitted_content = ""
    while low < high:
        candidate_tokens = (low + high + 1) // 2
        candidate_content, _ = truncate_text_to_tokens(content, candidate_tokens)
        candidate_result = {
            **read_result,
            "content": candidate_content,
            "bytes_read": len(candidate_content.encode("utf-8")),
            "truncated": True,
        }
        candidate_context_tokens = estimate_request_context_tokens(
            [*messages, _audit_read_tool_message(tool_call_id, candidate_result)],
            tools,
        )
        if candidate_context_tokens <= max_input_tokens:
            low = candidate_tokens
            fitted_content = candidate_content
        else:
            high = candidate_tokens - 1
    return {
        **read_result,
        "content": fitted_content,
        "bytes_read": len(fitted_content.encode("utf-8")),
        "truncated": True,
    }


def _read_for_audit_sync(
    path: str,
    original_tool_call_id: str,
    expected_tool_call_ids: set[str],
    working_directory: Path,
    read_state: dict[str, int],
    max_tokens: int,
) -> dict[str, Any]:
    read_state["calls"] += 1
    if read_state["calls"] > AUDIT_FILE_MAX_CALLS:
        return {"path": path, "tool_call_id": original_tool_call_id, "status": "limit_exceeded", "error": "audit file call limit exceeded"}
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
    read_state: dict[str, int],
    max_tokens: int,
) -> dict[str, Any]:
    if tool_call.name != READ_TEXT_FILE_TOOL_SCHEMA["function"]["name"]:
        return {"status": "denied", "error": "only read_text_file is available"}
    requested_path = tool_call.arguments.get("path")
    original_tool_call_id = tool_call.arguments.get("tool_call_id")
    if not isinstance(requested_path, str) or not requested_path or not isinstance(original_tool_call_id, str) or not original_tool_call_id:
        return {"status": "invalid", "error": "path and tool_call_id are required"}
    arguments_summary = serialize_execution_summary(tool_call.arguments)
    logger.bind(audit_llm_tool_call=True).info(t("LOG_AUDIT_LLM_TOOL_CALL", tool_name=tool_call.name, args=arguments_summary))
    return await asyncio.to_thread(
        _read_for_audit_sync,
        requested_path,
        original_tool_call_id,
        expected_tool_call_ids,
        working_directory,
        read_state,
        max_tokens,
    )


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


def _parse_results(payload: dict[str, Any], expected_ids: list[str]) -> list[dict[str, Any]]:
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


def _aggregate(conclusions: list[AuditToolConclusion]) -> AuditRecordStatus:
    if AuditToolConclusion.BLOCKED in conclusions:
        return AuditRecordStatus.BLOCKED
    if AuditToolConclusion.AUDIT_FAILED in conclusions:
        return AuditRecordStatus.AUDIT_FAILED
    if AuditToolConclusion.PENDING in conclusions:
        return AuditRecordStatus.PENDING
    return AuditRecordStatus.PASSED


def _result_message(tool_call: InternalToolCall, status: AuditRecordStatus, reason: str) -> InternalMessage:
    messages = {
        AuditRecordStatus.BLOCKED: t(ERR_AUDIT_ROUND_BLOCKED),
        AuditRecordStatus.AUDIT_FAILED: t(ERR_AUDIT_PERSISTENCE_FAILED_STOP) if reason == "audit persistence failed" else t(ERR_AUDIT_SERVICE_FAILED_STOP),
        AuditRecordStatus.PENDING: t(MSG_AUDIT_WAITING_CONFIRMATION),
    }
    return InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call.id,
        content=json.dumps({"status": status.value, "error": messages[status], "reason": reason}, ensure_ascii=False),
    )


async def _call_auditor(
    db: AsyncSession,
    cfg: ProfileConfig,
    request_payload: dict[str, Any],
    working_directory: Path,
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
    audit_payload = _fit_audit_write_file_payload_to_context(AUDIT_BATCH_PROMPT, request_payload, chat_params)
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content=AUDIT_BATCH_PROMPT),
        InternalMessage(role=MessageRole.USER, content=json.dumps(audit_payload, ensure_ascii=False)),
    ]
    expected_tool_call_ids = {item.get("tool_call_id") for item in request_payload.get("tool_calls", []) if isinstance(item, dict) and isinstance(item.get("tool_call_id"), str)}
    read_state = {"calls": 0}
    read_results: list[dict[str, Any]] = []
    for _round_index in range(AUDIT_FILE_MAX_ROUNDS):
        await db.commit()
        _, request_context_tokens = _audit_read_token_budget(messages, chat_params)
        response = await LLMClient.generate(
            api_key=channel.get_decrypted_api_key(),
            base_url=channel.base_url,
            model_id=model_id,
            messages=messages,
            temperature=chat_params["temperature"],
            top_p=chat_params["top_p"],
            max_tokens=chat_params["max_tokens"],
            timeout=chat_params["chat_timeout"],
            protocol=protocol,
            tools=[AUDIT_READ_TEXT_FILE_TOOL_SCHEMA],
            request_context_tokens=request_context_tokens,
            http_proxy=get_channel_http_proxy(channel),
            custom_headers=get_model_custom_headers(model_entry),
        )
        if not response.message.tool_calls:
            parsed = _extract_json_object(response.message.content)
            return (
                {"messages": [item.model_dump(mode="json", exclude_none=True) for item in messages]},
                {
                    "content": response.message.content,
                    "parsed": parsed,
                    "file_reads": read_results,
                },
            )
        messages.append(response.message)
        for tool_call in response.message.tool_calls:
            read_max_tokens, _ = _audit_read_token_budget(messages, chat_params)
            read_result = await _execute_audit_read_tool_call(
                tool_call,
                expected_tool_call_ids=expected_tool_call_ids,
                working_directory=working_directory,
                read_state=read_state,
                max_tokens=read_max_tokens,
            )
            read_result = _fit_audit_read_result_to_context(messages, tool_call.id, read_result, chat_params)
            read_results.append(read_result)
            messages.append(_audit_read_tool_message(tool_call.id, read_result))
    raise RuntimeError(t(ERR_AUDIT_FILE_ROUNDS_EXCEEDED))


async def _summarize_pending(
    db: AsyncSession,
    cfg: ProfileConfig,
    tool_calls: list[dict[str, Any]],
    server_confirmation_reasons: dict[str, list[dict[str, Any]]] | None = None,
    working_directory: str | Path | None = None,
) -> tuple[str, dict[str, Any]]:
    fallback_summary = t(MSG_AUDIT_CONFIRMATION_SUMMARY_FALLBACK, locale=cfg.security.audit_report_language)
    channel = await channel_crud.get(db, cfg.security.audit_channel_id)
    if channel is None or not channel.is_active or not cfg.security.audit_model_id:
        return fallback_summary, {"fallback": True}
    model_entry = get_model_entry(channel, cfg.security.audit_model_id, ModelUsage.CHAT.value)
    if model_entry is None:
        return fallback_summary, {"fallback": True}
    try:
        protocol = resolve_model_protocol(model_entry)
    except (KeyError, TypeError, ValueError):
        return fallback_summary, {"fallback": True}
    chat_params = resolve_chat_params(model_entry, cfg.channel.chat_channel)
    workdir = Path(working_directory or os.getcwd()).resolve(strict=False)
    summary_payload = {
        "working_directory": str(workdir),
        "tool_calls": tool_calls,
        "server_confirmation_reasons": server_confirmation_reasons or {},
    }
    summary_prompt = AUDIT_SUMMARY_PROMPT.format(audit_report_language=cfg.security.audit_report_language)
    audit_payload = _fit_audit_write_file_payload_to_context(summary_prompt, summary_payload, chat_params)
    messages = [
        InternalMessage(
            role=MessageRole.SYSTEM,
            content=summary_prompt,
        ),
        InternalMessage(
            role=MessageRole.USER,
            content=json.dumps(audit_payload, ensure_ascii=False),
        ),
    ]
    expected_tool_call_ids = {item.get("id") for item in tool_calls if isinstance(item, dict) and isinstance(item.get("id"), str)}
    read_state = {"calls": 0}
    read_results: list[dict[str, Any]] = []
    try:
        for _round_index in range(AUDIT_FILE_MAX_ROUNDS):
            await db.commit()
            _, request_context_tokens = _audit_read_token_budget(messages, chat_params)
            response = await LLMClient.generate(
                api_key=channel.get_decrypted_api_key(),
                base_url=channel.base_url,
                model_id=cfg.security.audit_model_id,
                messages=messages,
                temperature=chat_params["temperature"],
                top_p=chat_params["top_p"],
                max_tokens=chat_params["max_tokens"],
                timeout=chat_params["chat_timeout"],
                protocol=protocol,
                tools=[AUDIT_READ_TEXT_FILE_TOOL_SCHEMA],
                request_context_tokens=request_context_tokens,
                http_proxy=get_channel_http_proxy(channel),
                custom_headers=get_model_custom_headers(model_entry),
            )
            if not response.message.tool_calls:
                summary = (response.message.content or "").strip()
                if summary:
                    return summary[:500], {"request": [item.model_dump(mode="json", exclude_none=True) for item in messages], "response": summary, "file_reads": read_results}
                break
            messages.append(response.message)
            for tool_call in response.message.tool_calls:
                read_max_tokens, _ = _audit_read_token_budget(messages, chat_params)
                read_result = await _execute_audit_read_tool_call(
                    tool_call,
                    expected_tool_call_ids=expected_tool_call_ids,
                    working_directory=workdir,
                    read_state=read_state,
                    max_tokens=read_max_tokens,
                )
                read_result = _fit_audit_read_result_to_context(messages, tool_call.id, read_result, chat_params)
                read_results.append(read_result)
                messages.append(_audit_read_tool_message(tool_call.id, read_result))
    except Exception as exc:
        return fallback_summary, {"fallback": True, "error": str(exc)}
    return fallback_summary, {"fallback": True}


async def audit_tool_round(
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
    working_directory: str | Path | None = None,
) -> AuditRoundResult | None:
    if not is_audit_configured(cfg):
        return None
    audited_tool_calls = [item for item in tool_calls if tool_requires_audit(item.name)]
    if not audited_tool_calls:
        return None
    audited_tool_call_ids = [item.id for item in audited_tool_calls]
    audited_tool_call_id_set = set(audited_tool_call_ids)
    await cancel_confirmation_by_session(db, uid=uid, session_id=session_id, locale=cfg.security.audit_report_language)
    workdir = Path(working_directory or get_user_temp_dir(os.getcwd(), uid)).resolve(strict=False)
    payload_calls = _tool_payload(tool_calls)
    append_file_snapshots = await asyncio.to_thread(
        _collect_append_file_snapshots,
        audited_tool_calls,
        workdir,
    )
    server_confirmation_reasons: dict[str, list[dict[str, Any]]] = {}
    server_blocked_tool_call_ids: set[str] = set()
    snapshot = build_tool_round_integrity_snapshot(tool_calls=payload_calls, uid=uid, session_id=session_id, working_directory=workdir)
    record = await audit_crud.create_preparing(
        db,
        uid=uid,
        operator_username=operator_username,
        session_id=session_id,
        source=source,
        language=language,
        source_assistant_message_id=source_assistant_message_id,
        working_directory=str(workdir),
        round_arguments_hash=snapshot.round_sha256,
        tool_count=len(tool_calls),
    )
    audit_record_id = int(record.id)
    logger.bind(
        audit_record_id=audit_record_id,
        uid=uid,
        session_id=session_id,
        source=source,
        model_id=str(cfg.security.audit_model_id or "-"),
        tool_count=len(tool_calls),
    ).info(
        t(
            "LOG_AUDIT_ROUND_STARTED",
            audit_record_id=audit_record_id,
            model_id=str(cfg.security.audit_model_id or "-"),
            tool_count=len(tool_calls),
            source=source,
        )
    )
    request_payload = {
        "confirmation_threshold": cfg.security.audit_threshold,
        "working_directory": str(workdir),
        "tool_calls": [
            {
                "tool_call_id": item.id,
                "turn_index": index,
                "tool_name": item.name,
                "arguments": item.arguments,
            }
            for index, item in enumerate(tool_calls)
            if item.id in audited_tool_call_id_set
        ],
    }
    failure_type = None
    error_reason = None
    request_context: dict[str, Any] = {"messages": []}
    response_context: dict[str, Any] = {}
    file_reads: list[dict[str, Any]] = []
    read_file_snapshots: dict[str, list[dict[str, Any]]] = {}
    high_risk_override = False
    try:
        conflict_ids = _round_conflict_ids(audited_tool_calls, workdir)
        if conflict_ids:
            server_blocked_tool_call_ids.update(conflict_ids)
            response_context = {"local_block": "same-round file write conflict"}
            audited_results = [
                {
                    "tool_call_id": call.id,
                    "score": 10 if call.id in conflict_ids else 0,
                    "reason": t(ERR_AUDIT_ROUND_FILE_CONFLICT) if call.id in conflict_ids else t(MSG_AUDIT_ROUND_SKIPPED),
                    "file_checks": [],
                }
                for call in audited_tool_calls
            ]
        else:
            for item in audited_tool_calls:
                if item.name == "execute_shell":
                    blacklisted = ShellExecutor.check_blacklist(str((item.arguments or {}).get("command", "")))
                    if blacklisted:
                        server_blocked_tool_call_ids.add(item.id)
                        response_context = {"local_block": blacklisted}
                        audited_results = [
                            {
                                "tool_call_id": call.id,
                                "score": 10 if call.id == item.id else 0,
                                "reason": t(ERR_TOOL_SHELL_BLACKLISTED, command=blacklisted) if call.id == item.id else t(MSG_AUDIT_ROUND_SKIPPED),
                                "file_checks": [],
                            }
                            for call in audited_tool_calls
                        ]
                        break
            else:
                request_context, response_context = await _call_auditor(
                    db,
                    cfg,
                    request_payload,
                    workdir,
                )
                audited_results = _parse_results(response_context["parsed"], audited_tool_call_ids)
        audited_results_by_id = {item["tool_call_id"]: item for item in audited_results}
        parsed_results = [
            audited_results_by_id.get(
                tool_call.id,
                {
                    "tool_call_id": tool_call.id,
                    "score": 0,
                    "reason": t(MSG_AUDIT_ROUND_SKIPPED),
                    "file_checks": [],
                },
            )
            for tool_call in tool_calls
        ]
        high_risk_override = any(isinstance(item.get("score"), int) and not isinstance(item["score"], bool) and item["score"] >= AUDIT_HIGH_RISK_SCORE for item in parsed_results)
        conclusions = []
        file_reads = response_context.get("file_reads", []) if isinstance(response_context, dict) else []
        expected_tool_call_ids = audited_tool_call_id_set
        read_file_snapshots = _file_snapshots_from_reads(file_reads, expected_tool_call_ids)
        read_protocol_failures = [item for item in file_reads if not isinstance(item, dict) or item.get("status") in {"denied", "invalid"} or item.get("tool_call_id") not in expected_tool_call_ids]
        for tool_call, result in zip(tool_calls, parsed_results, strict=True):
            if tool_call.id not in audited_tool_call_id_set:
                conclusions.append(AuditToolConclusion.PASSED)
                continue
            tool_file_reads = [item for item in file_reads if isinstance(item, dict) and item.get("tool_call_id") == tool_call.id]
            local_reasons = list(server_confirmation_reasons.get(tool_call.id, []))
            evidence_requires_confirmation = _requires_confirmation_from_evidence(
                tool_call,
                read_file_snapshots.get(tool_call.id, []),
                tool_file_reads,
                result.get("file_checks", []),
            )
            requires_confirmation = bool(read_protocol_failures) or evidence_requires_confirmation
            evidence_reason = _evidence_confirmation_reason(
                tool_call,
                read_file_snapshots.get(tool_call.id, []),
                tool_file_reads,
                result.get("file_checks", []),
            )
            if evidence_reason is not None:
                local_reasons.append(evidence_reason)
            if read_protocol_failures:
                local_reasons.append(
                    {
                        "code": "file_read_protocol_invalid",
                        "message": t(ERR_AUDIT_FILE_EVIDENCE_INSUFFICIENT),
                        "details": {"failure_count": len(read_protocol_failures)},
                    }
                )
            if local_reasons:
                server_confirmation_reasons[tool_call.id] = local_reasons
            if local_reasons:
                result["reason"] = f"{result['reason']}; {'; '.join(item['message'] for item in local_reasons)}"
            if tool_call.id in server_blocked_tool_call_ids:
                conclusion = AuditToolConclusion.BLOCKED
            else:
                conclusion = classify_audit_score(result["score"], cfg.security.audit_threshold)
                if cfg.security.audit_threshold > 0 and (local_reasons or requires_confirmation) and conclusion != AuditToolConclusion.BLOCKED:
                    conclusion = AuditToolConclusion.PENDING
            conclusions.append(conclusion)
        status = _aggregate(conclusions)
    except Exception as exc:
        status = AuditRecordStatus.AUDIT_FAILED
        failure_type = AuditFailureType.AUDIT_SERVICE_FAILED
        error_reason = str(exc)
        parsed_results = [{"tool_call_id": item.id, "score": None, "reason": error_reason} for item in tool_calls]
        conclusions = [AuditToolConclusion.AUDIT_FAILED for _ in tool_calls]
        response_context = {"error": error_reason}

    intent_summary = None
    summary_context = None
    expires_at = None
    if status == AuditRecordStatus.PENDING:
        intent_summary, summary_context = await _summarize_pending(
            db,
            cfg,
            _tool_payload(audited_tool_calls),
            server_confirmation_reasons,
            working_directory=workdir,
        )
    if status == AuditRecordStatus.PENDING:
        expires_at = get_local_time() + timedelta(seconds=cfg.security.audit_confirmation_timeout_seconds)

    details = []
    for index, (call_snapshot, result, conclusion) in enumerate(zip(snapshot.tool_calls, parsed_results, conclusions, strict=True)):
        file_snapshots = [*append_file_snapshots.get(call_snapshot.tool_call_id, []), *read_file_snapshots.get(call_snapshot.tool_call_id, [])]
        details.append(
            {
                "original_tool_call_id": call_snapshot.tool_call_id,
                "turn_index": index,
                "tool_name": call_snapshot.tool_name,
                "conclusion": conclusion.value,
                "score": result["score"],
                "reason": result["reason"],
                "arguments_hash": call_snapshot.arguments_sha256,
                "arguments_summary": serialize_tool_arguments(call_snapshot.arguments),
                "file_snapshots": file_snapshots,
                "server_confirmation_reasons": server_confirmation_reasons.get(call_snapshot.tool_call_id, []),
            }
        )
    context_payload = {
        "audit_record_id": audit_record_id,
        "source_assistant_message_id": source_assistant_message_id,
        "round_arguments_hash": snapshot.round_sha256,
        "tool_calls": payload_calls,
        "audited_tool_call_ids": audited_tool_call_ids,
        "server_confirmation_reasons": server_confirmation_reasons,
        "scoring_request": request_context,
        "scoring_response": response_context,
        "results": details,
    }
    if summary_context is not None:
        context_payload["summary"] = summary_context
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
        status = AuditRecordStatus.AUDIT_FAILED
        error_reason = "audit persistence failed"
        parsed_results = [{**item, "reason": error_reason} for item in parsed_results]

    tool_scores = {str(item.get("tool_call_id")): item.get("score") for item in parsed_results if isinstance(item, dict) and item.get("tool_call_id")}
    score_values = [score for score in tool_scores.values() if isinstance(score, int) and not isinstance(score, bool)]
    max_score = max(score_values) if score_values else "-"
    logger.bind(
        audit_record_id=audit_record_id,
        uid=uid,
        session_id=session_id,
        source=source,
        status=status.value,
        max_score=max_score,
        tool_scores=tool_scores,
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

    if status == AuditRecordStatus.PASSED:
        return AuditRoundResult(audit_record_id=audit_record_id, status=status, tool_results=())
    tool_results = tuple(_result_message(call, status, next(item["reason"] for item in parsed_results if item["tool_call_id"] == call.id)) for call in tool_calls)
    confirmation_payload = None
    if status == AuditRecordStatus.PENDING:
        risk_score = max(max(item["score"] or 0 for item in parsed_results), cfg.security.audit_threshold)
        expires_at_text = expires_at.isoformat() if expires_at else "-"
        timeout_seconds = cfg.security.audit_confirmation_timeout_seconds
        confirmation_payload = {
            "type": "audit_confirmation",
            "audit_record_id": audit_record_id,
            "summary": intent_summary,
            "risk": risk_score,
            "status": status.value,
            "confirmation_mode": "high_risk_override" if high_risk_override else "standard",
            "expires_at": expires_at_text,
            "plain_text": t(
                MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM if high_risk_override else MSG_AUDIT_CONFIRMATION_IM,
                locale=language,
                summary=intent_summary,
                score=risk_score,
                expires_in_seconds=timeout_seconds,
            ),
        }
    return AuditRoundResult(audit_record_id=audit_record_id, status=status, tool_results=tool_results, confirmation_payload=confirmation_payload)
