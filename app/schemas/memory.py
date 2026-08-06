from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app.core.constants import (
    MEMORY_CHANGE_EVIDENCE_MAX_CHARS,
    MEMORY_CONTENT_MAX_CHARS,
    MEMORY_KEY_MAX_CHARS,
)
from app.core.memory.results import MemoryMutationStatus
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecordIndexStatus,
    LongTermMemorySource,
    LongTermMemoryType,
)


class _MemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryCreateRequest(_MemoryRequest):
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    content: StrictStr = Field(min_length=1, max_length=MEMORY_CONTENT_MAX_CHARS)
    memory_key: StrictStr = Field(min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    memory_type: LongTermMemoryType
    change_evidence: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_CHANGE_EVIDENCE_MAX_CHARS)
    max_attempts: StrictInt = Field(default=3, ge=1)


class MemoryUpdateRequest(_MemoryRequest):
    memory_id: StrictInt = Field(gt=0)
    expected_version: StrictInt = Field(ge=0)
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    content: StrictStr = Field(min_length=1, max_length=MEMORY_CONTENT_MAX_CHARS)
    memory_key: StrictStr = Field(min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    memory_type: LongTermMemoryType
    change_evidence: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_CHANGE_EVIDENCE_MAX_CHARS)
    suppress_current: StrictBool = False
    max_attempts: StrictInt = Field(default=3, ge=1)


class MemoryDeleteRequest(_MemoryRequest):
    memory_id: StrictInt = Field(gt=0)
    expected_version: StrictInt | None = Field(default=None, ge=0)
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    max_attempts: StrictInt = Field(default=3, ge=1)


class MemoryRestoreRequest(_MemoryRequest):
    revision_version: StrictInt = Field(gt=0)
    expected_version: StrictInt = Field(ge=0)
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    max_attempts: StrictInt = Field(default=3, ge=1)


class MemoryResumeCurrentRequest(_MemoryRequest):
    expected_version: StrictInt = Field(ge=0)


class MemoryMaintenanceRequest(_MemoryRequest):
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=MEMORY_KEY_MAX_CHARS)
    max_attempts: StrictInt = Field(default=3, ge=1)


class MemoryRecordResponse(BaseModel):
    id: int
    memory_key: str | None = None
    memory_type: LongTermMemoryType
    content: str
    content_token_count: int
    content_hash: str | None = None
    version: int
    indexed_version: int
    vector_item_id: str | None = None
    source: LongTermMemorySource
    source_id: str | None = None
    source_session_id: str | None = None
    source_profile_id: int | None = None
    source_message_id: int | None = None
    source_job_id: int | None = None
    change_evidence: str | None = None
    is_active: bool
    pinned: bool
    last_recalled_at: datetime | None = None
    pending_mutation_job_id: int | None = None
    suppress_recall: bool
    suppressed_by_job_id: int | None = None
    index_status: LongTermMemoryRecordIndexStatus
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None = None
    deleted_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class MemoryRevisionResponse(BaseModel):
    id: int
    memory_id: int
    version: int
    memory_key: str
    memory_type: LongTermMemoryType
    content: str
    content_token_count: int
    content_hash: str | None = None
    source: LongTermMemorySource
    source_id: str | None = None
    source_session_id: str | None = None
    source_profile_id: int | None = None
    source_message_id: int | None = None
    source_job_id: int | None = None
    change_evidence: str | None = None
    published_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class MemoryJobResponse(BaseModel):
    id: int
    operation: LongTermMemoryMutationOperation
    dedupe_key: str
    active_mutation_key: str | None = None
    status: LongTermMemoryMutationStatus
    memory_id: int | None = None
    expected_version: int | None = None
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    source_session_id: str | None = None
    source_profile_id: int | None = None
    source_message_id: int | None = None
    available_at: datetime
    attempt_count: int
    max_attempts: int
    locked_by: str | None = None
    lock_until: datetime | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class MemoryMutationResponse(BaseModel):
    status: MemoryMutationStatus
    job: MemoryJobResponse | None = None
    record: MemoryRecordResponse | None = None


class MemorySubmissionResponse(BaseModel):
    job: MemoryJobResponse
    created: bool


class MemoryCancelResponse(BaseModel):
    job: MemoryJobResponse | None = None
    accepted: bool
    changed: bool
    error: str | None = None


class MemoryContentTooLongErrorData(BaseModel):
    status: Literal["content_too_long"] = "content_too_long"
    actual_tokens: int
    max_tokens: int
    retryable: Literal[True] = True


class MemoryCapacitySettings(BaseModel):
    max_active_records: int
    organize_trigger_records: int
    content_max_tokens: int
    active_record_count: int
    status: LongTermMemoryCapacityStatus


class MemorySettingsResponse(BaseModel):
    configured: bool
    active: dict[str, Any]
    target: dict[str, Any]
    migration: dict[str, Any]
    delta: dict[str, Any]
    index: dict[str, Any]
    capacity: MemoryCapacitySettings
    old_collection_cleanup: dict[str, Any]
    migration_job: MemoryJobResponse | None = None
    store: dict[str, Any]


MemorySortField = Literal["updated_at", "created_at", "version"]
MemorySortOrder = Literal["asc", "desc"]


__all__ = [
    "MemoryCapacitySettings",
    "MemoryCancelResponse",
    "MemoryContentTooLongErrorData",
    "MemoryCreateRequest",
    "MemoryDeleteRequest",
    "MemoryJobResponse",
    "MemoryMaintenanceRequest",
    "MemoryMutationResponse",
    "MemoryRecordResponse",
    "MemoryRestoreRequest",
    "MemoryResumeCurrentRequest",
    "MemoryRevisionResponse",
    "MemorySortField",
    "MemorySortOrder",
    "MemorySettingsResponse",
    "MemorySubmissionResponse",
    "MemoryUpdateRequest",
]
