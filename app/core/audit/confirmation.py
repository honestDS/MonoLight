import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_EXPIRED,
    ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER,
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE,
    MSG_AUDIT_CONFIRMATION_STATUS_IM,
    MSG_AUDIT_CONFIRMATION_SUPERSEDED,
    MSG_AUDIT_STATUS_CANCELLED,
    MSG_AUDIT_STATUS_EXECUTING,
    MSG_AUDIT_STATUS_EXECUTION_UNKNOWN,
    MSG_AUDIT_STATUS_EXPIRED,
    MSG_AUDIT_STATUS_FAILED,
    MSG_AUDIT_STATUS_REJECTED,
    MSG_AUDIT_STATUS_SUCCEEDED,
)
from app.core.crud.audit import audit_crud
from app.core.crud.audit_tool_result_version import audit_tool_result_version_crud
from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.notifier import send_session_event
from app.core.utils.dispatcher.save_message import save_message
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType


class ConfirmationDecision(StrEnum):
    APPROVE = "approve"
    IGNORE = "ignore"
    REJECT = "reject"


_APPROVE_WORDS = {"同意", "继续", "approve", "continue"}
_IGNORE_WORDS = {"忽略", "ignore"}
_REJECT_WORDS = {"拒绝", "reject"}
_CONFIRMATION_CANDIDATE_WORDS = _APPROVE_WORDS | _IGNORE_WORDS | _REJECT_WORDS
CONFIRMATION_DECISION_FIELD = "confirmation_decision"
REJECTION_SOURCE_FIELD = "rejection_source"
_STATUS_TEXT_KEYS = {
    "executing": MSG_AUDIT_STATUS_EXECUTING,
    "rejected": MSG_AUDIT_STATUS_REJECTED,
    "expired": MSG_AUDIT_STATUS_EXPIRED,
    "cancelled": MSG_AUDIT_STATUS_CANCELLED,
    "succeeded": MSG_AUDIT_STATUS_SUCCEEDED,
    "failed": MSG_AUDIT_STATUS_FAILED,
    "execution_unknown": MSG_AUDIT_STATUS_EXECUTION_UNKNOWN,
}

logger = get_logger(__name__)


@dataclass(frozen=True)
class ConfirmationStatusUpdate:
    record: Any
    message_id: int
    status: str
    content: str


@dataclass(frozen=True)
class ConfirmationMessageProjection:
    record: Any
    status_update: ConfirmationStatusUpdate | None


@dataclass(frozen=True)
class PendingConfirmationCancellation:
    tool_results: list[InternalMessage]
    status_update: ConfirmationStatusUpdate | None


async def persist_pending_confirmation_bundle(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_results: list[InternalMessage] | tuple[InternalMessage, ...],
    confirmation_payload: dict[str, Any],
    dedupe_key: str | None,
) -> tuple[list[InternalMessage], InternalMessage]:
    try:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.source_assistant_message_id is None:
            raise LookupError(audit_record_id)
        stored_tool_results: list[InternalMessage] = []
        for tool_result in tool_results:
            stored_tool_result = tool_result.model_copy()
            saved_message = await save_message(
                db,
                session_id,
                uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                stored_tool_result,
                profile_id,
                is_processed=True,
                audit_record_id=audit_record_id,
                audit_tool_call_id=stored_tool_result.tool_call_id,
                commit=False,
            )
            if saved_message.id is None or not isinstance(stored_tool_result.tool_call_id, str):
                raise LookupError(audit_record_id)
            await audit_tool_result_version_crud.append_version(
                db,
                uid=uid,
                session_id=session_id,
                audit_record_id=audit_record_id,
                source_assistant_message_id=record.source_assistant_message_id,
                original_tool_call_id=stored_tool_result.tool_call_id,
                message_id=saved_message.id,
                content=saved_message.content or "",
                commit=False,
            )
            stored_tool_result.id = saved_message.id
            stored_tool_result.created_at = saved_message.created_at
            stored_tool_results.append(stored_tool_result)

        confirmation_message = await save_message(
            db,
            session_id,
            uid,
            MessageRole.ASSISTANT,
            MessageType.AUDIT_CONFIRMATION,
            confirmation_payload,
            profile_id,
            is_processed=True,
            dedupe_key=dedupe_key,
            commit=False,
        )
        activated = await audit_crud.activate_confirmation_claim(
            db,
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            commit=False,
        )
        if not activated:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        await db.commit()
        return stored_tool_results, confirmation_message
    except Exception as exc:
        await db.rollback()
        try:
            await audit_crud.mark_pending_persistence_failed(
                db,
                audit_record_id=audit_record_id,
                error_reason=str(exc),
            )
        except Exception:
            pass
        raise


