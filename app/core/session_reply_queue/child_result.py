import asyncio
import json
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SUBAGENT_CANCELLED,
    ERR_SUBAGENT_CONTEXT_INVALID,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud as reply_work_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.tools.send_file_to_user import resolve_file_token
from app.models.audit import AuditRecordStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import Subagent, SubagentWork
from app.schemas.response import SentFile

__all__ = ["build_subagent_work_result", "get_child_snapshot", "load_child_work"]


async def load_child_work(
    db: AsyncSession,
    *,
    subagent: Subagent,
    round_row: SubagentWork,
) -> SessionReplyWorkItem:
    work = await reply_work_crud.get(db, round_row.current_work_id)
    if work is None:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    await db.refresh(work)
    if round_row.subagent_id != subagent.id or work.id != round_row.current_work_id or work.uid != subagent.uid or work.session_id != subagent.session_id or work.profile_id != subagent.profile_id or work.subagent_id != subagent.id or work.execution_state.get("child_work_id", work.id) != round_row.child_work_id:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
    return work


async def _load_saved_message(db: AsyncSession, work: SessionReplyWorkItem) -> Message | None:
    message = await db.get(Message, work.result_message_id) if work.result_message_id is not None else None
    if message is None or not (message.uid == work.uid and message.session_id == work.session_id and message.profile_id == work.profile_id and message.work_id in (work.id, None)):
        message = await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
    if message is not None and (message.uid == work.uid and message.session_id == work.session_id and message.profile_id == work.profile_id and message.work_id in (work.id, None)):
        return message
    return None


