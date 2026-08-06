from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.constants import (
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_OVER_LIMIT,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_MAX_ACTIVE_RECORDS,
)
from app.core.crud.memory import memory_record_crud, memory_revision_crud, memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.memory import MemoryConflictError, MemoryMutationStatus, cancel_job, memory_service
from app.core.memory.normalization import build_memory_content_hash, normalize_memory_content
from app.core.utils.tokenizer import estimate_tokens
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryStore,
    LongTermMemoryType,
)

MEMORY_TABLES = [
    LongTermMemoryStore.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]


@pytest_asyncio.fixture
async def memory_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "memory-capacity-reservation-stage8.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
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


async def _create_store(
    db: AsyncSession,
    *,
    uid: str,
    max_active_records: int = MEMORY_MAX_ACTIVE_RECORDS,
) -> LongTermMemoryStore:
    return await memory_store_crud.create(
        db,
        uid=uid,
        max_active_records=max_active_records,
        active_embedding_channel_id=7,
        active_embedding_model_id="memory-capacity-test-model",
        active_embedding_dimensions=2,
        active_embedding_signature="memory-capacity-test-signature",
        active_embedding_revision=1,
        active_collection_name=f"memory-capacity-{uid}",
        index_revision=1,
        index_status=LongTermMemoryIndexStatus.READY,
        commit=False,
    )


def _content_token_count(content: str) -> tuple[str, int, str]:
    normalized_content = normalize_memory_content(content)
    return normalized_content, estimate_tokens(normalized_content), build_memory_content_hash(normalized_content)


async def _create_record(
    db: AsyncSession,
    *,
    uid: str,
    memory_key: str,
    content: str,
    is_active: bool = True,
    deleted: bool = False,
    version: int = 1,
) -> LongTermMemoryRecord:
    normalized_content, content_token_count, content_hash = _content_token_count(content)
    return await memory_record_crud.create(
        db,
        uid=uid,
        memory_key=memory_key,
        memory_type=LongTermMemoryType.FACT,
        content=normalized_content,
        content_token_count=content_token_count,
        content_hash=content_hash,
        version=version,
        indexed_version=version if is_active else 0,
        vector_item_id=f"{uid}-{memory_key}-v{version}" if is_active else None,
        source=LongTermMemorySource.USER_API,
        source_id="stage8-capacity-seed",
        change_evidence="stage8 capacity seed",
        is_active=is_active,
        deleted_at=datetime.now(UTC) if deleted else None,
        index_status=LongTermMemoryRecordIndexStatus.READY,
        commit=False,
    )


async def _create_revision(
    db: AsyncSession,
    *,
    uid: str,
    memory_id: int,
    version: int,
    memory_key: str,
    content: str,
) -> LongTermMemoryRevision:
    normalized_content, content_token_count, content_hash = _content_token_count(content)
    return await memory_revision_crud.create(
        db,
        uid=uid,
        memory_id=memory_id,
        version=version,
        memory_key=memory_key,
        memory_type=LongTermMemoryType.FACT,
        content=normalized_content,
        content_token_count=content_token_count,
        content_hash=content_hash,
        source=LongTermMemorySource.USER_API,
        source_id="stage8-capacity-history",
        change_evidence="stage8 capacity history",
        published_at=datetime.now(UTC),
        commit=False,
    )


async def _seed_active_records(
    db: AsyncSession,
    *,
    uid: str,
    count: int,
    oversized_index: int | None = None,
) -> list[LongTermMemoryRecord]:
    records: list[LongTermMemoryRecord] = []
    for index in range(count):
        content = " ".join([f"oversized-{index}"] * (MEMORY_CONTENT_MAX_TOKENS + 20)) if index == oversized_index else f"legacy memory {index}"
        records.append(
            await _create_record(
                db,
                uid=uid,
                memory_key=f"legacy-key-{index}",
                content=content,
            )
        )
    return records


async def _count_pending_create_slots(
    db: AsyncSession,
    *,
    uid: str,
) -> int:
    return await memory_job_crud.count_pending_create(db, uid=uid)


async def _submit_create(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    dedupe_key: str,
    memory_key: str,
    content: str = "pending capacity content",
    source_id: str = "stage8-capacity-request",
):
    async with session_factory() as db:
        return await memory_service.create(
            db,
            uid=uid,
            dedupe_key=dedupe_key,
            content=content,
            memory_key=memory_key,
            memory_type=LongTermMemoryType.FACT,
            source=LongTermMemorySource.USER_API,
            source_id=source_id,
        )


