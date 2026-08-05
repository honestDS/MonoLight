from __future__ import annotations

import hashlib
import re
import unicodedata
from enum import StrEnum
from typing import Any

from app.core.constants import (
    ERR_MEMORY_DEDUPE_KEY_INVALID,
    ERR_MEMORY_ENUM_INVALID,
    ERR_MEMORY_FIELD_LENGTH_EXCEEDED,
    ERR_MEMORY_FIELD_REQUIRED,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_ID_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN,
    ERR_MEMORY_VERSION_INVALID,
    MEMORY_CHANGE_EVIDENCE_MAX_CHARS,
    MEMORY_CONTENT_MAX_CHARS,
    MEMORY_KEY_MAX_CHARS,
)
from app.core.memory.errors import MemoryValidationError
from app.models.memory import LongTermMemoryRecord, LongTermMemoryRevision, LongTermMemorySource, LongTermMemoryType

_MEMORY_UID_MAX_CHARS = 100
_SOURCE_ID_MAX_CHARS = 255
_SOURCE_SESSION_ID_MAX_CHARS = 100
_MEMORY_RECORD_SNAPSHOT_FIELDS = (
    "memory_key",
    "content",
    "content_hash",
    "memory_type",
    "source",
    "source_id",
    "source_session_id",
    "source_profile_id",
    "source_message_id",
    "source_job_id",
    "change_evidence",
    "version",
)


def _normalize_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str | None:
    if not isinstance(value, str):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field=field)
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())
    if not normalized:
        if required:
            raise MemoryValidationError(ERR_MEMORY_FIELD_REQUIRED, field=field)
        return None
    if len(normalized) > maximum:
        raise MemoryValidationError(ERR_MEMORY_FIELD_LENGTH_EXCEEDED, field=field, maximum=maximum)
    return normalized


def _validate_identifier(
    value: Any,
    *,
    field: str,
    maximum: int,
    required: bool = True,
    required_error: str = ERR_MEMORY_FIELD_REQUIRED,
) -> str | None:
    if not isinstance(value, str):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field=field)
    if len(value) > maximum:
        raise MemoryValidationError(ERR_MEMORY_FIELD_LENGTH_EXCEEDED, field=field, maximum=maximum)
    if not value.strip():
        if required:
            raise MemoryValidationError(required_error, field=field)
        return None
    return value


def normalize_memory_content(content: str) -> str:
    return _normalize_text(content, field="content", maximum=MEMORY_CONTENT_MAX_CHARS) or ""


def normalize_memory_key(memory_key: str) -> str:
    return _normalize_text(memory_key, field="memory_key", maximum=MEMORY_KEY_MAX_CHARS) or ""


def normalize_change_evidence(change_evidence: str | None) -> str | None:
    return (
        _normalize_text(
            change_evidence,
            field="change_evidence",
            maximum=MEMORY_CHANGE_EVIDENCE_MAX_CHARS,
            required=False,
        )
        if change_evidence is not None
        else None
    )


def build_memory_content_hash(content: str) -> str:
    normalized_content = normalize_memory_content(content)
    return hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()


def _normalize_uid(uid: str) -> str:
    normalized = _validate_identifier(uid, field="uid", maximum=_MEMORY_UID_MAX_CHARS)
    return normalized or ""


def _normalize_dedupe_key(dedupe_key: str) -> str:
    normalized = _validate_identifier(
        dedupe_key,
        field="dedupe_key",
        maximum=MEMORY_KEY_MAX_CHARS,
        required_error=ERR_MEMORY_DEDUPE_KEY_INVALID,
    )
    if normalized is None:
        raise MemoryValidationError(ERR_MEMORY_DEDUPE_KEY_INVALID)
    return normalized


def _normalize_optional_source_id(value: str | None, *, field: str, maximum: int) -> str | None:
    return _validate_identifier(value, field=field, maximum=maximum, required=False) if value is not None else None


def _normalize_enum(value: Any, enum_type: type[StrEnum], *, field: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(ERR_MEMORY_ENUM_INVALID, field=field) from exc


def _require_positive(value: Any, *, field: str, error_key: str = ERR_MEMORY_ID_INVALID) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field=field)
    if value < 1:
        raise MemoryValidationError(error_key, field=field)
    return value


def _require_non_negative(value: Any, *, field: str, error_key: str = ERR_MEMORY_VERSION_INVALID) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, field=field)
    if value < 0:
        raise MemoryValidationError(error_key, field=field)
    return value


def _validate_commit(commit: Any) -> bool:
    if not isinstance(commit, bool):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "commit"})
    return commit


def _validate_source_fields(
    *,
    source: Any,
    source_id: str | None,
    source_session_id: str | None,
    source_profile_id: int | None,
    source_message_id: int | None,
) -> tuple[LongTermMemorySource, str | None, str | None, int | None, int | None]:
    normalized_source = _normalize_enum(source, LongTermMemorySource, field="source")
    normalized_source_id = _normalize_optional_source_id(source_id, field="source_id", maximum=_SOURCE_ID_MAX_CHARS)
    normalized_source_session_id = _normalize_optional_source_id(
        source_session_id,
        field="source_session_id",
        maximum=_SOURCE_SESSION_ID_MAX_CHARS,
    )
    normalized_source_profile_id = _require_positive(source_profile_id, field="source_profile_id") if source_profile_id is not None else None
    normalized_source_message_id = _require_positive(source_message_id, field="source_message_id") if source_message_id is not None else None
    return (
        normalized_source,
        normalized_source_id,
        normalized_source_session_id,
        normalized_source_profile_id,
        normalized_source_message_id,
    )