async def persist_cancelled_pending_audit_results(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_results: list[InternalMessage] | tuple[InternalMessage, ...],
) -> list[InternalMessage]:
    try:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.status != AuditRecordStatus.PENDING or record.uid != uid or record.session_id != session_id or record.source_assistant_message_id is None:
            raise LookupError(audit_record_id)

        cancellation_reason = t(MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE, locale=record.language)
        stored_tool_results: list[InternalMessage] = []
        for tool_result in tool_results:
            stored_tool_result = tool_result.model_copy(deep=True)
            if stored_tool_result.role != MessageRole.TOOL or not isinstance(stored_tool_result.tool_call_id, str):
                raise LookupError(audit_record_id)
            try:
                result_payload = json.loads(stored_tool_result.content or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError from exc
            if not isinstance(result_payload, dict) or result_payload.get("status") != AuditRecordStatus.PENDING.value:
                raise ValueError
            result_payload.update(
                status=AuditRecordStatus.CANCELLED.value,
                confirmation_status="superseded",
                error=cancellation_reason,
            )
            stored_tool_result.content = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            saved_message = await save_message(
                db,
                session_id,
                uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                stored_tool_result,
                profile_id,
                is_processed=True,
                audit_record_id=audit_record_id,
                audit_tool_call_id=stored_tool_result.tool_call_id,
                commit=False,
            )
            if saved_message.id is None:
                raise LookupError(audit_record_id)
            await audit_tool_result_version_crud.append_version(
                db,
                uid=uid,
                session_id=session_id,
                audit_record_id=audit_record_id,
                source_assistant_message_id=record.source_assistant_message_id,
                original_tool_call_id=stored_tool_result.tool_call_id,
                message_id=saved_message.id,
                content=saved_message.content or "",
                commit=False,
            )
            stored_tool_result.id = saved_message.id
            stored_tool_result.created_at = saved_message.created_at
            stored_tool_results.append(stored_tool_result)

        closed = await audit_crud.close_pending(
            db,
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            status=AuditRecordStatus.CANCELLED,
            error_reason=cancellation_reason,
            commit=False,
        )
        if not closed:
            raise LookupError(audit_record_id)
        await db.commit()
        return stored_tool_results
    except Exception as exc:
        await db.rollback()
        try:
            await audit_crud.mark_pending_persistence_failed(
                db,
                audit_record_id=audit_record_id,
                error_reason=str(exc),
            )
        except Exception:
            pass
        raise


def _normalize_confirmation_message(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
) -> str | None:
    if not isinstance(message, str) or attachments or has_quote:
        return None
    normalized = message.strip()
    if not normalized:
        return None
    return normalized.lower()


def is_confirmation_candidate(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
) -> bool:
    lowered = _normalize_confirmation_message(
        message,
        attachments=attachments,
        has_quote=has_quote,
    )
    return lowered in _CONFIRMATION_CANDIDATE_WORDS if lowered is not None else False


def parse_confirmation_decision(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
    requires_high_risk_override: bool = False,
) -> ConfirmationDecision | None:
    lowered = _normalize_confirmation_message(
        message,
        attachments=attachments,
        has_quote=has_quote,
    )
    if lowered is None:
        return None
    if lowered in _REJECT_WORDS:
        return ConfirmationDecision.REJECT
    if requires_high_risk_override:
        if lowered in _IGNORE_WORDS:
            return ConfirmationDecision.IGNORE
    elif lowered in _APPROVE_WORDS:
        return ConfirmationDecision.APPROVE
    return None


def message_has_quote(raw_message: object) -> bool:
    if not isinstance(raw_message, dict):
        return False
    quote_keys = {
        "quote",
        "quoted_message",
        "reference",
        "refer_message",
        "ref_message",
        "reply_to",
    }
    if any(raw_message.get(key) for key in quote_keys):
        return True
    item_list = raw_message.get("item_list")
    if not isinstance(item_list, list):
        return False
    return any(isinstance(item, dict) and (str(item.get("type") or "").lower() in {"quote", "reference", "reply"} or any(item.get(key) for key in quote_keys)) for item in item_list)


async def _get_structured_tool_result_messages(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    audit_record_id: int,
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(
            Message.uid == uid,
            Message.session_id == session_id,
            Message.type == MessageType.TOOL_RESULT,
            Message.audit_record_id == audit_record_id,
            Message.audit_tool_call_id.is_not(None),
        )
        .order_by(Message.id.asc())
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def get_pending_tool_results(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    source_assistant_message_id: int,
    before_message_id: int | None,
    tool_call_ids: list[str],
    audit_record_id: int | None = None,
) -> dict[str, Message] | None:
    if (audit_record_id is None and before_message_id is None) or not tool_call_ids or len(set(tool_call_ids)) != len(tool_call_ids):
        return None

    structured = audit_record_id is not None
    if structured:
        messages = await _get_structured_tool_result_messages(
            db,
            uid=uid,
            session_id=session_id,
            audit_record_id=audit_record_id,
        )
    else:
        messages = await message_crud.get_history_forward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            after_id=source_assistant_message_id,
            before_id=before_message_id,
            limit=500,
        )
    tool_result_messages = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    if len(tool_result_messages) != len(tool_call_ids):
        return None

    expected_tool_call_ids = set(tool_call_ids)
    pending_results: dict[str, Message] = {}
    for message in tool_result_messages:
        try:
            tool_result = InternalMessage.model_validate_json(message.content or "{}")
            result_payload = json.loads(tool_result.content or "{}")
        except (TypeError, ValueError):
            return None
        tool_call_id = message.audit_tool_call_id if structured else tool_result.tool_call_id
        if structured and tool_result.tool_call_id != tool_call_id:
            return None
        if tool_result.role != MessageRole.TOOL or not isinstance(tool_call_id, str) or tool_call_id not in expected_tool_call_ids or tool_call_id in pending_results or not isinstance(result_payload, dict) or result_payload.get("status") != AuditRecordStatus.PENDING.value:
            return None
        pending_results[tool_call_id] = message

    if set(pending_results) != expected_tool_call_ids:
        return None
    return pending_results


async def replace_pending_tool_result(
    db: AsyncSession,
    *,
    pending_message: Message,
    original_tool_call_id: str,
    content: str | None,
    audit_record_id: int | None = None,
) -> str | None:
    replacement_content = content
    confirmation_decision = None
    try:
        pending_tool_result = InternalMessage.model_validate_json(pending_message.content or "{}")
        pending_payload = json.loads(pending_tool_result.content or "{}")
        if isinstance(pending_payload, dict):
            decision_value = pending_payload.get(CONFIRMATION_DECISION_FIELD)
            if isinstance(decision_value, str):
                confirmation_decision = decision_value
    except (TypeError, ValueError):
        pass

    if confirmation_decision is not None:
        try:
            result_payload = json.loads(replacement_content or "{}")
        except (TypeError, ValueError):
            result_payload = None
        if isinstance(result_payload, dict):
            result_payload[CONFIRMATION_DECISION_FIELD] = confirmation_decision
            replacement_content = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    stored_tool_result = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=original_tool_call_id,
        content=replacement_content,
    )
    serialized_content = stored_tool_result.model_dump_json(exclude_none=True)
    if audit_record_id is not None:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.source_assistant_message_id is None or pending_message.id is None:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        await audit_tool_result_version_crud.append_version(
            db,
            uid=pending_message.uid,
            session_id=pending_message.session_id,
            audit_record_id=audit_record_id,
            source_assistant_message_id=record.source_assistant_message_id,
            original_tool_call_id=original_tool_call_id,
            message_id=pending_message.id,
            content=serialized_content,
            commit=False,
        )
    else:
        updated = await message_crud.update_content_if_matches(
            db,
            message_id=pending_message.id,
            expected_content=pending_message.content,
            content=serialized_content,
            message_type=MessageType.TOOL_RESULT,
            commit=False,
        )
        if not updated:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        await session_crud.bump_context_content_revision(
            db,
            session_id=pending_message.session_id,
            uid=pending_message.uid,
            commit=False,
        )
    await db.refresh(pending_message)
    return replacement_content