@pytest.mark.asyncio
async def test_concurrent_distinct_creates_reserve_only_one_final_slot(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-race-owner"
    other_uid = "capacity-race-other"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _create_store(db, uid=other_uid)
        await _seed_active_records(db, uid=uid, count=49)
        await db.commit()

    barrier = asyncio.Barrier(2)

    async def submit_racing_create(dedupe_key: str, memory_key: str):
        async with memory_database() as db:
            await barrier.wait()
            return await memory_service.create(
                db,
                uid=uid,
                dedupe_key=dedupe_key,
                content=f"content-{memory_key}",
                memory_key=memory_key,
                memory_type=LongTermMemoryType.FACT,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-race-request",
            )

    outcomes = await asyncio.gather(
        submit_racing_create("race-create-a", "race-key-a"),
        submit_racing_create("race-create-b", "race-key-b"),
        return_exceptions=True,
    )

    accepted = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, MemoryConflictError)]
    assert len(accepted) == 1
    assert len(conflicts) == 1
    assert conflicts[0].message == ERR_MEMORY_CAPACITY_PENDING
    assert accepted[0].status == MemoryMutationStatus.ACCEPTED
    assert accepted[0].job_id is not None

    other_result = await _submit_create(
        memory_database,
        uid=other_uid,
        dedupe_key="other-user-create",
        memory_key="other-user-key",
    )
    assert other_result.status == MemoryMutationStatus.ACCEPTED
    assert other_result.job is not None
    assert other_result.job.uid == other_uid

    async with memory_database() as db:
        owner_active = await memory_record_crud.count_active(db, uid=uid)
        owner_pending = await _count_pending_create_slots(db, uid=uid)
        other_active = await memory_record_crud.count_active(db, uid=other_uid)
        other_pending = await _count_pending_create_slots(db, uid=other_uid)

    assert owner_active + owner_pending == MEMORY_MAX_ACTIVE_RECORDS
    assert owner_active == 49
    assert owner_pending == 1
    assert other_active == 0
    assert other_pending == 1


@pytest.mark.asyncio
async def test_same_dedupe_concurrent_and_immediate_retries_reuse_one_reserved_job(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-dedupe-owner"
    other_uid = "capacity-dedupe-other"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _create_store(db, uid=other_uid)
        await db.commit()

    barrier = asyncio.Barrier(2)

    async def submit_duplicate():
        async with memory_database() as db:
            await barrier.wait()
            return await memory_service.create(
                db,
                uid=uid,
                dedupe_key="same-create-dedupe",
                content="same dedupe content",
                memory_key="same-dedupe-key",
                memory_type=LongTermMemoryType.FACT,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-dedupe-request",
            )

    concurrent_results = await asyncio.gather(submit_duplicate(), submit_duplicate())
    immediate_retry = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="same-create-dedupe",
        memory_key="same-dedupe-key",
        content="same dedupe content",
        source_id="stage8-dedupe-request",
    )
    other_result = await _submit_create(
        memory_database,
        uid=other_uid,
        dedupe_key="same-create-dedupe",
        memory_key="same-dedupe-key",
        content="same dedupe content",
    )

    job_ids = {result.job_id for result in (*concurrent_results, immediate_retry)}
    assert len(job_ids) == 1
    assert None not in job_ids
    assert all(result.status == MemoryMutationStatus.ACCEPTED for result in (*concurrent_results, immediate_retry))
    assert other_result.job_id is not None
    assert other_result.job_id not in job_ids

    async with memory_database() as db:
        owner_pending = await _count_pending_create_slots(db, uid=uid)
        other_pending = await _count_pending_create_slots(db, uid=other_uid)
        owner_jobs = await memory_job_crud.count(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE,
        )

    assert owner_pending == 1
    assert other_pending == 1
    assert owner_jobs == 1


