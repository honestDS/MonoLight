from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.core.crypto as crypto_module
import app.core.memory_jobs.consumer as consumer_module
import app.core.memory_jobs.manager as manager_module
from app.core.constants import ERR_MEMORY_JOB_PAYLOAD_INVALID, MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS
from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_record_crud, memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.exceptions import ParameterException
from app.core.memory.normalization import build_memory_content_hash
from app.core.memory.organization import (
    build_organization_dedupe_key,
    restore_organization_execution_payload,
    update_organization_settings,
)
from app.core.memory_jobs.consumer import MemoryJobConsumer
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutionResult,
    MemoryJobExecutor,
    MemoryJobRetryableError,
)
from app.core.memory_jobs.manager import (
    MemoryJobManager,
    best_effort_submit_auto_organization_after_publication,
)
from app.core.utils.tokenizer import estimate_tokens
from app.models.channel import ChannelCreate, ModelChannel
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time

ORGANIZATION_TABLES = [
    ModelChannel.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryMutationJob.__table__,
]


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)


@pytest_asyncio.fixture
async def memory_database(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = (tmp_path / "memory-organization-auto-trigger-stage9.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=ORGANIZATION_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _chat_model(model_id: str = "organization-model", max_tokens: int = 20_000) -> dict[str, object]:
    return {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 64,
        "max_tokens": max_tokens,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
        "description": "stage9 auto organization model",
        "advanced_settings": {"custom_headers": {"x-stage": "stage9"}},
    }


async def _create_channel(
    db: AsyncSession,
    *,
    model_id: str = "organization-model",
    max_tokens: int = 20_000,
    api_key: str = "organization-api-key",
) -> ModelChannel:
    return await channel_crud.create_with_plain_api_key(
        db,
        obj_in=ChannelCreate(
            name=f"organization-channel-{uuid4().hex[:8]}",
            api_key=api_key,
            base_url="https://llm.example/v1",
            http_proxy="http://proxy.example:8080",
            is_active=True,
            model_ids=[_chat_model(model_id=model_id, max_tokens=max_tokens)],
        ),
    )


async def _create_store(
    db: AsyncSession,
    *,
    uid: str,
    channel_id: int | None,
    model_id: str | None,
    auto_organize_enabled: bool = True,
    **overrides: object,
) -> LongTermMemoryStore:
    values: dict[str, object] = {
        "active_embedding_channel_id": 7,
        "active_embedding_model_id": "embedding-model",
        "active_embedding_dimensions": 3,
        "active_embedding_signature": "embedding-signature",
        "active_embedding_revision": 3,
        "active_collection_name": f"memory-{uid}",
        "index_revision": 4,
        "index_status": LongTermMemoryIndexStatus.READY,
        "migration_status": None,
        "organization_channel_id": channel_id,
        "organization_model_id": model_id,
        "auto_organize_enabled": auto_organize_enabled,
    }
    values.update(overrides)
    return await memory_store_crud.create(db, uid=uid, **values)


async def _create_record(
    db: AsyncSession,
    *,
    uid: str,
    index: int,
    content: str | None = None,
) -> LongTermMemoryRecord:
    memory_key = f"memory-{index:03d}"
    content = content or f"memory content {index}"
    return await memory_record_crud.create(
        db,
        uid=uid,
        memory_key=memory_key,
        content=content,
        content_token_count=estimate_tokens(content),
        content_hash=build_memory_content_hash(content),
        version=1,
        indexed_version=1,
        vector_item_id=f"vector-{uid}-{index}",
        is_active=True,
        index_status=LongTermMemoryRecordIndexStatus.READY,
        commit=False,
    )


async def _seed_records(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    count: int,
    content_prefix: str = "memory content",
) -> list[LongTermMemoryRecord]:
    async with session_factory() as db:
        records = [
            await _create_record(
                db,
                uid=uid,
                index=index,
                content=f"{content_prefix} {index}",
            )
            for index in range(count)
        ]
        await db.commit()
    return records


async def _setup_auto_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    record_count: int,
    auto_organize_enabled: bool = True,
    model_id: str | None = "organization-model",
    **store_overrides: object,
) -> tuple[LongTermMemoryStore, list[LongTermMemoryRecord]]:
    async with session_factory() as db:
        channel = await _create_channel(db)
        assert channel.id is not None
        store = await _create_store(
            db,
            uid=uid,
            channel_id=channel.id,
            model_id=model_id,
            auto_organize_enabled=auto_organize_enabled,
            **store_overrides,
        )
    records = await _seed_records(session_factory, uid=uid, count=record_count)
    return store, records


async def _get_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
) -> LongTermMemoryStore:
    async with session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert store is not None
    return store


