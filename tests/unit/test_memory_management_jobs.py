from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from tests.unit.memory_test_support import (
    MEMORY_TABLES,
    claim_job,
    configure_store,
    create_recallable_record,
)


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    import app.core.memory.management as memory_management_module
    from app.core.constants import (
        ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    )
    from app.core.crud.memory.job import memory_job_crud
    from app.core.crud.memory.store import (
        memory_record_crud,
    )
    from app.core.memory import (
        MemoryConflictError,
        MemoryNotFoundError,
        list_jobs,
        memory_service,
        retry_job,
    )
    from app.core.memory.management_helpers import _job_view
    from app.core.memory.normalization import build_memory_content_hash
    from app.core.memory_jobs.manager import MemoryJobValidationError, memory_job_manager
    from app.core.memory_jobs.manager_common import MemoryJobSubmissionResult
    from app.models.memory import (
        LongTermMemoryMutationJob,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryRecord,
        LongTermMemorySource,
        LongTermMemoryType,
    )


pytest_plugins = ("tests.unit.memory_fixture",)


@pytest_asyncio.fixture
async def memory_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=MEMORY_TABLES,
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _create_raw_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    operation: LongTermMemoryMutationOperation,
    dedupe_key: str,
    status: LongTermMemoryMutationStatus = LongTermMemoryMutationStatus.PENDING,
    memory_id: int | None = None,
    expected_version: int | None = None,
    payload: dict[str, Any] | None = None,
) -> LongTermMemoryMutationJob:
    async with session_factory() as db:
        job, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=operation,
            dedupe_key=dedupe_key,
            status=status,
            memory_id=memory_id,
            expected_version=expected_version,
            payload=payload or {},
        )
        assert created
        return job


async def _fail_claimed_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: LongTermMemoryMutationJob,
    *,
    owner: str,
    error: str,
    result: dict[str, Any] | None = None,
) -> None:
    assert claimed.id is not None
    async with session_factory() as db:
        changed = await memory_job_crud.mark_failed(
            db,
            uid=claimed.uid,
            job_id=claimed.id,
            owner=owner,
            error=error,
            result=result,
            commit=False,
        )
        assert changed
        await db.commit()


async def _create_failed_update_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_key: str,
) -> tuple[LongTermMemoryRecord, LongTermMemoryMutationJob]:
    record = await create_recallable_record(
        session_factory,
        uid=uid,
        memory_key=memory_key,
        content=f"before-{memory_key}",
        version=1,
    )
    async with session_factory() as db:
        result = await memory_service.update(
            db,
            uid=uid,
            dedupe_key=f"update-{memory_key}",
            memory_id=record.id,
            expected_version=1,
            content=f"after-{memory_key}",
            memory_key=f"{memory_key}-updated",
            memory_type=LongTermMemoryType.FACT,
            change_evidence="memory_management_jobs retry",
            source=LongTermMemorySource.USER_API,
            source_id="memory_management_jobs-source",
        )
    assert result.job is not None
    assert result.job.id is not None
    owner = f"memory_management_jobs-failed-{memory_key}"
    claimed = await claim_job(
        session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.UPDATE,
        job_id=result.job.id,
        owner=owner,
    )
    assert claimed is not None
    await _fail_claimed_job(
        session_factory,
        claimed,
        owner=owner,
        error="publication failed",
    )
    async with session_factory() as db:
        failed = await memory_job_crud.get_by_id(db, uid=uid, job_id=result.job.id)
    assert failed is not None
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    return record, failed


@pytest.mark.asyncio
async def test_failed_publication_retry_requeues_and_enforces_version_and_uid(
    memory_session_factory,
) -> None:
    uid = "memory_management_jobs-publication-owner"
    await configure_store(memory_session_factory, uid=uid)
    record, failed = await _create_failed_update_job(
        memory_session_factory,
        uid=uid,
        memory_key="retry-success",
    )
    assert failed.id is not None

    async with memory_session_factory() as db:
        retried = await retry_job(db, uid=uid, job_id=failed.id)

    retry_view = retried["job"]
    assert retried["status"] == "accepted"
    assert retry_view["status"] == LongTermMemoryMutationStatus.PENDING.value
    assert retry_view["memory_id"] == record.id
    assert retry_view["expected_version"] == 1
    assert retry_view["payload"]["content"] == "after-retry-success"

    async with memory_session_factory() as db:
        retry_record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=record.id)
        persisted_retry_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=retry_view["id"])
    assert retry_record is not None
    assert retry_record.pending_mutation_job_id == retry_view["id"]
    assert persisted_retry_job is not None
    assert persisted_retry_job.uid == uid

    async with memory_session_factory() as db:
        with pytest.raises(MemoryNotFoundError):
            await retry_job(db, uid="memory_management_jobs-publication-other", job_id=failed.id)

    stale_record, stale_failed = await _create_failed_update_job(
        memory_session_factory,
        uid=uid,
        memory_key="retry-stale",
    )
    assert stale_failed.id is not None
    async with memory_session_factory() as db:
        changed = await db.execute(update(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == stale_record.id).values(version=2))
        assert changed.rowcount == 1
        await db.commit()
    async with memory_session_factory() as db:
        with pytest.raises(MemoryConflictError):
            await retry_job(db, uid=uid, job_id=stale_failed.id)