@pytest.mark.asyncio
async def test_cancelled_pending_create_releases_slot_for_a_different_create(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-cancel-owner"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _seed_active_records(db, uid=uid, count=49)
        await db.commit()

    first = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="cancelled-create",
        memory_key="cancelled-key",
    )
    assert first.job_id is not None

    async with memory_database() as db:
        assert await _count_pending_create_slots(db, uid=uid) == 1
        cancellation = await cancel_job(db, uid=uid, job_id=first.job_id)

    assert cancellation["accepted"] is True
    assert cancellation["changed"] is True
    assert cancellation["job"]["status"] == LongTermMemoryMutationStatus.CANCELLED.value

    second = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="replacement-create",
        memory_key="replacement-key",
    )
    assert second.status == MemoryMutationStatus.ACCEPTED
    assert second.job_id is not None
    assert second.job_id != first.job_id

    async with memory_database() as db:
        active = await memory_record_crud.count_active(db, uid=uid)
        pending = await _count_pending_create_slots(db, uid=uid)
        old_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=first.job_id)
        new_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=second.job_id)
        old_placeholder = await memory_record_crud.get_by_id(db, uid=uid, memory_id=old_job.memory_id) if old_job is not None and old_job.memory_id is not None else None

    assert active + pending == MEMORY_MAX_ACTIVE_RECORDS
    assert active == 49
    assert pending == 1
    assert old_job is not None
    assert old_job.uid == uid
    assert old_job.status == LongTermMemoryMutationStatus.CANCELLED
    assert old_placeholder is None
    assert new_job is not None
    assert new_job.uid == uid


@pytest.mark.asyncio
async def test_restore_rejects_when_last_capacity_slot_is_pending_create(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-restore-owner"
    other_uid = "capacity-restore-other"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _create_store(db, uid=other_uid)
        await _seed_active_records(db, uid=uid, count=49)
        deleted = await _create_record(
            db,
            uid=uid,
            memory_key="deleted-for-restore",
            content="deleted current content",
            is_active=False,
            deleted=True,
        )
        assert deleted.id is not None
        revision = await _create_revision(
            db,
            uid=uid,
            memory_id=deleted.id,
            version=1,
            memory_key="restored-memory-key",
            content="restored content",
        )
        await db.commit()

    pending_create = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="reserve-before-restore",
        memory_key="reserve-before-restore-key",
    )
    assert pending_create.status == MemoryMutationStatus.ACCEPTED
    assert revision.version is not None

    async with memory_database() as db:
        with pytest.raises(MemoryConflictError) as exc_info:
            await memory_service.restore(
                db,
                uid=uid,
                dedupe_key="restore-while-reserved",
                memory_id=deleted.id,
                revision_version=revision.version,
                expected_version=1,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-restore-request",
            )
        owner_pending = await _count_pending_create_slots(db, uid=uid)
        other_pending = await _count_pending_create_slots(db, uid=other_uid)
        restore_jobs = await memory_job_crud.count(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.RESTORE,
        )

    assert exc_info.value.message == ERR_MEMORY_CAPACITY_PENDING
    assert owner_pending == 1
    assert other_pending == 0
    assert restore_jobs == 0