def _parse_audit_record_id(message: Message) -> int | None:
    try:
        payload = json.loads(message.content or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("audit_record_id")
    if isinstance(value, int) and not isinstance(value, bool):
        audit_record_id = value
    elif isinstance(value, str) and value.isdigit():
        audit_record_id = int(value)
    else:
        return None
    return audit_record_id if audit_record_id > 0 else None


async def _failed_execution_is_unknown(db: AsyncSession, work: SessionReplyWorkItem) -> bool:
    if SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in work.execution_state:
        return True
    checkpoint = await reply_work_crud.get_checkpoint(db, work.id)
    if checkpoint is None or SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint:
        return checkpoint is not None
    pending_tool_calls = Counter()
    for message in checkpoint["messages"]:
        if message["role"] == MessageRole.ASSISTANT:
            if pending_tool_calls:
                return True
            pending_tool_calls = Counter(tool_call["id"] for tool_call in (message.get("tool_calls") or []))
        elif message["role"] == MessageRole.TOOL:
            tool_call_id = message.get("tool_call_id")
            if pending_tool_calls[tool_call_id] > 0:
                pending_tool_calls[tool_call_id] -= 1
                if pending_tool_calls[tool_call_id] == 0:
                    del pending_tool_calls[tool_call_id]
    if pending_tool_calls:
        return True
    return any(message["role"] == MessageRole.TOOL and message.get("tool_execution_status") == "execution_unknown" for key in ("messages", "turn_messages") for message in checkpoint.get(key, []))


def _add_verified_generated_image_paths(files: list[Any], uid: str) -> list[dict[str, Any]]:
    verified_files: list[dict[str, Any]] = []

    if not isinstance(uid, str) or not uid or "\x00" in uid or uid in {".", ".."} or PurePosixPath(uid).name != uid or PureWindowsPath(uid).name != uid:
        return verified_files

    try:
        user_dir = get_user_temp_dir(Path.cwd(), uid)
        temp_root = user_dir.parent.resolve(strict=False)
        generated_dir = temp_root / user_dir.name / "generated_images"
    except (OSError, RuntimeError, ValueError):
        return verified_files

    for file_item in files:
        try:
            sent_file = SentFile.model_validate(file_item)
            if not sent_file.mime_type.lower().startswith("image/"):
                continue
            if sent_file.download_url != f"/api/v1/download-sent?token={quote(sent_file.id)}":
                continue
            registered_path = resolve_file_token(sent_file.id, uid=uid)
            if registered_path.parent != generated_dir:
                continue
            resolved_path = registered_path.resolve(strict=True)
            if resolved_path != registered_path or not resolved_path.is_file():
                continue
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        verified_file = sent_file.model_dump()
        verified_file["name"] = resolved_path.name
        verified_file["path"] = str(resolved_path)
        verified_files.append(verified_file)

    return verified_files


def _parse_saved_assistant_files(content: str | None) -> tuple[str, list[Any]] | None:
    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "assistant_files":
        return None
    text = payload.get("text")
    files = payload.get("files")
    if not isinstance(text, str) or not isinstance(files, list) or not files:
        return None
    return text, files


async def get_child_snapshot(
    db: AsyncSession,
    *,
    subagent: Subagent,
    round_row: SubagentWork,
) -> dict[str, Any]:
    if isinstance(round_row.result, dict):
        return deepcopy(round_row.result)

    work = await load_child_work(db, subagent=subagent, round_row=round_row)
    message = await _load_saved_message(db, work)
    snapshot: dict[str, Any] = {
        "work_id": round_row.child_work_id,
        "current_work_id": work.id,
        "completed": False,
        "status": work.status.value,
        "result": None,
        "error": None,
    }

    if round_row.waiting_confirmation or (message is not None and message.type == MessageType.AUDIT_CONFIRMATION):
        if message is None:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        audit_record_id = _parse_audit_record_id(message)
        if audit_record_id is None:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.id != audit_record_id or record.source_work_id != work.id or record.uid != work.uid or record.session_id != work.session_id:
            raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)
        snapshot.update(status="waiting_confirmation", completed=False, result=None)
        if record.status in {
            AuditRecordStatus.REJECTED,
            AuditRecordStatus.EXPIRED,
            AuditRecordStatus.CANCELLED,
            AuditRecordStatus.BLOCKED,
            AuditRecordStatus.AUDIT_FAILED,
        }:
            snapshot.update(
                status="cancelled",
                completed=True,
                error=record.error_reason or t(ERR_SUBAGENT_CANCELLED),
            )
        elif record.status == AuditRecordStatus.EXECUTION_UNKNOWN:
            snapshot.update(
                status="execution_unknown",
                completed=True,
                error=record.error_reason or t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN),
            )
        return snapshot

    status = work.status.value
    if status in {
        SessionReplyWorkStatus.READY_FOR_LLM.value,
        SessionReplyWorkStatus.RUNNING.value,
        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value,
    }:
        if status == SessionReplyWorkStatus.RUNNING.value and (work.lock_until is None or work.lock_until <= time.time()):
            snapshot.update(status="execution_unknown", error=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
        return snapshot

    if status == SessionReplyWorkStatus.SUCCEEDED.value:
        response = work.execution_state.get("response") or {}
        final_text = message is not None and message.type == MessageType.TEXT and message.role == MessageRole.ASSISTANT
        if response.get("skip_reply") is True or final_text:
            content = deepcopy(response.get("content"))
            response_files = response.get("files")
            skip_reply = response.get("skip_reply") is True
            if not response_files and final_text and not skip_reply:
                saved_assistant_files = _parse_saved_assistant_files(message.content)
                if saved_assistant_files is not None:
                    saved_content, response_files = saved_assistant_files
                    if content is None:
                        content = saved_content
            if isinstance(response_files, list) and response_files:
                response_files = await asyncio.to_thread(_add_verified_generated_image_paths, response_files, work.uid)
            else:
                response_files = deepcopy(response_files)
            result = {
                "content": content,
                "files": response_files,
                "skip_reply": skip_reply,
            }
            if result["content"] is None and message is not None:
                result["content"] = message.content
            snapshot.update(completed=True, status="succeeded", result=result)
        else:
            snapshot.update(
                completed=True,
                status="execution_unknown",
                error=t(ERR_SUBAGENT_CONTEXT_INVALID),
            )
        return snapshot

    if status == SessionReplyWorkStatus.FAILED.value:
        snapshot["completed"] = True
        if await _failed_execution_is_unknown(db, work):
            snapshot.update(status="execution_unknown", error=work.error or t(ERR_LLM_UNEXPECTED_ERROR))
        else:
            snapshot["error"] = work.error or t(ERR_LLM_UNEXPECTED_ERROR)
        return snapshot

    if status == SessionReplyWorkStatus.CANCELLED.value:
        snapshot.update(completed=True, error=work.error or t(ERR_SUBAGENT_CANCELLED))
    return snapshot


def build_subagent_work_result(subagent_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    result = {
        "subagent_id": subagent_id,
        "work_id": snapshot["work_id"],
        "status": snapshot["status"],
    }
    internal_result = snapshot.get("result")
    if isinstance(internal_result, dict):
        if internal_result.get("content"):
            result["content"] = deepcopy(internal_result["content"])
        if internal_result.get("files"):
            result["files"] = deepcopy(internal_result["files"])
        if snapshot["status"] == "succeeded" and internal_result.get("skip_reply") is True:
            result["status"] = "ended_without_reply"
    if snapshot.get("error"):
        result["reason"] = deepcopy(snapshot["error"])
    return result