async def _update_confirmation_tool_results(
    db: AsyncSession,
    *,
    audit_record_id: int,
    before_message_id: int | None,
    status: AuditRecordStatus,
    confirmation_status: str,
    feedback: str | None,
    confirmation_decision: str | None = None,
) -> int:
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.source_assistant_message_id is None:
        return 0

    source_message = await message_crud.get(db, record.source_assistant_message_id)
    if source_message is None or source_message.uid != record.uid or source_message.session_id != record.session_id or source_message.role != MessageRole.ASSISTANT or source_message.type != MessageType.TOOL_CALL:
        return 0
    try:
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    except ValueError:
        return 0
    tool_call_ids = {tool_call.id for tool_call in source_internal.tool_calls or []}
    if not tool_call_ids:
        return 0

    structured_messages = await _get_structured_tool_result_messages(
        db,
        uid=record.uid,
        session_id=record.session_id,
        audit_record_id=audit_record_id,
    )
    structured = bool(structured_messages)
    if structured:
        messages = structured_messages
    else:
        messages = await message_crud.get_history_forward_by_id(
            db,
            session_id=record.session_id,
            uid=record.uid,
            after_id=record.source_assistant_message_id,
            before_id=before_message_id,
            limit=500,
        )
    updated_count = 0
    for message in messages:
        if message.type != MessageType.TOOL_RESULT:
            continue
        try:
            tool_result = InternalMessage.model_validate_json(message.content or "{}")
            result_payload = json.loads(tool_result.content or "{}")
        except (TypeError, ValueError):
            continue
        tool_call_id = message.audit_tool_call_id if structured else tool_result.tool_call_id
        if structured and tool_result.tool_call_id != tool_call_id:
            continue
        if tool_call_id not in tool_call_ids or not isinstance(result_payload, dict):
            continue
        if result_payload.get("status") != AuditRecordStatus.PENDING.value:
            continue
        result_payload.update(status=status.value, confirmation_status=confirmation_status)
        if feedback is not None:
            result_payload["error"] = feedback
        if status == AuditRecordStatus.REJECTED:
            result_payload["error"] = t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER, locale=record.language)
            result_payload[REJECTION_SOURCE_FIELD] = "user"
        if confirmation_decision is not None:
            result_payload[CONFIRMATION_DECISION_FIELD] = confirmation_decision
        tool_result.content = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        serialized_content = tool_result.model_dump_json(exclude_none=True)
        if structured:
            if message.id is None:
                continue
            await audit_tool_result_version_crud.append_version(
                db,
                uid=record.uid,
                session_id=record.session_id,
                audit_record_id=audit_record_id,
                source_assistant_message_id=record.source_assistant_message_id,
                original_tool_call_id=tool_call_id,
                message_id=message.id,
                content=serialized_content,
                commit=False,
            )
            await db.refresh(message)
            updated_count += 1
        elif await message_crud.update_content(
            db,
            message_id=message.id,
            content=serialized_content,
            commit=False,
        ):
            await db.refresh(message)
            updated_count += 1
    if not structured and updated_count:
        await session_crud.bump_context_content_revision(
            db,
            session_id=record.session_id,
            uid=record.uid,
            commit=False,
        )
    return updated_count


