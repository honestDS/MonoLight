from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    MEMORY_CONTENT_MAX_CHARS,
    MSG_MEMORY_CLEANUP_RETRY_SUBMITTED,
    MSG_MEMORY_CREATED,
    MSG_MEMORY_DELETED,
    MSG_MEMORY_DETAIL_SUCCESS,
    MSG_MEMORY_HISTORY_SUCCESS,
    MSG_MEMORY_JOB_CANCELLED,
    MSG_MEMORY_JOB_DETAIL_SUCCESS,
    MSG_MEMORY_JOB_LIST_SUCCESS,
    MSG_MEMORY_JOB_RETRIED,
    MSG_MEMORY_LIST_SUCCESS,
    MSG_MEMORY_MIGRATION_CANCELLED,
    MSG_MEMORY_MIGRATION_DETAIL_SUCCESS,
    MSG_MEMORY_MIGRATION_LIST_SUCCESS,
    MSG_MEMORY_MIGRATION_RETRIED,
    MSG_MEMORY_ORGANIZE_SUBMITTED,
    MSG_MEMORY_PINNED,
    MSG_MEMORY_REINDEX_SUBMITTED,
    MSG_MEMORY_RESUMED,
    MSG_MEMORY_SETTINGS_SUCCESS,
    MSG_MEMORY_SETTINGS_UPDATED,
    MSG_MEMORY_UNPINNED,
    MSG_MEMORY_UPDATED,
)
from app.core.memory import (
    cancel_embedding_migration,
    cancel_job,
    get_embedding_migration,
    get_job,
    get_memory,
    get_memory_settings,
    list_embedding_migrations,
    list_jobs,
    list_memories,
    list_memory_history,
    memory_service,
    pin_memory,
    retry_embedding_migration,
    retry_job,
    submit_memory_cleanup_retry,
    submit_memory_organization,
    submit_memory_reindex,
    unpin_memory,
    update_memory_settings,
)
from app.core.security import get_current_user
from app.models.memory import LongTermMemoryMutationOperation, LongTermMemoryMutationStatus, LongTermMemorySource, LongTermMemoryType
from app.models.user import User
from app.providers.database import get_db
from app.schemas.memory import (
    MemoryCancelResponse,
    MemoryContentTooLongErrorData,
    MemoryCreateRequest,
    MemoryDeleteRequest,
    MemoryJobResponse,
    MemoryMaintenanceRequest,
    MemoryMutationResponse,
    MemoryOrganizeRequest,
    MemoryOrganizeResponse,
    MemoryRecordDetailResponse,
    MemoryRecordResponse,
    MemoryResumeCurrentRequest,
    MemoryRevisionResponse,
    MemorySettingsResponse,
    MemorySettingsUpdateRequest,
    MemorySortField,
    MemorySortOrder,
    MemorySubmissionResponse,
    MemoryUpdateRequest,
)
from app.schemas.response import PageData, StandardResponse

router = APIRouter(
    prefix="/memories",
    tags=["Memories"],
    dependencies=[Depends(get_current_user)],
)


def _new_dedupe_key(prefix: str = "memory-api") -> str:
    return f"{prefix}:{uuid4().hex}"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "model_dump"):
        return {key: _json_value(item) for key, item in value.model_dump(exclude={"uid"}).items() if key != "uid"}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items() if key != "uid"}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _page_data(result: dict[str, Any]) -> PageData:
    skip = int(result.get("skip", 0))
    limit = int(result.get("limit", 1))
    return PageData(
        items=result.get("items", []),
        total=int(result.get("total", 0)),
        page=skip // limit + 1,
        size=limit,
    )


def _mutation_data(result: Any) -> dict[str, Any]:
    return {
        "status": _json_value(result.status),
        "job": _json_value(result.job),
        "record": _json_value(result.record),
    }


def _submission_data(result: Any) -> dict[str, Any]:
    return {"job": _json_value(result.job), "created": result.created}


