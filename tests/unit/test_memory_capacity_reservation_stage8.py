from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.constants import (
    ERR_MEMORY_CAPACITY_FULL,
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_OVER_LIMIT,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_MAX_ACTIVE_RECORDS,
)
from app.core.crud.memory import memory_record_crud, memory_revision_crud, memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.memory import (
    MemoryConflictError,
    MemoryMutationStatus,
    build_memory_active_mutation_key,
    build_memory_record_snapshot,
    cancel_job,
    memory_service,
)
from app.core.memory.normalization import build_memory_content_hash, normalize_memory_content
from app.core.utils.tokenizer import estimate_tokens
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
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
    LongTermMemoryEmbeddingDelta.__table__,
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
    indexed_version: int | None = None,
    vector_item_id: str | None = None,
    pinned: bool = False,
    last_recalled_at: datetime | None = None,
    pending_mutation_job_id: int | None = None,
    suppress_recall: bool = False,
    index_status: LongTermMemoryRecordIndexStatus = LongTermMemoryRecordIndexStatus.READY,
    updated_at: datetime | None = None,
) -> LongTermMemoryRecord:
    normalized_content, content_token_count, content_hash = _content_token_count(content)
    if indexed_version is None:
        indexed_version = version if is_active else 0
    if vector_item_id is None and is_active:
        vector_item_id = f"{uid}-{memory_key}-v{version}"
    values = {
        "uid": uid,
        "memory_key": memory_key,
        "memory_type": LongTermMemoryType.FACT,
        "content": normalized_content,
        "content_token_count": content_token_count,
        "content_hash": content_hash,
        "version": version,
        "indexed_version": indexed_version,
        "vector_item_id": vector_item_id,
        "source": LongTermMemorySource.USER_API,
        "source_id": "stage8-capacity-seed",
        "change_evidence": "stage8 capacity seed",
        "is_active": is_active,
        "pinned": pinned,
        "last_recalled_at": last_recalled_at,
        "pending_mutation_job_id": pending_mutation_job_id,
        "suppress_recall": suppress_recall,
        "deleted_at": datetime.now(UTC) if deleted else None,
        "index_status": index_status,
    }
    if updated_at is not None:
        values["updated_at"] = updated_at
    return await memory_record_crud.create(
        db,
        **values,
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
    source: LongTermMemorySource = LongTermMemorySource.USER_API,
    max_attempts: int = 3,
):
    async with session_factory() as db:
        return await memory_service.create(
            db,
            uid=uid,
            dedupe_key=dedupe_key,
            content=content,
            memory_key=memory_key,
            memory_type=LongTermMemoryType.FACT,
            source=source,
            source_id=source_id,
            max_attempts=max_attempts,
        )


@pytest.mark.asyncio
async def test_full_capacity_create_accepts_replacement_and_reserves_candidate_with_strict_payload(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-replacement-owner"
    content = "pending capacity content"
    memory_key = "replacement-memory-key"
    async with memory_database() as db:
        store = await _create_store(db, uid=uid)
        existing_records = await _seed_active_records(db, uid=uid, count=MEMORY_MAX_ACTIVE_RECORDS)
        existing_ids = [record.id for record in existing_records]
        assert all(memory_id is not None for memory_id in existing_ids)
        await db.commit()

    result = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="full-capacity-replacement",
        memory_key=memory_key,
        content=content,
    )

    assert result.status == MemoryMutationStatus.ACCEPTED
    assert result.job is not None
    assert result.job.id is not None
    assert result.job.operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION
    assert result.job.memory_id is not None
    assert result.job.memory_id > max(existing_ids)
    assert result.job.active_mutation_key == build_memory_active_mutation_key(uid, memory_key=memory_key)

    normalized_content, content_token_count, content_hash = _content_token_count(content)
    async with memory_database() as db:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=result.job.id)
        assert job is not None
        assert job.id is not None
        candidate_id = job.payload["candidate"]["memory_id"]
        candidate = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        current_store = await memory_store_crud.get_by_uid(db, uid=uid)
        active_count = await memory_record_crud.count_active(db, uid=uid)
        pending_create_count = await _count_pending_create_slots(db, uid=uid)
        replacement_record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=job.memory_id)

    assert candidate is not None
    assert current_store is not None
    assert candidate.pending_mutation_job_id == job.id
    assert replacement_record is None
    assert active_count == MEMORY_MAX_ACTIVE_RECORDS
    assert pending_create_count == 0
    assert set(job.payload) == {"publication", "candidate", "store"}
    assert job.payload == {
        "publication": {
            "memory_key": memory_key,
            "content": normalized_content,
            "content_token_count": content_token_count,
            "content_hash": content_hash,
            "memory_type": LongTermMemoryType.FACT.value,
            "source": LongTermMemorySource.USER_API.value,
            "source_id": "stage8-capacity-request",
            "source_session_id": None,
            "source_profile_id": None,
            "source_message_id": None,
            "change_evidence": None,
        },
        "candidate": {
            "memory_id": candidate.id,
            "version": candidate.version,
            "vector_item_id": candidate.vector_item_id,
            "record_snapshot": build_memory_record_snapshot(candidate),
        },
        "store": {
            "active_embedding_channel_id": current_store.active_embedding_channel_id,
            "active_embedding_model_id": current_store.active_embedding_model_id,
            "active_embedding_dimensions": current_store.active_embedding_dimensions,
            "active_embedding_signature": current_store.active_embedding_signature,
            "active_embedding_revision": current_store.active_embedding_revision,
            "active_collection_name": current_store.active_collection_name,
            "max_active_records": current_store.max_active_records,
            "organize_trigger_records": current_store.organize_trigger_records,
            "active_count": MEMORY_MAX_ACTIVE_RECORDS,
            "index_revision": current_store.index_revision,
            "index_status": getattr(current_store.index_status, "value", current_store.index_status),
            "capacity_status": getattr(current_store.capacity_status, "value", current_store.capacity_status),
        },
    }
    assert job.payload["store"]["active_collection_name"] == store.active_collection_name