async def _get_cancelled_structured_tool_results(
    db: AsyncSession,
    *,
    record,
) -> list[InternalMessage]:
    if record.id is None:
        raise LookupError(record.id)
    stored_messages = await _get_structured_tool_result_messages(
        db,
        uid=record.uid,
        session_id=record.session_id,
        audit_record_id=record.id,
    )
    if not stored_messages:
        return []
    if len(stored_messages) != record.tool_count:
        raise LookupError(record.id)

    tool_results: list[InternalMessage] = []
    tool_call_ids: set[str] = set()
    for stored_message in stored_messages:
        tool_result = InternalMessage.model_validate_json(stored_message.content or "{}")
        if tool_result.role != MessageRole.TOOL:
            raise ValueError(record.id)
        if not isinstance(tool_result.tool_call_id, str) or not tool_result.tool_call_id or tool_result.tool_call_id != stored_message.audit_tool_call_id or tool_result.tool_call_id in tool_call_ids:
            raise ValueError(record.id)
        if not isinstance(tool_result.content, str):
            raise ValueError(record.id)
        try:
            result_payload = json.loads(tool_result.content)
        except (TypeError, ValueError) as exc:
            raise ValueError(record.id) from exc
        if not isinstance(result_payload, dict) or result_payload.get("status") != AuditRecordStatus.CANCELLED.value:
            raise ValueError(record.id)

        tool_call_ids.add(tool_result.tool_call_id)
        tool_result.id = stored_message.id
        tool_result.created_at = stored_message.created_at
        tool_results.append(tool_result)
    return tool_results