@router.get("/list", response_model=StandardResponse[PageData[MemoryRecordResponse]])
async def list_memories_api(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None, max_length=MEMORY_CONTENT_MAX_CHARS),
    memory_type: LongTermMemoryType | None = Query(default=None),
    sort_by: MemorySortField = Query(default="updated_at"),
    sort_order: MemorySortOrder = Query(default="desc"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await list_memories(
        db,
        uid=current_user.uid,
        skip=(page - 1) * size,
        limit=size,
        keyword=keyword,
        memory_type=memory_type,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return StandardResponse.success(data=_page_data(result), message=MSG_MEMORY_LIST_SUCCESS)


@router.get("/get", response_model=StandardResponse[MemoryRecordDetailResponse])
async def get_memory_api(
    memory_id: int = Query(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_memory(db, uid=current_user.uid, memory_id=memory_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_DETAIL_SUCCESS)


@router.post(
    "/create",
    response_model=StandardResponse[MemoryMutationResponse],
    responses={400: {"model": StandardResponse[MemoryContentTooLongErrorData]}},
)
async def create_memory_api(
    request: MemoryCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await memory_service.create(
        db,
        uid=current_user.uid,
        dedupe_key=request.dedupe_key or _new_dedupe_key(),
        content=request.content,
        memory_key=request.memory_key,
        memory_type=request.memory_type,
        change_evidence=request.change_evidence,
        source=LongTermMemorySource.USER_API,
        max_attempts=request.max_attempts,
    )
    return StandardResponse.success(data=_mutation_data(result), message=MSG_MEMORY_CREATED)


@router.post(
    "/update",
    response_model=StandardResponse[MemoryMutationResponse],
    responses={400: {"model": StandardResponse[MemoryContentTooLongErrorData]}},
)
async def update_memory_api(
    request: MemoryUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await memory_service.update(
        db,
        uid=current_user.uid,
        dedupe_key=request.dedupe_key or _new_dedupe_key(),
        memory_id=request.memory_id,
        expected_version=request.expected_version,
        content=request.content,
        memory_key=request.memory_key,
        memory_type=request.memory_type,
        change_evidence=request.change_evidence,
        source=LongTermMemorySource.USER_API,
        suppress_current=request.suppress_current,
        max_attempts=request.max_attempts,
    )
    return StandardResponse.success(data=_mutation_data(result), message=MSG_MEMORY_UPDATED)


@router.post("/delete", response_model=StandardResponse[MemoryMutationResponse])
async def delete_memory_api(
    request: MemoryDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await memory_service.delete(
        db,
        uid=current_user.uid,
        dedupe_key=request.dedupe_key or _new_dedupe_key(),
        memory_id=request.memory_id,
        expected_version=request.expected_version,
        source=LongTermMemorySource.USER_API,
        max_attempts=request.max_attempts,
    )
    return StandardResponse.success(data=_mutation_data(result), message=MSG_MEMORY_DELETED)


@router.get("/jobs/{job_id}", response_model=StandardResponse[MemoryJobResponse])
async def get_memory_job_api(
    job_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_job(db, uid=current_user.uid, job_id=job_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_JOB_DETAIL_SUCCESS)


@router.get("/jobs", response_model=StandardResponse[PageData[MemoryJobResponse]])
async def list_memory_jobs_api(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    status: LongTermMemoryMutationStatus | None = Query(default=None),
    operation: LongTermMemoryMutationOperation | None = Query(default=None),
    memory_id: int | None = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await list_jobs(
        db,
        uid=current_user.uid,
        skip=(page - 1) * size,
        limit=size,
        status=status,
        operation=operation,
        memory_id=memory_id,
    )
    return StandardResponse.success(data=_page_data(result), message=MSG_MEMORY_JOB_LIST_SUCCESS)


@router.post("/jobs/{job_id}/retry", response_model=StandardResponse[dict[str, Any]])
async def retry_memory_job_api(
    job_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await retry_job(db, uid=current_user.uid, job_id=job_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_JOB_RETRIED)


@router.post("/jobs/{job_id}/cancel", response_model=StandardResponse[MemoryCancelResponse])
async def cancel_memory_job_api(
    job_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await cancel_job(db, uid=current_user.uid, job_id=job_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_JOB_CANCELLED)


@router.get("/settings", response_model=StandardResponse[MemorySettingsResponse])
async def get_memory_settings_api(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_memory_settings(db, uid=current_user.uid)
    return StandardResponse.success(data=result, message=MSG_MEMORY_SETTINGS_SUCCESS)


@router.post("/settings", response_model=StandardResponse[MemorySettingsResponse])
async def update_memory_settings_api(
    request: MemorySettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await update_memory_settings(
        db,
        uid=current_user.uid,
        auto_organize_enabled=request.auto_organize_enabled,
        organization_channel_id=request.organization_channel_id,
        organization_model_id=request.organization_model_id,
    )
    return StandardResponse.success(data=result, message=MSG_MEMORY_SETTINGS_UPDATED)


@router.post("/organize", response_model=StandardResponse[MemoryOrganizeResponse])
async def organize_memories_api(
    request: MemoryOrganizeRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await submit_memory_organization(
        db,
        uid=current_user.uid,
        dedupe_key=request.dedupe_key if request is not None else None,
    )
    return StandardResponse.success(data=result, message=MSG_MEMORY_ORGANIZE_SUBMITTED)


@router.post("/reindex", response_model=StandardResponse[MemorySubmissionResponse])
async def reindex_memories_api(
    request: MemoryMaintenanceRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    request = request or MemoryMaintenanceRequest()
    result = await submit_memory_reindex(
        db,
        uid=current_user.uid,
        dedupe_key=request.dedupe_key or _new_dedupe_key("memory-reindex"),
        max_attempts=request.max_attempts,
    )
    return StandardResponse.success(data=_submission_data(result), message=MSG_MEMORY_REINDEX_SUBMITTED)


@router.get("/embedding-migrations", response_model=StandardResponse[PageData[dict[str, Any]]])
async def list_memory_embedding_migrations_api(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await list_embedding_migrations(
        db,
        uid=current_user.uid,
        skip=(page - 1) * size,
        limit=size,
    )
    return StandardResponse.success(data=_page_data(result), message=MSG_MEMORY_MIGRATION_LIST_SUCCESS)


@router.get("/embedding-migrations/{job_id}", response_model=StandardResponse[dict[str, Any]])
async def get_memory_embedding_migration_api(
    job_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await get_embedding_migration(db, uid=current_user.uid, migration_id=job_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_MIGRATION_DETAIL_SUCCESS)


@router.post("/embedding-migrations/{job_id}/retry", response_model=StandardResponse[dict[str, Any]])
async def retry_memory_embedding_migration_api(
    job_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await retry_embedding_migration(db, uid=current_user.uid, migration_id=job_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_MIGRATION_RETRIED)


@router.post("/embedding-migrations/{job_id}/cancel", response_model=StandardResponse[MemoryCancelResponse])
async def cancel_memory_embedding_migration_api(
    job_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await cancel_embedding_migration(db, uid=current_user.uid, migration_id=job_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_MIGRATION_CANCELLED)


@router.post("/collections/{job_id}/cleanup-retry", response_model=StandardResponse[MemorySubmissionResponse])
async def retry_memory_collection_cleanup_api(
    job_id: int = Path(..., ge=1),
    request: MemoryMaintenanceRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    request = request or MemoryMaintenanceRequest()
    result = await submit_memory_cleanup_retry(
        db,
        uid=current_user.uid,
        dedupe_key=request.dedupe_key or _new_dedupe_key(f"memory-cleanup-retry-{job_id}"),
        max_attempts=request.max_attempts,
    )
    return StandardResponse.success(data=_submission_data(result), message=MSG_MEMORY_CLEANUP_RETRY_SUBMITTED)


@router.get("/{memory_id}/history", response_model=StandardResponse[PageData[MemoryRevisionResponse]])
async def list_memory_history_api(
    memory_id: int = Path(..., ge=1),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await list_memory_history(
        db,
        uid=current_user.uid,
        memory_id=memory_id,
        skip=(page - 1) * size,
        limit=size,
    )
    data = _page_data(result)
    return StandardResponse.success(data=data, message=MSG_MEMORY_HISTORY_SUCCESS)


@router.post("/{memory_id}/resume-current", response_model=StandardResponse[MemoryMutationResponse])
async def resume_current_memory_api(
    request: MemoryResumeCurrentRequest,
    memory_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await memory_service.resume_current(
        db,
        uid=current_user.uid,
        memory_id=memory_id,
        expected_version=request.expected_version,
    )
    return StandardResponse.success(data=_mutation_data(result), message=MSG_MEMORY_RESUMED)


@router.post("/{memory_id}/pin", response_model=StandardResponse[MemoryRecordResponse])
async def pin_memory_api(
    memory_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await pin_memory(db, uid=current_user.uid, memory_id=memory_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_PINNED)


@router.post("/{memory_id}/unpin", response_model=StandardResponse[MemoryRecordResponse])
async def unpin_memory_api(
    memory_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await unpin_memory(db, uid=current_user.uid, memory_id=memory_id)
    return StandardResponse.success(data=result, message=MSG_MEMORY_UNPINNED)


__all__ = ["router"]