async def _count_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    operation: LongTermMemoryMutationOperation | None = None,
) -> int:
    async with session_factory() as db:
        return await memory_job_crud.count(db, uid=uid, operation=operation)


async def _create_direct_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    operation: LongTermMemoryMutationOperation,
    dedupe_key: str,
) -> LongTermMemoryMutationJob:
    async with session_factory() as db:
        available_at = await get_database_time(db)
        job, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=operation,
            dedupe_key=dedupe_key,
            payload={},
            available_at=available_at,
        )
    assert created
    assert job.id is not None
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("record_count", [45, 50])
async def test_auto_organization_submits_complete_snapshot_and_updates_store(
    memory_database: async_sessionmaker[AsyncSession],
    record_count: int,
) -> None:
    uid = f"auto-threshold-{record_count}"
    store, records = await _setup_auto_user(memory_database, uid=uid, record_count=record_count)

    async with memory_database() as db:
        submission = await MemoryJobManager().submit_auto_organization(db, uid=uid, commit=False)
        assert submission is not None
        same_transaction_store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert same_transaction_store is not None
        assert same_transaction_store.organization_last_job_id == submission.job.id
        assert same_transaction_store.organization_last_run_at is not None
        assert same_transaction_store.organization_error is None
        await db.commit()

    assert submission is not None
    assert submission.created
    assert submission.job.id is not None
    assert submission.job.operation == LongTermMemoryMutationOperation.ORGANIZE
    assert submission.job.payload["trigger"] == "auto"
    snapshot = submission.job.payload["snapshot"]
    assert snapshot["count"] == record_count
    assert {item["memory_id"] for item in snapshot["items"]} == {record.id for record in records}
    assert submission.job.dedupe_key == build_organization_dedupe_key(
        uid,
        snapshot_digest=snapshot["digest"],
        policy_version=store.organization_policy_version,
        caller_dedupe_key=None,
    )

    persisted_store = await _get_store(memory_database, uid=uid)
    assert persisted_store.organization_last_job_id == submission.job.id
    assert persisted_store.organization_last_run_at is not None
    assert persisted_store.organization_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("record_count", [45, 50])