def _publication_payload(
    *,
    content: str,
    memory_key: str,
    memory_type: Any,
    change_evidence: str | None,
    source: Any,
    source_id: str | None,
    source_session_id: str | None,
    source_profile_id: int | None,
    source_message_id: int | None,
) -> dict[str, Any]:
    normalized_source, normalized_source_id, normalized_session_id, normalized_profile_id, normalized_message_id = _validate_source_fields(
        source=source,
        source_id=source_id,
        source_session_id=source_session_id,
        source_profile_id=source_profile_id,
        source_message_id=source_message_id,
    )
    normalized_content = normalize_memory_content(content)
    normalized_key = normalize_memory_key(memory_key)
    normalized_type = _normalize_enum(memory_type, LongTermMemoryType, field="memory_type")
    normalized_evidence = normalize_change_evidence(change_evidence)
    return {
        "memory_key": normalized_key,
        "content": normalized_content,
        "content_hash": hashlib.sha256(normalized_content.encode("utf-8")).hexdigest(),
        "memory_type": normalized_type.value,
        "source": normalized_source.value,
        "source_id": normalized_source_id,
        "source_session_id": normalized_session_id,
        "source_profile_id": normalized_profile_id,
        "source_message_id": normalized_message_id,
        "change_evidence": normalized_evidence,
    }


_PUBLICATION_PAYLOAD_INPUT_FIELDS = (
    "content",
    "memory_key",
    "memory_type",
    "change_evidence",
    "source",
    "source_id",
    "source_session_id",
    "source_profile_id",
    "source_message_id",
)


def normalize_memory_publication_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a persisted memory publication payload without dropping operation fields."""
    if not isinstance(payload, dict):
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if "uid" in payload:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN)

    missing_fields = [field for field in (*_PUBLICATION_PAYLOAD_INPUT_FIELDS, "content_hash") if field not in payload]
    if missing_fields:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    rebuilt = _publication_payload(
        content=payload["content"],
        memory_key=payload["memory_key"],
        memory_type=payload["memory_type"],
        change_evidence=payload["change_evidence"],
        source=payload["source"],
        source_id=payload["source_id"],
        source_session_id=payload["source_session_id"],
        source_profile_id=payload["source_profile_id"],
        source_message_id=payload["source_message_id"],
    )
    if any(payload[field] != rebuilt[field] for field in _PUBLICATION_PAYLOAD_INPUT_FIELDS):
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if payload["content_hash"] != rebuilt["content_hash"]:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return dict(payload)


def normalize_memory_record_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate and return an independent normalized memory record snapshot."""
    if not isinstance(snapshot, dict):
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if "uid" in snapshot:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN)
    if set(snapshot) != set(_MEMORY_RECORD_SNAPSHOT_FIELDS):
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    publication = _publication_payload(
        content=snapshot["content"],
        memory_key=snapshot["memory_key"],
        memory_type=snapshot["memory_type"],
        change_evidence=snapshot["change_evidence"],
        source=snapshot["source"],
        source_id=snapshot["source_id"],
        source_session_id=snapshot["source_session_id"],
        source_profile_id=snapshot["source_profile_id"],
        source_message_id=snapshot["source_message_id"],
    )
    if snapshot["content_hash"] != publication["content_hash"]:
        raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    normalized_source_job_id = _require_positive(snapshot["source_job_id"], field="source_job_id") if snapshot["source_job_id"] is not None else None
    normalized_version = _require_positive(snapshot["version"], field="version", error_key=ERR_MEMORY_VERSION_INVALID)
    return {
        "memory_key": publication["memory_key"],
        "content": publication["content"],
        "content_hash": publication["content_hash"],
        "memory_type": publication["memory_type"],
        "source": publication["source"],
        "source_id": publication["source_id"],
        "source_session_id": publication["source_session_id"],
        "source_profile_id": publication["source_profile_id"],
        "source_message_id": publication["source_message_id"],
        "source_job_id": normalized_source_job_id,
        "change_evidence": publication["change_evidence"],
        "version": normalized_version,
    }


def build_memory_record_snapshot(record: LongTermMemoryRecord | LongTermMemoryRevision) -> dict[str, Any]:
    """Build an independent JSON snapshot from a memory record or revision."""
    if not isinstance(record, (LongTermMemoryRecord, LongTermMemoryRevision)):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "record"})
    return normalize_memory_record_snapshot(
        {
            "memory_key": record.memory_key,
            "content": record.content,
            "content_hash": record.content_hash,
            "memory_type": record.memory_type,
            "source": record.source,
            "source_id": record.source_id,
            "source_session_id": record.source_session_id,
            "source_profile_id": record.source_profile_id,
            "source_message_id": record.source_message_id,
            "source_job_id": getattr(record, "source_job_id", None),
            "change_evidence": record.change_evidence,
            "version": record.version,
        }
    )


__all__ = [
    "build_memory_content_hash",
    "build_memory_record_snapshot",
    "normalize_change_evidence",
    "normalize_memory_content",
    "normalize_memory_key",
    "normalize_memory_publication_payload",
    "normalize_memory_record_snapshot",
]