@pytest.mark.asyncio
async def test_retry_rejects_nonterminal_job(memory_session_factory) -> None:
    pending = await _create_raw_job(
        memory_session_factory,
        uid="memory-management-retry-pending",
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="pending-not-retryable",
        status=LongTermMemoryMutationStatus.PENDING,
    )
    assert pending.id is not None

    async with memory_session_factory() as db:
        with pytest.raises(MemoryConflictError) as exc_info:
            await retry_job(db, uid=pending.uid, job_id=pending.id)

    assert exc_info.value.message == ERR_MEMORY_JOB_TARGET_STATE_CONFLICT


@pytest.mark.asyncio
async def test_restore_jobs_remain_queryable_but_are_not_retryable(memory_session_factory) -> None:
    uid = "memory_management_jobs-restore-compatibility"
    failed = await _create_raw_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.RESTORE,
        dedupe_key="legacy-restore",
        status=LongTermMemoryMutationStatus.FAILED,
        memory_id=42,
        expected_version=2,
        payload={"restored_from_version": 1},
    )
    assert failed.id is not None

    async with memory_session_factory() as db:
        jobs = await list_jobs(db, uid=uid, operation=LongTermMemoryMutationOperation.RESTORE)
        with pytest.raises(MemoryConflictError) as exc_info:
            await retry_job(db, uid=uid, job_id=failed.id)

    assert jobs["total"] == 1
    assert jobs["items"][0]["operation"] == LongTermMemoryMutationOperation.RESTORE.value
    assert exc_info.value.message == ERR_MEMORY_JOB_TARGET_STATE_CONFLICT


@pytest.mark.asyncio
async def test_new_restore_jobs_are_rejected_by_the_submission_manager(memory_session_factory) -> None:
    uid = "memory_management_jobs-restore-submission-disabled"
    await configure_store(memory_session_factory, uid=uid)
    async with memory_session_factory() as db:
        with pytest.raises(MemoryJobValidationError):
            await memory_job_manager.submit(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.RESTORE,
                dedupe_key="new-restore",
                payload={"restored_from_version": 1},
                active_mutation_key="restore-target",
                memory_id=1,
                expected_version=1,
            )