@pytest.mark.asyncio
async def test_update_rejects_eviction_candidate_reserved_by_replacement_job_without_changes(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-replacement-update-owner"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _seed_active_records(db, uid=uid, count=MEMORY_MAX_ACTIVE_RECORDS)
        await db.commit()

    replacement = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="replacement-before-update",
        memory_key="replacement-before-update-key",
    )
    assert replacement.job is not None
    assert replacement.job.id is not None
    assert replacement.job.operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION
    candidate_id = replacement.job.payload["candidate"]["memory_id"]

    async with memory_database() as db:
        candidate = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        before = {
            "version": candidate.version,
            "content": candidate.content,
            "vector_item_id": candidate.vector_item_id,
            "indexed_version": candidate.indexed_version,
            "index_status": candidate.index_status,
        }

        with pytest.raises(MemoryConflictError) as exc_info:
            await memory_service.update(
                db,
                uid=uid,
                dedupe_key="update-reserved-candidate",
                memory_id=candidate_id,
                expected_version=candidate.version,
                content="candidate update must be rejected",
                memory_key="candidate-update-key",
                memory_type=LongTermMemoryType.FACT,
                source=LongTermMemorySource.USER_API,
                source_id="stage8-capacity-update-request",
            )

        update_jobs = await memory_job_crud.count(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
        )
        candidate_after = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        active_count = await memory_record_crud.count_active(db, uid=uid)

    assert exc_info.value.message == ERR_MEMORY_MUTATION_PENDING
    assert update_jobs == 0
    assert candidate_after is not None
    assert {
        "version": candidate_after.version,
        "content": candidate_after.content,
        "vector_item_id": candidate_after.vector_item_id,
        "indexed_version": candidate_after.indexed_version,
        "index_status": candidate_after.index_status,
    } == before
    assert candidate_after.pending_mutation_job_id == replacement.job.id
    assert active_count == MEMORY_MAX_ACTIVE_RECORDS