async def test_enabling_auto_organization_submits_snapshot_and_updates_store(
    memory_database: async_sessionmaker[AsyncSession],
    record_count: int,
) -> None:
    uid = f"auto-settings-threshold-{record_count}"
    store, _ = await _setup_auto_user(
        memory_database,
        uid=uid,
        record_count=record_count,
        auto_organize_enabled=False,
    )

    async with memory_database() as db:
        settings = await update_organization_settings(
            db,
            uid=uid,
            auto_organize_enabled=True,
            organization_channel_id=store.organization_channel_id,
            organization_model_id=store.organization_model_id,
            commit=False,
        )
        jobs = await memory_job_crud.list_by_uid(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        assert len(jobs) == 1
        job = jobs[0]
        assert job.payload["trigger"] == "auto"
        assert job.payload["snapshot"]["count"] == record_count
        assert settings["last_job_id"] == job.id
        await db.commit()

    assert (
        await _count_jobs(
            memory_database,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "below-threshold", "interval", "reindex", "migration", "active-job"])
async def test_auto_organization_quietly_skips_without_new_job_or_store_changes(
    memory_database: async_sessionmaker[AsyncSession],
    case: str,
) -> None:
    uid = f"auto-skip-{case}"
    baseline = datetime(2025, 1, 1, tzinfo=UTC)
    _, _ = await _setup_auto_user(
        memory_database,
        uid=uid,
        record_count=44 if case == "below-threshold" else 45,
        auto_organize_enabled=case != "disabled",
        organization_last_job_id=901,
        organization_last_run_at=(datetime.now(UTC) if case == "interval" else baseline),
        organization_error="preserve-this-error",
        index_status=(LongTermMemoryIndexStatus.REINDEXING if case == "reindex" else LongTermMemoryIndexStatus.READY),
        migration_status=(LongTermMemoryMigrationStatus.BUILDING if case == "migration" else None),
    )

    if case == "active-job":
        async with memory_database() as db:
            active = await MemoryJobManager().submit_organization(db, uid=uid)
        assert active.created

    before_store = await _get_store(memory_database, uid=uid)
    before = (
        before_store.organization_last_job_id,
        before_store.organization_last_run_at,
        before_store.organization_error,
    )
    before_count = await _count_jobs(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
    )

    async with memory_database() as db:
        submission = await MemoryJobManager().submit_auto_organization(db, uid=uid)

    assert submission is None
    after_store = await _get_store(memory_database, uid=uid)
    assert (
        after_store.organization_last_job_id,
        after_store.organization_last_run_at,
        after_store.organization_error,
    ) == before
    assert (
        await _count_jobs(
            memory_database,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == before_count
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_seconds", [0, 1])
@pytest.mark.parametrize("last_run_timezone", [None, UTC])
async def test_auto_organization_accepts_exact_and_overdue_interval_for_naive_and_aware_times(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    extra_seconds: int,
    last_run_timezone: Any,
) -> None:
    uid = f"auto-interval-{last_run_timezone}-{extra_seconds}"
    fixed_now = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
    last_run_at = fixed_now - timedelta(seconds=MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS + extra_seconds)
    if last_run_timezone is None:
        fixed_now = fixed_now.replace(tzinfo=None)
        last_run_at = last_run_at.replace(tzinfo=None)
    await _setup_auto_user(
        memory_database,
        uid=uid,
        record_count=45,
        organization_last_run_at=last_run_at,
    )

    async def fixed_database_time(_db: AsyncSession) -> datetime:
        return fixed_now

    original_lock = manager_module.memory_store_crud.lock_for_mutation

    async def lock_with_requested_timezone(
        db: AsyncSession,
        *,
        uid: str,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        store = await original_lock(db, uid=uid, commit=commit)
        if store is not None:
            store.organization_last_run_at = last_run_at
        return store

    monkeypatch.setattr(manager_module, "get_database_time", fixed_database_time)
    monkeypatch.setattr(manager_module.memory_store_crud, "lock_for_mutation", lock_with_requested_timezone)

    async with memory_database() as db:
        submission = await MemoryJobManager().submit_auto_organization(db, uid=uid)

    assert submission is not None
    assert submission.created


@pytest.mark.asyncio
async def test_auto_dedupe_reuses_succeeded_snapshot_manual_ignores_interval_and_old_payload_survives_model_change(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "auto-dedupe-boundaries"
    fixed_now = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)
    old_run = fixed_now - timedelta(seconds=MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS)
    await _setup_auto_user(memory_database, uid=uid, record_count=45)

    async def fixed_database_time(_db: AsyncSession) -> datetime:
        return fixed_now

    monkeypatch.setattr(manager_module, "get_database_time", fixed_database_time)
    manager = MemoryJobManager()
    async with memory_database() as db:
        first = await manager.submit_auto_organization(db, uid=uid)
    assert first is not None
    assert first.job.id is not None

    async with memory_database() as db:
        finished = await memory_job_crud.update_status(
            db,
            uid=uid,
            job_id=first.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            clear_active_mutation_key=True,
        )
        assert finished is not None
        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            organization_last_run_at=old_run,
            commit=False,
        )
        assert updated_store is not None
        await db.commit()

    async with memory_database() as db:
        second = await manager.submit_auto_organization(db, uid=uid)
    assert second is not None
    assert not second.created
    assert second.job.id == first.job.id
    assert (
        await _count_jobs(
            memory_database,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == 1
    )

    async with memory_database() as db:
        manual_with_caller_key = await manager.submit_organization(
            db,
            uid=uid,
            dedupe_key="manual-after-auto",
        )
    assert manual_with_caller_key.created
    assert manual_with_caller_key.job.payload["trigger"] == "manual"

    assert manual_with_caller_key.job.id is not None
    async with memory_database() as db:
        finished_manual = await memory_job_crud.update_status(
            db,
            uid=uid,
            job_id=manual_with_caller_key.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            clear_active_mutation_key=True,
        )
        assert finished_manual is not None
        changed_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            organization_model_id="changed-after-auto",
            commit=False,
        )
        assert changed_store is not None
        await db.commit()

    async with memory_database() as db:
        manual_without_caller_key = await manager.submit_organization(db, uid=uid)
    assert not manual_without_caller_key.created
    assert manual_without_caller_key.job.id == first.job.id
    assert (
        await _count_jobs(
            memory_database,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == 2
    )


@pytest.mark.asyncio
async def test_auto_organization_retries_failed_snapshot_after_interval(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "auto-failed-retry"
    fixed_now = datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC)
    old_run = fixed_now - timedelta(seconds=MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS)
    _, records = await _setup_auto_user(memory_database, uid=uid, record_count=45)

    async def fixed_database_time(_db: AsyncSession) -> datetime:
        return fixed_now

    monkeypatch.setattr(manager_module, "get_database_time", fixed_database_time)
    manager = MemoryJobManager()

    async with memory_database() as db:
        first = await manager.submit_auto_organization(db, uid=uid)
    assert first is not None
    assert first.created
    assert first.job.id is not None
    first_job_id = first.job.id
    first_dedupe_key = first.job.dedupe_key
    first_snapshot = first.job.payload["snapshot"]

    async with memory_database() as db:
        failed = await memory_job_crud.update_status(
            db,
            uid=uid,
            job_id=first_job_id,
            status=LongTermMemoryMutationStatus.FAILED,
            clear_active_mutation_key=True,
        )
        assert failed is not None
        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            organization_last_run_at=fixed_now,
            commit=False,
        )
        assert updated_store is not None
        await db.commit()

    async with memory_database() as db:
        before = await memory_store_crud.get_by_uid(db, uid=uid)
        assert before is not None
        before_store_state = (
            before.organization_last_job_id,
            before.organization_last_run_at,
            before.organization_error,
        )
        skipped = await manager.submit_auto_organization(db, uid=uid)
    assert skipped is None
    after_skip_store = await _get_store(memory_database, uid=uid)
    assert (
        after_skip_store.organization_last_job_id,
        after_skip_store.organization_last_run_at,
        after_skip_store.organization_error,
    ) == before_store_state
    assert (
        await _count_jobs(
            memory_database,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == 1
    )

    async with memory_database() as db:
        unrelated = await manager.submit_organization(
            db,
            uid=uid,
            dedupe_key="unrelated-organization",
        )
    assert unrelated.created
    assert unrelated.job.id is not None
    assert unrelated.job.payload["trigger"] == "manual"

    async with memory_database() as db:
        unrelated_finished = await memory_job_crud.update_status(
            db,
            uid=uid,
            job_id=unrelated.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            clear_active_mutation_key=True,
        )
        assert unrelated_finished is not None
        overdue_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            organization_last_job_id=unrelated.job.id,
            organization_last_run_at=old_run,
            commit=False,
        )
        assert overdue_store is not None
        await db.commit()

    async with memory_database() as db:
        second = await manager.submit_auto_organization(db, uid=uid)
    assert second is not None
    assert second.created
    assert second.job.id is not None
    assert second.job.id != first_job_id
    assert second.job.dedupe_key != first_dedupe_key
    assert second.job.dedupe_key.startswith(first_dedupe_key)
    assert second.job.payload["snapshot"] == first_snapshot
    assert {item["memory_id"] for item in second.job.payload["snapshot"]["items"]} == {record.id for record in records}

    assert second.job.id is not None
    async with memory_database() as db:
        succeeded_retry = await memory_job_crud.update_status(
            db,
            uid=uid,
            job_id=second.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            clear_active_mutation_key=True,
        )
        assert succeeded_retry is not None
        due_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            organization_last_run_at=old_run,
            commit=False,
        )
        assert due_store is not None
        await db.commit()

    async with memory_database() as db:
        third = await manager.submit_auto_organization(db, uid=uid)
    assert third is not None
    assert not third.created
    assert third.job.id == second.job.id
    assert third.job.status == LongTermMemoryMutationStatus.SUCCEEDED
    async with memory_database() as db:
        organization_jobs = await memory_job_crud.list_by_uid(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
    automatic_jobs = [job for job in organization_jobs if job.payload.get("trigger") == "auto"]
    manual_jobs = [job for job in organization_jobs if job.payload.get("trigger") == "manual"]
    assert len(organization_jobs) == 3
    assert len(automatic_jobs) == 2
    assert len(manual_jobs) == 1
    assert {job.id for job in automatic_jobs} == {first_job_id, second.job.id}

    async with memory_database() as db:
        old_failed = await memory_job_crud.get_by_id(db, uid=uid, job_id=first_job_id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert old_failed is not None
    assert old_failed.status == LongTermMemoryMutationStatus.FAILED
    assert old_failed.dedupe_key == first_dedupe_key
    assert old_failed.payload == first.job.payload
    assert old_failed.active_mutation_key is None
    assert store is not None
    assert store.organization_last_job_id == second.job.id
    assert store.organization_last_run_at is not None
    stored_run_at = store.organization_last_run_at
    if stored_run_at.tzinfo is None:
        stored_run_at = stored_run_at.replace(tzinfo=UTC)
    assert stored_run_at == fixed_now


@pytest.mark.asyncio
async def test_auto_payload_restores_as_auto_and_unknown_trigger_is_rejected(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "auto-payload-restore"
    await _setup_auto_user(memory_database, uid=uid, record_count=45)

    async with memory_database() as db:
        submission = await MemoryJobManager().submit_auto_organization(db, uid=uid)

    assert submission is not None
    restored = restore_organization_execution_payload(submission.job.payload)
    assert restored.trigger == "auto"
    assert restored.snapshot.digest == submission.job.payload["snapshot"]["digest"]

    unknown_trigger_payload = dict(submission.job.payload)
    unknown_trigger_payload["trigger"] = "scheduled"
    with pytest.raises(ParameterException) as exc_info:
        restore_organization_execution_payload(unknown_trigger_payload)
    assert exc_info.value.message == ERR_MEMORY_JOB_PAYLOAD_INVALID


async def _wait_for_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    status: LongTermMemoryMutationStatus,
) -> LongTermMemoryMutationJob:
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        async with session_factory() as db:
            job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
        if job is not None and job.status == status:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} did not reach {status}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_consumer_calls_auto_helper_only_for_successful_memory_publications(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "consumer-auto-trigger"
    success_operations = [
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
        LongTermMemoryMutationOperation.UPDATE,
    ]
    success_jobs = {
        operation: await _create_direct_job(
            memory_database,
            uid=uid,
            operation=operation,
            dedupe_key=f"success-{operation.value}",
        )
        for operation in success_operations
    }
    delete_job = await _create_direct_job(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
        dedupe_key="delete-cleanup",
    )
    organize_job = await _create_direct_job(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key="organize",
    )
    failed_job = await _create_direct_job(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EXTRACT,
        dedupe_key="failed",
    )
    retry_job = await _create_direct_job(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        dedupe_key="retry",
    )
    cancelled_job = await _create_direct_job(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="cancelled",
    )
    assert cancelled_job.id is not None
    async with memory_database() as db:
        cancellation = await memory_job_crud.request_cancel(db, uid=uid, job_id=cancelled_job.id)
    assert cancellation.changed

    calls: list[tuple[str, int]] = []

    async def submit_helper(_session_factory: Any, called_uid: str, source_job_id: int) -> None:
        calls.append((called_uid, source_job_id))

    async def success_handler(context) -> dict[str, object] | MemoryJobExecutionResult:
        if (
            context.job.operation
            in {
                LongTermMemoryMutationOperation.CREATE,
                LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            }
            and context.job.dedupe_key != "cancelled"
        ):
            async with context.session_factory() as db:
                assert await memory_job_crud.mark_succeeded(
                    db,
                    uid=context.job.uid,
                    job_id=context.job.id,
                    owner=context.worker_id,
                    result={"published": True},
                )
            return MemoryJobExecutionResult(result={"published": True}, finalized=True)
        return {"published": True}

    async def failure_handler(_context) -> dict[str, object]:
        raise MemoryJobDeterministicError("expected deterministic failure")

    async def retry_handler(_context) -> dict[str, object]:
        raise MemoryJobRetryableError("expected retry")

    executor = MemoryJobExecutor(
        {
            LongTermMemoryMutationOperation.CREATE: success_handler,
            LongTermMemoryMutationOperation.CREATE_WITH_EVICTION: success_handler,
            LongTermMemoryMutationOperation.UPDATE: success_handler,
            LongTermMemoryMutationOperation.DELETE_CLEANUP: success_handler,
            LongTermMemoryMutationOperation.ORGANIZE: success_handler,
            LongTermMemoryMutationOperation.EXTRACT: failure_handler,
            LongTermMemoryMutationOperation.ORGANIZE_MERGE: retry_handler,
        },
        session_factory=memory_database,
    )
    monkeypatch.setattr(consumer_module, "best_effort_submit_auto_organization_after_publication", submit_helper)
    consumer = MemoryJobConsumer(
        executor,
        memory_database,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=10,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=1,
    )
    try:
        assert await consumer.run_once() == 7
        for operation, job in success_jobs.items():
            assert job.id is not None
            await _wait_for_status(
                memory_database,
                uid=uid,
                job_id=job.id,
                status=LongTermMemoryMutationStatus.SUCCEEDED,
            )
        for job in (delete_job, organize_job):
            assert job.id is not None
            await _wait_for_status(
                memory_database,
                uid=uid,
                job_id=job.id,
                status=LongTermMemoryMutationStatus.SUCCEEDED,
            )
        assert failed_job.id is not None
        await _wait_for_status(
            memory_database,
            uid=uid,
            job_id=failed_job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert retry_job.id is not None
        await _wait_for_status(
            memory_database,
            uid=uid,
            job_id=retry_job.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert cancelled_job.id is not None
        cancelled = await _wait_for_status(
            memory_database,
            uid=uid,
            job_id=cancelled_job.id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        assert cancelled.status == LongTermMemoryMutationStatus.CANCELLED
        deadline = asyncio.get_running_loop().time() + 3
        while len(calls) < len(success_operations) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        expected_calls = [(uid, success_jobs[operation].id) for operation in success_operations]
        assert sorted(calls, key=lambda item: item[1]) == sorted(expected_calls, key=lambda item: item[1])
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_best_effort_auto_organization_keeps_source_success_and_stores_safe_config_error(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "best-effort-invalid-organization-config"
    await _setup_auto_user(
        memory_database,
        uid=uid,
        record_count=45,
        model_id="missing-organization-model",
    )
    source_job = await _create_direct_job(
        memory_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="published-source-job",
    )
    assert source_job.id is not None
    async with memory_database() as db:
        finished = await memory_job_crud.update_status(
            db,
            uid=uid,
            job_id=source_job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            clear_active_mutation_key=True,
        )
    assert finished is not None

    await best_effort_submit_auto_organization_after_publication(
        memory_database,
        uid,
        source_job.id,
    )

    async with memory_database() as db:
        current_source = await memory_job_crud.get_by_id(db, uid=uid, job_id=source_job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert current_source is not None
    assert current_source.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert store is not None
    assert store.organization_error is not None
    assert "organization-api-key" not in store.organization_error
    assert "memory content" not in store.organization_error
    assert (
        await _count_jobs(
            memory_database,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_auto_organization_isolates_records_and_store_state_by_uid(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    first_uid = "auto-isolation-first"
    second_uid = "auto-isolation-second"
    _, first_records = await _setup_auto_user(memory_database, uid=first_uid, record_count=45)
    second_store, second_records = await _setup_auto_user(
        memory_database,
        uid=second_uid,
        record_count=45,
        organization_last_job_id=777,
        organization_last_run_at=datetime(2025, 1, 1),
        organization_error="other-user-error",
    )

    async with memory_database() as db:
        submission = await MemoryJobManager().submit_auto_organization(db, uid=first_uid)

    assert submission is not None
    snapshot_ids = {item["memory_id"] for item in submission.job.payload["snapshot"]["items"]}
    assert snapshot_ids == {record.id for record in first_records}
    assert snapshot_ids.isdisjoint({record.id for record in second_records})

    first_after = await _get_store(memory_database, uid=first_uid)
    second_after = await _get_store(memory_database, uid=second_uid)
    assert first_after.organization_last_job_id == submission.job.id
    assert first_after.organization_error is None
    assert second_after.organization_last_job_id == second_store.organization_last_job_id
    assert second_after.organization_last_run_at == second_store.organization_last_run_at
    assert second_after.organization_error == second_store.organization_error
    assert (
        await _count_jobs(
            memory_database,
            uid=second_uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
        )
        == 0
    )