@pytest.mark.asyncio
async def test_over_limit_legacy_data_rejects_growth_but_allows_safe_mutations_and_shrinking(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-over-limit-owner"
    other_uid = "capacity-over-limit-other"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _create_store(db, uid=other_uid)
        records = await _seed_active_records(db, uid=uid, count=51, oversized_index=0)
        restore_record = await _create_record(
            db,
            uid=uid,
            memory_key="legacy-deleted-record",
            content="legacy deleted content",
            is_active=False,
            deleted=True,
        )
        assert restore_record.id is not None
        restore_revision = await _create_revision(
            db,
            uid=uid,
            memory_id=restore_record.id,
            version=1,
            memory_key="legacy-restore-key",
            content="legacy restore content",
        )
        await db.commit()

    assert records[0].id is not None
    assert records[1].id is not None
    assert records[2].id is not None
    assert restore_revision.version is not None

    async with memory_database() as db:
        with pytest.raises(MemoryConflictError) as create_error:
            await memory_service.create(
                db,
                uid=uid,
                dedupe_key="over-limit-create",
                content="new short create",
                memory_key="over-limit-new-key",
                memory_type=LongTermMemoryType.FACT,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-over-limit-create",
            )
        with pytest.raises(MemoryConflictError) as restore_error:
            await memory_service.restore(
                db,
                uid=uid,
                dedupe_key="over-limit-restore",
                memory_id=restore_record.id,
                revision_version=restore_revision.version,
                expected_version=1,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-over-limit-restore",
            )
        with pytest.raises(MemoryConflictError) as update_error:
            await memory_service.update(
                db,
                uid=uid,
                dedupe_key="over-limit-growing-update",
                memory_id=records[1].id,
                expected_version=1,
                content="legacy memory 1 with additional replacement words",
                memory_key="legacy-growing-update",
                memory_type=LongTermMemoryType.FACT,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-over-limit-update",
            )

    assert create_error.value.message == ERR_MEMORY_OVER_LIMIT
    assert restore_error.value.message == ERR_MEMORY_OVER_LIMIT
    assert update_error.value.message == ERR_MEMORY_OVER_LIMIT

    async with memory_database() as db:
        shortened = await memory_service.update(
            db,
            uid=uid,
            dedupe_key="over-limit-shortening-update",
            memory_id=records[0].id,
            expected_version=1,
            content="shortened legacy memory",
            memory_key="legacy-shortened-update",
            memory_type=LongTermMemoryType.FACT,
            source=LongTermMemorySource.USER_API,
            source_id="stage8-over-limit-shortening",
        )
    assert shortened.status == MemoryMutationStatus.ACCEPTED
    assert shortened.job_id is not None
    assert shortened.job is not None
    assert shortened.job.payload["content_token_count"] < records[0].content_token_count

    async with memory_database() as db:
        deleted = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="over-limit-delete",
            memory_id=records[2].id,
            expected_version=1,
            source=LongTermMemorySource.USER_API,
            source_id="stage8-over-limit-delete",
        )
        pinned = await memory_service.pin(db, uid=uid, memory_id=records[3].id)
        assert pinned.pinned is True
        unpinned = await memory_service.unpin(db, uid=uid, memory_id=records[3].id)
        owner_deleted = await memory_record_crud.get_by_id(db, uid=uid, memory_id=records[2].id)
        owner_pinned = await memory_record_crud.get_by_id(db, uid=uid, memory_id=records[3].id)

    assert deleted.status == MemoryMutationStatus.ACCEPTED
    assert deleted.job_id is not None
    assert unpinned.pinned is False
    assert owner_deleted is not None
    assert owner_deleted.uid == uid
    assert owner_deleted.is_active is False
    assert owner_pinned is not None
    assert owner_pinned.uid == uid
    assert owner_pinned.pinned is False

    other_result = await _submit_create(
        memory_database,
        uid=other_uid,
        dedupe_key="other-user-over-limit-create",
        memory_key="other-user-over-limit-key",
        content="other user remains within limit",
    )
    assert other_result.status == MemoryMutationStatus.ACCEPTED
    async with memory_database() as db:
        assert await _count_pending_create_slots(db, uid=uid) == 0
        assert await _count_pending_create_slots(db, uid=other_uid) == 1


@pytest.mark.asyncio
async def test_oversized_active_record_blocks_create_but_allows_strictly_shorter_update(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-content-over-limit-owner"
    other_uid = "capacity-content-over-limit-other"
    oversized_content = " ".join(["oversized"] * (MEMORY_CONTENT_MAX_TOKENS + 20))
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _create_store(db, uid=other_uid)
        oversized = await _create_record(
            db,
            uid=uid,
            memory_key="oversized-active-memory",
            content=oversized_content,
        )
        await db.commit()

    assert oversized.id is not None
    assert oversized.content_token_count > MEMORY_CONTENT_MAX_TOKENS

    async with memory_database() as db:
        with pytest.raises(MemoryConflictError) as create_error:
            await memory_service.create(
                db,
                uid=uid,
                dedupe_key="content-over-limit-create",
                content="new short content",
                memory_key="content-over-limit-new-key",
                memory_type=LongTermMemoryType.FACT,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-content-over-limit-create",
            )

    assert create_error.value.message == ERR_MEMORY_OVER_LIMIT

    async with memory_database() as db:
        shortened = await memory_service.update(
            db,
            uid=uid,
            dedupe_key="content-over-limit-shortening-update",
            memory_id=oversized.id,
            expected_version=1,
            content="short replacement content",
            memory_key="shortened-active-memory",
            memory_type=LongTermMemoryType.FACT,
            source=LongTermMemorySource.USER_API,
            source_id="stage8-content-over-limit-update",
        )

    assert shortened.status == MemoryMutationStatus.ACCEPTED
    assert shortened.job is not None
    assert shortened.job.payload["content_token_count"] < oversized.content_token_count

    other_result = await _submit_create(
        memory_database,
        uid=other_uid,
        dedupe_key="other-content-over-limit-create",
        memory_key="other-content-over-limit-key",
        content="other user short content",
    )
    assert other_result.status == MemoryMutationStatus.ACCEPTED

    async with memory_database() as db:
        owner = await memory_record_crud.get_by_id(db, uid=uid, memory_id=oversized.id)
        assert owner is not None
        assert owner.uid == uid
        assert await _count_pending_create_slots(db, uid=uid) == 0
        assert await _count_pending_create_slots(db, uid=other_uid) == 1