async def cancel_persisted_pending_confirmation_bundle(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    feedback: str,
    confirmation_status: str,
    commit: bool = True,
) -> PendingConfirmationCancellation:
    """原子取消已持久化的待确认审计，并同步工具结果和确认卡片。

    ``commit=False`` 时由调用方提交整个外层事务，并在成功后调用
    ``broadcast_pending_confirmation_cancellation``。
    """
    try:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.uid != uid or record.session_id != session_id:
            raise LookupError(audit_record_id)

        closed = False
        if record.status == AuditRecordStatus.PENDING:
            closed = await audit_crud.close_pending(
                db,
                audit_record_id=audit_record_id,
                uid=uid,
                session_id=session_id,
                status=AuditRecordStatus.CANCELLED,
                error_reason=feedback,
                commit=False,
            )
            if closed:
                await _update_confirmation_tool_results(
                    db,
                    audit_record_id=audit_record_id,
                    before_message_id=None,
                    status=AuditRecordStatus.CANCELLED,
                    confirmation_status=confirmation_status,
                    feedback=feedback,
                )
            record = await audit_crud.get_record(db, audit_record_id)

        if record is None or record.uid != uid or record.session_id != session_id or record.status != AuditRecordStatus.CANCELLED:
            raise LookupError(audit_record_id)
        tool_results = await _get_cancelled_structured_tool_results(db, record=record)
        projection = await _sync_confirmation_message_status_projection(
            db,
            audit_record_id=audit_record_id,
            commit=False,
        )
        if projection is None:
            raise LookupError(audit_record_id)
        cancellation = PendingConfirmationCancellation(
            tool_results=tool_results,
            status_update=projection.status_update,
        )
        if commit:
            await db.commit()
    except Exception:
        if commit:
            await db.rollback()
        raise
    if commit:
        await broadcast_pending_confirmation_cancellation(db, cancellation=cancellation)
    return cancellation


async def supersede_persisted_pending_confirmation_bundle(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
) -> list[InternalMessage]:
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.uid != uid or record.session_id != session_id:
        raise LookupError(audit_record_id)
    cancellation = await cancel_persisted_pending_confirmation_bundle(
        db,
        audit_record_id=audit_record_id,
        uid=uid,
        session_id=session_id,
        feedback=t(MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE, locale=record.language),
        confirmation_status="superseded",
    )
    return cancellation.tool_results


async def update_confirmation_tool_results_for_decision(
    db: AsyncSession,
    *,
    audit_record_id: int,
    before_message_id: int,
    decision: ConfirmationDecision,
    raw_message: str,
) -> int:
    status = AuditRecordStatus.PENDING if decision in {ConfirmationDecision.APPROVE, ConfirmationDecision.IGNORE} else AuditRecordStatus.REJECTED
    return await _update_confirmation_tool_results(
        db,
        audit_record_id=audit_record_id,
        before_message_id=before_message_id,
        status=status,
        confirmation_status=decision.value,
        feedback=None,
        confirmation_decision=raw_message,
    )