@pytest.mark.asyncio
async def test_retry_failed_replacement_routes_back_through_normal_create_publication(
    memory_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "memory-management-replacement-retry"
    content = "replacement retry content"
    publication = {
        "content": content,
        "memory_key": "replacement-retry-key",
        "content_hash": build_memory_content_hash(content),
        "memory_type": LongTermMemoryType.FACT.value,
        "change_evidence": None,
        "source": LongTermMemorySource.USER_API.value,
        "source_id": None,
        "source_session_id": None,
        "source_profile_id": None,
        "source_message_id": None,
    }
    failed = await _create_raw_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
        dedupe_key="failed-replacement",
        status=LongTermMemoryMutationStatus.FAILED,
        payload={
            "publication": publication,
            "candidate": {"memory_id": 99, "version": 1, "vector_item_id": "old-vector"},
        },
    )
    assert failed.id is not None
    captured: dict[str, Any] = {}

    async def fake_create(_db: AsyncSession, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        retry_job = LongTermMemoryMutationJob(
            id=777,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE,
            dedupe_key=str(kwargs["dedupe_key"]),
            status=LongTermMemoryMutationStatus.PENDING,
            payload=publication,
            max_attempts=int(kwargs["max_attempts"]),
        )
        return SimpleNamespace(status="accepted", job=retry_job, record=None)

    monkeypatch.setattr(memory_management_module.memory_service, "create", fake_create)

    async with memory_session_factory() as db:
        result = await retry_job(db, uid=uid, job_id=failed.id)

    assert result["status"] == "accepted"
    assert result["job"]["operation"] == LongTermMemoryMutationOperation.CREATE.value
    assert result["job"]["id"] == 777
    assert captured["content"] == content
    assert captured["memory_key"] == "replacement-retry-key"
    assert captured["memory_type"] == LongTermMemoryType.FACT
    assert captured["source"] == LongTermMemorySource.USER_API
    assert captured["dedupe_key"] != failed.dedupe_key
    assert captured["commit"] is False


@pytest.mark.asyncio
async def test_retry_failed_organization_chain_always_submits_a_new_organization_snapshot(
    memory_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "memory-management-organization-retry"
    failed = await _create_raw_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        dedupe_key="failed-organization-merge",
        status=LongTermMemoryMutationStatus.FAILED,
        payload={"parent_job_id": 21, "sources": []},
    )
    assert failed.id is not None
    captured: dict[str, Any] = {}

    async def fake_submit_organization(
        _db: AsyncSession,
        *,
        uid: str,
        dedupe_key: str | None = None,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult:
        captured.update(uid=uid, dedupe_key=dedupe_key, commit=commit)
        return MemoryJobSubmissionResult(
            job=LongTermMemoryMutationJob(
                id=778,
                uid=uid,
                operation=LongTermMemoryMutationOperation.ORGANIZE,
                dedupe_key=str(dedupe_key),
                status=LongTermMemoryMutationStatus.PENDING,
                payload={
                    "snapshot": {
                        "count": 1,
                        "items": [{"memory_id": 5, "version": 2}],
                    }
                },
            ),
            created=True,
        )

    monkeypatch.setattr(
        memory_management_module.memory_job_manager,
        "submit_organization",
        fake_submit_organization,
    )

    async with memory_session_factory() as db:
        result = await retry_job(db, uid=uid, job_id=failed.id)

    assert result["status"] == "accepted"
    assert result["retry_scope"] == "new_snapshot"
    assert result["created"] is True
    assert result["job"]["operation"] == LongTermMemoryMutationOperation.ORGANIZE.value
    assert result["job"]["payload"]["snapshot"]["count"] == 1
    assert captured["uid"] == uid
    assert captured["dedupe_key"] != failed.dedupe_key
    assert captured["commit"] is False


def test_job_view_derives_organization_summary_and_redacts_model_secrets() -> None:
    parent = LongTermMemoryMutationJob(
        id=901,
        uid="summary-owner",
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key="summary-parent",
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        payload={
            "snapshot": {"count": 2},
            "organization_model": {
                "channel_id": 7,
                "channel_name": "organization",
                "model_id": "organizer",
                "usage": "CHAT",
                "protocol": "OPENAI",
                "context_window_tokens": 4096,
                "max_tokens": 512,
                "required_output_tokens": 256,
                "base_url": "https://secret.invalid",
                "api_key": "secret-key",
                "http_proxy": "http://secret.invalid",
                "custom_headers": {"authorization": "secret"},
            },
        },
        result={
            "snapshot_count": 2,
            "keep_count": 1,
            "update_count": 1,
            "merge_count": 0,
            "conflict_count": 0,
            "stale_count": 0,
            "skipped_count": 1,
            "child_job_ids": [902, 903],
            "budget": {"required_input_tokens": 300, "available_input_tokens": 500},
        },
    )

    parent_view = _job_view(parent)

    assert parent_view is not None
    assert parent_view["parent_job_id"] is None
    assert parent_view["snapshot_count"] == 2
    assert parent_view["keep_count"] == 1
    assert parent_view["update_count"] == 1
    assert parent_view["merge_count"] == 0
    assert parent_view["child_job_ids"] == [902, 903]
    assert parent_view["token_budget"] == {
        "required_input_tokens": 300,
        "available_input_tokens": 500,
    }
    public_model = parent_view["payload"]["organization_model"]
    assert public_model["channel_id"] == 7
    assert public_model["model_id"] == "organizer"
    for secret_field in ("base_url", "api_key", "http_proxy", "custom_headers"):
        assert secret_field not in public_model

    merge = LongTermMemoryMutationJob(
        id=902,
        uid="summary-owner",
        parent_job_id=901,
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        dedupe_key="summary-merge",
        status=LongTermMemoryMutationStatus.FAILED,
        payload={
            "parent_job_id": 901,
            "action": "merge",
            "sources": [
                {"memory_id": 7, "expected_version": 1},
                {"memory_id": 8, "expected_version": 2},
            ],
        },
        result={"action": "merge"},
    )
    merge_view = _job_view(merge)
    assert merge_view is not None
    assert merge_view["parent_job_id"] == 901
    assert merge_view["snapshot_count"] == 2
    assert merge_view["keep_count"] == 0
    assert merge_view["update_count"] == 0
    assert merge_view["merge_count"] == 1
    assert merge_view["conflict_count"] == 0


def test_job_view_does_not_infer_merge_summary_from_invalid_action_or_sources() -> None:
    payloads = (
        {"sources": [{"memory_id": 1, "expected_version": 1}]},
        {"action": "merge", "sources": []},
        {"action": "merge", "sources": [{"memory_id": 1, "expected_version": 0}]},
    )
    views = []
    for index, payload in enumerate(payloads, start=1):
        view = _job_view(
            LongTermMemoryMutationJob(
                id=950 + index,
                uid="summary-owner",
                operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
                dedupe_key=f"invalid-summary-{index}",
                status=LongTermMemoryMutationStatus.FAILED,
                payload=payload,
                result={},
            )
        )
        assert view is not None
        views.append(view)

    missing_action, empty_sources, invalid_sources = views
    assert missing_action["snapshot_count"] == 1
    for key in ("keep_count", "update_count", "merge_count", "conflict_count"):
        assert missing_action[key] is None
    assert empty_sources["snapshot_count"] is None
    assert invalid_sources["snapshot_count"] is None