@pytest.mark.asyncio
async def test_eviction_candidate_sorting_filters_pinned_pending_suppressed_and_unready_records(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-candidate-order-owner"
    base = datetime(2024, 1, 1, tzinfo=UTC)
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        null_old = await _create_record(
            db,
            uid=uid,
            memory_key="null-old",
            content="null old",
            updated_at=base + timedelta(days=1),
        )
        null_new = await _create_record(
            db,
            uid=uid,
            memory_key="null-new",
            content="null new",
            updated_at=base + timedelta(days=2),
        )
        recalled_early = await _create_record(
            db,
            uid=uid,
            memory_key="recalled-early",
            content="recalled early",
            last_recalled_at=base + timedelta(days=3),
            updated_at=base - timedelta(days=4),
        )
        recalled_late = await _create_record(
            db,
            uid=uid,
            memory_key="recalled-late",
            content="recalled late",
            last_recalled_at=base + timedelta(days=4),
            updated_at=base - timedelta(days=5),
        )
        recalled_tie_a = await _create_record(
            db,
            uid=uid,
            memory_key="recalled-tie-a",
            content="recalled tie a",
            last_recalled_at=base + timedelta(days=5),
            updated_at=base + timedelta(days=6),
        )
        recalled_tie_b = await _create_record(
            db,
            uid=uid,
            memory_key="recalled-tie-b",
            content="recalled tie b",
            last_recalled_at=base + timedelta(days=5),
            updated_at=base + timedelta(days=6),
        )
        await _create_record(
            db,
            uid=uid,
            memory_key="filtered-pinned",
            content="filtered pinned",
            pinned=True,
            updated_at=base - timedelta(days=20),
        )
        await _create_record(
            db,
            uid=uid,
            memory_key="filtered-pending",
            content="filtered pending",
            pending_mutation_job_id=9001,
            updated_at=base - timedelta(days=21),
        )
        await _create_record(
            db,
            uid=uid,
            memory_key="filtered-suppressed",
            content="filtered suppressed",
            suppress_recall=True,
            updated_at=base - timedelta(days=22),
        )
        await _create_record(
            db,
            uid=uid,
            memory_key="filtered-unready",
            content="filtered unready",
            index_status=LongTermMemoryRecordIndexStatus.FAILED,
            indexed_version=0,
            updated_at=base - timedelta(days=23),
        )
        await _create_record(
            db,
            uid=uid,
            memory_key="filtered-stale-index",
            content="filtered stale index",
            indexed_version=0,
            updated_at=base - timedelta(days=24),
        )
        for index in range(MEMORY_MAX_ACTIVE_RECORDS - 11):
            await _create_record(
                db,
                uid=uid,
                memory_key=f"filtered-filler-{index}",
                content=f"filtered filler {index}",
                pinned=True,
                updated_at=base - timedelta(days=30),
            )
        await db.commit()

        candidate = await memory_record_crud.get_eviction_candidate(db, uid=uid)
        assert candidate is not None
        assert candidate.id == null_old.id
        null_old.pinned = True
        await db.flush()

        candidate = await memory_record_crud.get_eviction_candidate(db, uid=uid)
        assert candidate is not None
        assert candidate.id == null_new.id
        null_new.pinned = True
        await db.flush()

        candidate = await memory_record_crud.get_eviction_candidate(db, uid=uid)
        assert candidate is not None
        assert candidate.id == recalled_early.id
        recalled_early.pinned = True
        await db.flush()

        candidate = await memory_record_crud.get_eviction_candidate(db, uid=uid)
        assert candidate is not None
        assert candidate.id == recalled_late.id
        recalled_late.pinned = True
        await db.flush()

        candidate = await memory_record_crud.get_eviction_candidate(db, uid=uid)
        assert candidate is not None
        assert candidate.id == recalled_tie_a.id
        recalled_tie_a.pinned = True
        await db.flush()

        candidate = await memory_record_crud.get_eviction_candidate(db, uid=uid)
        assert candidate is not None
        assert candidate.id == recalled_tie_b.id
        assert recalled_tie_a.id is not None
        assert recalled_tie_b.id is not None
        assert recalled_tie_a.id < recalled_tie_b.id


@pytest.mark.asyncio
async def test_full_capacity_create_raises_when_all_records_are_unqualified_without_replacement_job(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-no-candidate-owner"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        records = await _seed_active_records(db, uid=uid, count=MEMORY_MAX_ACTIVE_RECORDS)
        for record in records:
            record.pinned = True
        await db.commit()

    with pytest.raises(MemoryConflictError) as exc_info:
        await _submit_create(
            memory_database,
            uid=uid,
            dedupe_key="full-without-candidate",
            memory_key="no-candidate-key",
        )

    assert exc_info.value.message == ERR_MEMORY_CAPACITY_FULL
    async with memory_database() as db:
        assert await memory_job_crud.count(db, uid=uid) == 0
        assert (
            await memory_job_crud.count(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_replacement_retry_reuses_job_candidate_and_memory_id_but_changed_identity_conflicts(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-replacement-dedupe-owner"
    other_uid = "capacity-replacement-dedupe-other"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _create_store(db, uid=other_uid)
        await _seed_active_records(db, uid=uid, count=MEMORY_MAX_ACTIVE_RECORDS)
        other_records = await _seed_active_records(db, uid=other_uid, count=MEMORY_MAX_ACTIVE_RECORDS)
        other_ids = {record.id for record in other_records}
        await db.commit()

    first = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="replacement-dedupe",
        memory_key="replacement-dedupe-key",
        content="replacement body",
        source_id="replacement-source",
        max_attempts=3,
    )
    retry = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="replacement-dedupe",
        memory_key="replacement-dedupe-key",
        content="replacement body",
        source_id="replacement-source",
        max_attempts=3,
    )

    assert first.job is not None
    assert retry.job is not None
    assert retry.job.id == first.job.id
    assert retry.job.memory_id == first.job.memory_id
    assert retry.job.payload["candidate"] == first.job.payload["candidate"]

    other_result = await _submit_create(
        memory_database,
        uid=other_uid,
        dedupe_key="replacement-dedupe",
        memory_key="replacement-dedupe-key",
        content="replacement body",
        source_id="replacement-source",
        max_attempts=3,
    )
    assert other_result.job is not None
    assert other_result.job.id != first.job.id
    assert other_result.job.uid == other_uid
    assert other_result.job.payload["candidate"]["memory_id"] in other_ids

    with pytest.raises(MemoryConflictError):
        await _submit_create(
            memory_database,
            uid=uid,
            dedupe_key="replacement-dedupe",
            memory_key="replacement-dedupe-key",
            content="changed replacement body",
            source_id="replacement-source",
            max_attempts=3,
        )
    with pytest.raises(MemoryConflictError):
        await _submit_create(
            memory_database,
            uid=uid,
            dedupe_key="replacement-dedupe",
            memory_key="replacement-dedupe-key",
            content="replacement body",
            source=LongTermMemorySource.LLM_TOOL,
            source_id="replacement-source-other",
            max_attempts=3,
        )
    with pytest.raises(MemoryConflictError):
        await _submit_create(
            memory_database,
            uid=uid,
            dedupe_key="replacement-dedupe",
            memory_key="replacement-dedupe-key",
            content="replacement body",
            source_id="replacement-source",
            max_attempts=4,
        )

    async with memory_database() as db:
        assert (
            await memory_job_crud.count(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            )
            == 1
        )
        assert (
            await memory_job_crud.count(
                db,
                uid=other_uid,
                operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            )
            == 1
        )
        candidate = await memory_record_crud.get_by_id(
            db,
            uid=uid,
            memory_id=first.job.payload["candidate"]["memory_id"],
        )
    assert candidate is not None
    assert candidate.pending_mutation_job_id == first.job.id


@pytest.mark.asyncio
async def test_cancelled_replacement_clears_candidate_and_allows_another_full_capacity_replacement(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "capacity-replacement-cancel-owner"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        await _seed_active_records(db, uid=uid, count=MEMORY_MAX_ACTIVE_RECORDS)
        await db.commit()

    first = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="replacement-cancel-first",
        memory_key="replacement-cancel-first-key",
    )
    assert first.job is not None
    assert first.job.id is not None
    assert first.job.memory_id is not None
    candidate_id = first.job.payload["candidate"]["memory_id"]

    async with memory_database() as db:
        cancellation = await cancel_job(db, uid=uid, job_id=first.job.id)
    assert cancellation["accepted"] is True
    assert cancellation["changed"] is True

    async with memory_database() as db:
        cancelled = await memory_job_crud.get_by_id(db, uid=uid, job_id=first.job.id)
        candidate = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        unmaterialized = await memory_record_crud.get_by_id(db, uid=uid, memory_id=first.job.memory_id)
    assert cancelled is not None
    assert cancelled.status == LongTermMemoryMutationStatus.CANCELLED
    assert candidate is not None
    assert candidate.is_active is True
    assert candidate.deleted_at is None
    assert candidate.pending_mutation_job_id is None
    assert candidate.suppress_recall is False
    assert candidate.index_status == LongTermMemoryRecordIndexStatus.READY
    assert candidate.indexed_version == candidate.version
    assert candidate.vector_item_id
    assert candidate.pinned is False
    assert unmaterialized is None

    second = await _submit_create(
        memory_database,
        uid=uid,
        dedupe_key="replacement-cancel-second",
        memory_key="replacement-cancel-second-key",
    )
    assert second.status == MemoryMutationStatus.ACCEPTED
    assert second.job is not None
    assert second.job.id is not None
    assert second.job.id != first.job.id
    assert second.job.payload["candidate"]["memory_id"] == candidate_id

    async with memory_database() as db:
        candidate = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        assert candidate.pending_mutation_job_id == second.job.id
        assert await memory_record_crud.count_active(db, uid=uid) == MEMORY_MAX_ACTIVE_RECORDS
        assert await _count_pending_create_slots(db, uid=uid) == 0


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