async def _get_confirmation_message(db: AsyncSession, record) -> tuple[Message | None, dict | None]:
    for candidate in await message_crud.list_by_type(
        db,
        uid=record.uid,
        session_id=record.session_id,
        message_type=MessageType.AUDIT_CONFIRMATION,
    ):
        try:
            candidate_payload = json.loads(candidate.content or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(candidate_payload, dict) and str(candidate_payload.get("audit_record_id")) == str(record.id):
            return candidate, candidate_payload
    return None, None


async def _get_confirmation_tool_result_events(db: AsyncSession, record) -> list[dict]:
    if record.source_assistant_message_id is None or record.id is None:
        return []

    source_message = await message_crud.get(db, record.source_assistant_message_id)
    if source_message is None:
        return []
    try:
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    except ValueError:
        return []

    tool_call_ids = [tool_call.id for tool_call in source_internal.tool_calls or []]
    if not tool_call_ids:
        return []
    structured_messages = await _get_structured_tool_result_messages(
        db,
        uid=record.uid,
        session_id=record.session_id,
        audit_record_id=record.id,
    )
    structured = bool(structured_messages)
    if structured:
        messages = structured_messages
    else:
        messages = await message_crud.get_history_forward_by_id(
            db,
            session_id=record.session_id,
            uid=record.uid,
            after_id=record.source_assistant_message_id,
            before_id=record.decision_message_id,
            limit=500,
        )
    expected_tool_call_ids = set(tool_call_ids)
    results_by_tool_call_id: dict[str, dict] = {}
    for message in messages:
        if message.type != MessageType.TOOL_RESULT or message.id is None:
            continue
        try:
            tool_result = InternalMessage.model_validate_json(message.content or "{}")
        except ValueError:
            continue
        tool_call_id = message.audit_tool_call_id if structured else tool_result.tool_call_id
        if structured and tool_result.tool_call_id != tool_call_id:
            continue
        if tool_result.role != MessageRole.TOOL or tool_call_id not in expected_tool_call_ids or tool_call_id in results_by_tool_call_id:
            continue
        created_at = message.created_at.timestamp() if message.created_at is not None else None
        results_by_tool_call_id[tool_call_id] = {
            "id": message.id,
            "db_id": message.id,
            "role": message.role.value if isinstance(message.role, MessageRole) else str(message.role),
            "type": message.type.value if isinstance(message.type, MessageType) else str(message.type),
            "content": message.content,
            "tool_call_id": tool_call_id,
            "created_at": created_at,
        }
    return [results_by_tool_call_id[tool_call_id] for tool_call_id in tool_call_ids if tool_call_id in results_by_tool_call_id]


def _confirmation_status_value(record) -> str:
    return record.status.value if isinstance(record.status, AuditRecordStatus) else str(record.status)


async def _send_confirmation_status_event(
    record,
    *,
    message_id: int | None,
    status: str,
    content: str | None,
    event_id: str,
) -> None:
    event = {
        "type": "audit_confirmation_status",
        "source": "audit_confirmation",
        "event_id": event_id,
        "session_id": record.session_id,
        "audit_record_id": record.id,
        "message_id": message_id,
        "status": status,
        "content": content,
    }
    try:
        await send_session_event(record.uid, record.session_id, event)
    except Exception:
        logger.bind(uid=record.uid, session_id=record.session_id, audit_record_id=record.id, status=status).warning(
            "Failed to broadcast audit confirmation status",
            exc_info=True,
        )


async def _send_confirmation_tool_results_event(record, tool_results: list[dict]) -> None:
    if not tool_results:
        return

    result_hash = hashlib.sha256(json.dumps(tool_results, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    event = {
        "type": "audit_tool_results_update",
        "source": "audit_confirmation",
        "event_id": f"audit-tool-results:{record.id}:{result_hash}",
        "session_id": record.session_id,
        "audit_record_id": record.id,
        "messages": tool_results,
    }
    try:
        await send_session_event(record.uid, record.session_id, event)
    except Exception:
        logger.bind(uid=record.uid, session_id=record.session_id, audit_record_id=record.id).warning(
            "Failed to broadcast audit tool result update",
            exc_info=True,
        )


async def notify_confirmation_tool_results(db: AsyncSession, *, audit_record_id: int) -> bool:
    """在工具结果已提交后，广播数据库中的最终工具结果。"""
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.id is None:
        return False
    tool_results = await _get_confirmation_tool_result_events(db, record)
    if not tool_results:
        return False
    await _send_confirmation_tool_results_event(record, tool_results)
    return True


async def _broadcast_confirmation_status_update(
    db: AsyncSession,
    *,
    status_update: ConfirmationStatusUpdate,
) -> None:
    await _send_confirmation_status_event(
        status_update.record,
        message_id=status_update.message_id,
        status=status_update.status,
        content=status_update.content,
        event_id=f"audit-confirmation:{status_update.record.id}:{status_update.status}",
    )
    tool_results = await _get_confirmation_tool_result_events(db, status_update.record)
    await _send_confirmation_tool_results_event(status_update.record, tool_results)


async def _sync_confirmation_message_status_projection(
    db: AsyncSession,
    *,
    audit_record_id: int,
    commit: bool,
) -> ConfirmationMessageProjection | None:
    for _ in range(3):
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.id is None:
            return None
        status = record.status.value if isinstance(record.status, AuditRecordStatus) else str(record.status)
        if status == AuditRecordStatus.PREPARING.value:
            return None
        message, payload = await _get_confirmation_message(db, record)
        if message is None or message.id is None or payload is None:
            return None
        if str(payload.get("status") or "") == status:
            return ConfirmationMessageProjection(record=record, status_update=None)

        payload["status"] = status
        status_key = _STATUS_TEXT_KEYS.get(status)
        if status_key:
            payload["plain_text"] = t(
                MSG_AUDIT_CONFIRMATION_STATUS_IM,
                locale=record.language,
                summary=str(payload.get("summary") or ""),
                status=t(status_key, locale=record.language),
            )
        serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        updated = await message_crud.update_content_if_matches(
            db,
            message_id=message.id,
            expected_content=message.content,
            content=serialized_payload,
            commit=False,
        )
        if not updated:
            continue

        if commit:
            await db.commit()
        else:
            await db.flush()
        return ConfirmationMessageProjection(
            record=record,
            status_update=ConfirmationStatusUpdate(
                record=record,
                message_id=message.id,
                status=status,
                content=serialized_payload,
            ),
        )
    return None


async def update_confirmation_message_status(
    db: AsyncSession,
    *,
    audit_record_id: int,
    commit: bool = True,
) -> bool:
    projection = await _sync_confirmation_message_status_projection(
        db,
        audit_record_id=audit_record_id,
        commit=commit,
    )
    if projection is None or projection.status_update is None:
        return False
    if commit:
        await _broadcast_confirmation_status_update(db, status_update=projection.status_update)
    return True


async def broadcast_pending_confirmation_cancellation(
    db: AsyncSession,
    *,
    cancellation: PendingConfirmationCancellation,
) -> None:
    """广播已提交的取消投影。"""
    if cancellation.status_update is not None:
        await _broadcast_confirmation_status_update(db, status_update=cancellation.status_update)


async def sync_expired_confirmation_messages(
    db: AsyncSession,
    *,
    audit_record_id: int,
    locale: str | None,
) -> None:
    await _update_confirmation_tool_results(
        db,
        audit_record_id=audit_record_id,
        before_message_id=None,
        status=AuditRecordStatus.EXPIRED,
        confirmation_status=AuditRecordStatus.EXPIRED.value,
        feedback=t(ERR_AUDIT_CONFIRMATION_EXPIRED, locale=locale),
    )
    await db.commit()
    await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    await db.commit()


async def expire_confirmation_by_session(db: AsyncSession, *, uid: str, session_id: str) -> int:
    record = await audit_crud.get_confirmation_claim(db, uid=uid, session_id=session_id)
    expired_count = await audit_crud.expire_confirmation_by_session(db, uid=uid, session_id=session_id)
    if expired_count and record is not None and record.id is not None:
        await sync_expired_confirmation_messages(
            db,
            audit_record_id=record.id,
            locale=record.language,
        )
    return expired_count


async def cancel_confirmation_by_session(db: AsyncSession, *, uid: str, session_id: str, locale: str | None = None) -> int:
    record = await audit_crud.get_confirmation_claim(db, uid=uid, session_id=session_id)
    if record is None or record.id is None:
        return 0
    await cancel_persisted_pending_confirmation_bundle(
        db,
        audit_record_id=record.id,
        uid=uid,
        session_id=session_id,
        feedback=t(MSG_AUDIT_CONFIRMATION_SUPERSEDED, locale=locale),
        confirmation_status="superseded",
    )
    return 1
