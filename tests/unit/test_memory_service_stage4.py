from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.constants import ERR_MEMORY_OVER_LIMIT, ERR_MEMORY_VERSION_CONFLICT
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_embedding_delta_crud,
    memory_record_crud,
    memory_store_crud,
)
from app.core.memory import (
    MemoryConflictError,
    MemoryMutationStatus,
    MemoryNotFoundError,
    MemoryRecallStatus,
    MemoryValidationError,
    append_memory_embedding_delta,
    build_memory_active_mutation_key,
    build_memory_content_hash,
    build_memory_vector_item_id,
    memory_service,
    normalize_change_evidence,
    normalize_memory_content,
    normalize_memory_key,
)
from app.core.memory import service as memory_service_module
from app.core.memory_jobs.manager import memory_job_manager
from app.core.utils.tokenizer import estimate_tokens
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
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
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryMutationJob.__table__,
    LongTermMemoryRevision.__table__,
]


@pytest_asyncio.fixture
async def memory_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "memory-service-stage4.db"
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


def _collection_name(uid: str) -> str:
    return f"memory-test-{hashlib.sha256(uid.encode()).hexdigest()[:12]}"


async def _create_store(
    db: AsyncSession,
    *,
    uid: str,
    configured: bool = True,
    max_active_records: int = 50,
    migration_job_id: int | None = None,
    migration_status: LongTermMemoryMigrationStatus | None = None,
    migration_delta_high_watermark: int = 0,
    collection_name: str | None = None,
) -> LongTermMemoryStore:
    values: dict[str, Any] = {"max_active_records": max_active_records}
    if configured:
        values.update(
            {
                "active_embedding_channel_id": 7,
                "active_embedding_model_id": "memory-embedding-model",
                "active_embedding_dimensions": 2,
                "active_embedding_signature": "memory-embedding-signature",
                "active_embedding_revision": 1,
                "active_collection_name": collection_name or _collection_name(uid),
                "index_revision": 1,
                "index_status": LongTermMemoryIndexStatus.READY,
            }
        )
    if migration_job_id is not None:
        values.update(
            {
                "migration_job_id": migration_job_id,
                "migration_status": migration_status or LongTermMemoryMigrationStatus.PREPARING,
                "migration_delta_high_watermark": migration_delta_high_watermark,
            }
        )
    return await memory_store_crud.create(db, uid=uid, commit=False, **values)


async def _create_record(
    db: AsyncSession,
    *,
    uid: str,
    memory_key: str,
    content: str,
    version: int = 1,
    memory_type: LongTermMemoryType = LongTermMemoryType.FACT,
    source: LongTermMemorySource = LongTermMemorySource.USER_API,
    source_id: str | None = "seed-source",
    source_message_id: int | None = 1,
    change_evidence: str | None = "seed evidence",
    is_active: bool = True,
    deleted: bool = False,
    suppress_recall: bool = False,
    indexed_version: int | None = None,
    index_status: LongTermMemoryRecordIndexStatus = LongTermMemoryRecordIndexStatus.READY,
    vector_item_id: str | None = None,
    pending_mutation_job_id: int | None = None,
) -> LongTermMemoryRecord:
    if indexed_version is None:
        indexed_version = version if index_status == LongTermMemoryRecordIndexStatus.READY else 0
    record = await memory_record_crud.create(
        db,
        uid=uid,
        memory_key=memory_key,
        content=content,
        content_token_count=estimate_tokens(normalize_memory_content(content)),
        content_hash=build_memory_content_hash(content),
        memory_type=memory_type,
        version=version,
        indexed_version=indexed_version,
        vector_item_id=vector_item_id,
        source=source,
        source_id=source_id,
        source_message_id=source_message_id,
        change_evidence=change_evidence,
        is_active=is_active,
        deleted_at=datetime.now(UTC) if deleted else None,
        suppress_recall=suppress_recall,
        pending_mutation_job_id=pending_mutation_job_id,
        index_status=index_status,
        commit=False,
    )
    if vector_item_id is None and version >= 1 and record.id is not None:
        record.vector_item_id = build_memory_vector_item_id(record.id, version)
        await db.flush()
    return record


async def _get_record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_id: int,
) -> LongTermMemoryRecord | None:
    async with session_factory() as db:
        return await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)


async def _get_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
):
    async with session_factory() as db:
        return await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)


async def _get_deltas(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    migration_job_id: int,
):
    async with session_factory() as db:
        return await memory_embedding_delta_crud.list_by_migration_job(
            db,
            uid=uid,
            migration_job_id=migration_job_id,
        )


def _create_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "content": "memory content",
        "memory_key": "memory-key",
        "memory_type": LongTermMemoryType.FACT,
        "change_evidence": "created by test",
        "source": LongTermMemorySource.USER_API,
        "source_id": "source-id",
        "source_session_id": "session-id",
        "source_profile_id": 11,
        "source_message_id": 12,
    }
    values.update(overrides)
    return values


def test_validate_active_store_rejects_over_limit_capacity_configuration() -> None:
    store = SimpleNamespace(
        active_embedding_channel_id=1,
        active_embedding_model_id="memory-model",
        active_embedding_dimensions=2,
        active_embedding_signature="memory-signature",
        active_embedding_revision=1,
        active_collection_name="memory-collection",
        max_active_records=51,
        organize_trigger_records=45,
    )

    with pytest.raises(MemoryConflictError) as exc_info:
        memory_service_module._validate_active_store(store)

    assert exc_info.value.message == ERR_MEMORY_OVER_LIMIT


def test_memory_normalization_hash_and_active_target_identity_are_stable() -> None:
    content = "  Ａ\tＢ\nＣ  "
    assert normalize_memory_content(content) == "A B C"
    assert normalize_memory_content("Ａ  B") == "A B"
    assert normalize_memory_key("  ｋｅｙ\nvalue ") == "key value"
    assert normalize_change_evidence("  reason\n\tfor update ") == "reason for update"
    assert build_memory_content_hash(content) == build_memory_content_hash("A B C")

    by_key = build_memory_active_mutation_key("uid-with-secret", memory_key="key-with-secret")
    by_same_key = build_memory_active_mutation_key("uid-with-secret", memory_key="key-with-secret")
    by_other_key = build_memory_active_mutation_key("uid-with-secret", memory_key="other-key")
    by_id = build_memory_active_mutation_key("uid-with-secret", memory_id=19)
    assert by_key == by_same_key
    assert by_key != by_other_key
    assert by_key != by_id
    assert "uid-with-secret" not in by_key
    assert "key-with-secret" not in by_key
    assert by_id == build_memory_active_mutation_key("uid-with-secret", memory_id=19)


@pytest.mark.asyncio
async def test_create_normalizes_publication_but_preserves_nonempty_identifiers(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = " uid with original spacing "
    dedupe_key = " dedupe with original spacing "
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        result = await memory_service.create(
            db,
            uid=uid,
            dedupe_key=dedupe_key,
            content="  Ａ\tB\nＣ  ",
            memory_key="  ｋｅｙ\nname ",
            memory_type=LongTermMemoryType.FACT,
            change_evidence=" evidence\nline ",
            source=LongTermMemorySource.LLM_TOOL,
            source_id=" source id ",
            source_session_id=" session id ",
            source_profile_id=7,
            source_message_id=8,
        )

    assert result.status == MemoryMutationStatus.ACCEPTED
    job = await _get_job(memory_database, uid=uid, job_id=result.job_id)
    assert job is not None
    assert job.uid == uid
    assert job.dedupe_key == dedupe_key
    assert job.payload == {
        "memory_key": "key name",
        "content": "A B C",
        "content_token_count": estimate_tokens("A B C"),
        "content_hash": build_memory_content_hash("A B C"),
        "memory_type": "fact",
        "source": "llm_tool",
        "source_id": " source id ",
        "source_session_id": " session id ",
        "source_profile_id": 7,
        "source_message_id": 8,
        "change_evidence": "evidence line",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_content",
    [
        "password = p@ss word",
        "Token: bearer-test-token",
        "API Key: api-key-test-value",
        "-----BEGIN PRIVATE KEY-----\nprivate-key-body\n-----END PRIVATE KEY-----",
    ],
)
async def test_create_accepts_credential_shaped_plaintext_and_persists_normalized_plaintext(
    memory_database: async_sessionmaker[AsyncSession],
    credential_content: str,
) -> None:
    uid = "credential-shaped-user"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        result = await memory_service.create(
            db,
            uid=uid,
            dedupe_key=f"credential-{credential_content[:8]}",
            **_create_kwargs(content=f"  {credential_content}\n"),
        )

    job = await _get_job(memory_database, uid=uid, job_id=result.job_id)
    assert job is not None
    assert job.payload["content"] == normalize_memory_content(f"  {credential_content}\n")
    assert job.payload["content_hash"] == build_memory_content_hash(job.payload["content"])
    assert not any(field_name.lower() in {"encrypted", "ciphertext", "masked", "redacted", "secret"} for field_name in job.payload)
    assert credential_content.splitlines()[0].split("=")[0].split(":")[0].strip().lower() in job.payload["content"].lower()


@pytest.mark.asyncio
async def test_create_requires_configured_store_and_dedupes_exact_identity(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "create-identity-user"
    kwargs = _create_kwargs()
    async with memory_database() as db:
        with pytest.raises(MemoryConflictError):
            await memory_service.create(db, uid=uid, dedupe_key="create-1", **kwargs)

    async with memory_database() as db:
        await _create_store(db, uid=uid)
        first = await memory_service.create(db, uid=uid, dedupe_key="create-1", **kwargs)
        second = await memory_service.create(db, uid=uid, dedupe_key="create-1", **kwargs)

    assert first.status == MemoryMutationStatus.ACCEPTED
    assert second.status == MemoryMutationStatus.ACCEPTED
    assert first.job_id == second.job_id
    jobs = await _get_job(memory_database, uid=uid, job_id=first.job_id)
    assert jobs is not None
    assert jobs.status == LongTermMemoryMutationStatus.PENDING

    async with memory_database() as db:
        with pytest.raises(MemoryConflictError):
            await memory_service.create(
                db,
                uid=uid,
                dedupe_key="create-1",
                **_create_kwargs(content="different payload"),
            )
    unchanged = await _get_job(memory_database, uid=uid, job_id=first.job_id)
    assert unchanged is not None
    assert unchanged.payload["content"] == "memory content"


@pytest.mark.asyncio
async def test_create_returns_unchanged_for_exact_published_record_and_existing_for_key_or_hash(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "create-existing-user"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        exact = await _create_record(
            db,
            uid=uid,
            memory_key="exact-key",
            content="exact content",
        )
        same_key = await _create_record(
            db,
            uid=uid,
            memory_key="key-only",
            content="key-only content",
        )
        same_hash = await _create_record(
            db,
            uid=uid,
            memory_key="hash-only",
            content="hash-only content",
        )
        await db.commit()

        unchanged = await memory_service.create(
            db,
            uid=uid,
            dedupe_key="exact-request",
            content="exact content",
            memory_key="exact-key",
            memory_type=LongTermMemoryType.FACT,
        )
        key_existing = await memory_service.create(
            db,
            uid=uid,
            dedupe_key="key-request",
            content="different content",
            memory_key="key-only",
            memory_type=LongTermMemoryType.FACT,
        )
        hash_existing = await memory_service.create(
            db,
            uid=uid,
            dedupe_key="hash-request",
            content="hash-only content",
            memory_key="different-key",
            memory_type=LongTermMemoryType.FACT,
        )

    assert unchanged.status == MemoryMutationStatus.UNCHANGED
    assert unchanged.memory_id == exact.id
    assert key_existing.status == MemoryMutationStatus.EXISTING
    assert key_existing.memory_id == same_key.id
    assert hash_existing.status == MemoryMutationStatus.EXISTING
    assert hash_existing.memory_id == same_hash.id


@pytest.mark.asyncio
async def test_create_rejects_capacity_but_allows_same_key_and_hash_for_other_uid(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    async with memory_database() as db:
        await _create_store(db, uid="capacity-user", max_active_records=1)
        await _create_record(
            db,
            uid="capacity-user",
            memory_key="occupied",
            content="occupied content",
        )
        await db.commit()
        with pytest.raises(MemoryConflictError):
            await memory_service.create(
                db,
                uid="capacity-user",
                dedupe_key="over-capacity",
                **_create_kwargs(memory_key="new-key", content="new content"),
            )

        await _create_store(db, uid="other-user")
        first = await memory_service.create(
            db,
            uid="other-user",
            dedupe_key="other-create",
            **_create_kwargs(memory_key="occupied", content="occupied content"),
        )

    assert first.status == MemoryMutationStatus.ACCEPTED
    assert first.job_id is not None
    other_job = await _get_job(memory_database, uid="other-user", job_id=first.job_id)
    assert other_job is not None
    assert other_job.uid == "other-user"


@pytest.mark.asyncio
async def test_update_enforces_uid_expected_version_and_pending_mutation(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    async with memory_database() as db:
        await _create_store(db, uid="update-owner")
        await _create_store(db, uid="other-owner")
        record = await _create_record(
            db,
            uid="update-owner",
            memory_key="update-key",
            content="old update content",
        )
        await db.commit()
        memory_id = record.id
        assert memory_id is not None

        with pytest.raises(MemoryNotFoundError):
            await memory_service.update(
                db,
                uid="other-owner",
                dedupe_key="wrong-owner",
                memory_id=memory_id,
                expected_version=1,
                **_create_kwargs(content="wrong owner content", memory_key="wrong-owner-key"),
            )
        with pytest.raises(MemoryConflictError):
            await memory_service.update(
                db,
                uid="update-owner",
                dedupe_key="wrong-version",
                memory_id=memory_id,
                expected_version=2,
                **_create_kwargs(content="wrong version content", memory_key="wrong-version-key"),
            )

        first = await memory_service.update(
            db,
            uid="update-owner",
            dedupe_key="pending-update",
            memory_id=memory_id,
            expected_version=1,
            **_create_kwargs(content="pending content", memory_key="pending-key"),
        )
        with pytest.raises(MemoryConflictError):
            await memory_service.update(
                db,
                uid="update-owner",
                dedupe_key="second-pending-update",
                memory_id=memory_id,
                expected_version=1,
                **_create_kwargs(content="second pending content", memory_key="second-pending-key"),
            )

    assert first.status == MemoryMutationStatus.ACCEPTED
    current = await _get_record(memory_database, uid="update-owner", memory_id=memory_id)
    assert current is not None
    assert current.pending_mutation_job_id == first.job_id
    update_job = await _get_job(memory_database, uid="update-owner", job_id=first.job_id)
    assert update_job is not None
    assert update_job.payload["content_token_count"] == estimate_tokens(update_job.payload["content"])


@pytest.mark.asyncio
async def test_update_unchanged_ignores_source_evidence_and_does_not_search(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_search(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("update must not perform similarity search")

    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", unexpected_search)
    async with memory_database() as db:
        await _create_store(db, uid="unchanged-update-user")
        record = await _create_record(
            db,
            uid="unchanged-update-user",
            memory_key="same-key",
            content="same content",
            source_id="original-source",
            source_message_id=1,
            change_evidence="original evidence",
        )
        await db.commit()
        result = await memory_service.update(
            db,
            uid="unchanged-update-user",
            dedupe_key="unchanged-update",
            memory_id=record.id,
            expected_version=1,
            content="same content",
            memory_key="same-key",
            memory_type=LongTermMemoryType.FACT,
            change_evidence="new evidence",
            source_id="new-source",
            source_message_id=99,
        )

    assert result.status == MemoryMutationStatus.UNCHANGED
    assert result.record is not None
    assert result.record.id == record.id
    assert await _get_job(memory_database, uid="unchanged-update-user", job_id=1) is None


@pytest.mark.asyncio
async def test_update_suppress_current_is_immediate_and_cancel_preserves_suppression(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "suppress-update-user"
    migration_job_id = 900
    async with memory_database() as db:
        await _create_store(
            db,
            uid=uid,
            migration_job_id=migration_job_id,
            migration_status=LongTermMemoryMigrationStatus.BUILDING,
        )
        record = await _create_record(
            db,
            uid=uid,
            memory_key="suppress-key",
            content="old content",
            vector_item_id="memory-old-vector",
        )
        await db.commit()
        result = await memory_service.update(
            db,
            uid=uid,
            dedupe_key="suppress-request",
            memory_id=record.id,
            expected_version=1,
            suppress_current=True,
            **_create_kwargs(content="new content", memory_key="new-key"),
        )

    assert result.status == MemoryMutationStatus.ACCEPTED
    assert result.job_id is not None
    suppressed = await _get_record(memory_database, uid=uid, memory_id=record.id)
    assert suppressed is not None
    assert suppressed.suppress_recall is True
    assert suppressed.suppressed_by_job_id == result.job_id
    assert suppressed.pending_mutation_job_id == result.job_id
    deltas = await _get_deltas(memory_database, uid=uid, migration_job_id=migration_job_id)
    assert [(delta.sequence, delta.action, delta.snapshot["suppress_recall"]) for delta in deltas] == [(1, LongTermMemoryEmbeddingDeltaAction.SUPPRESS, True)]

    async with memory_database() as db:
        cancellation = await memory_job_manager.request_cancel(db, uid=uid, job_id=result.job_id)
    assert cancellation.accepted is True
    assert cancellation.changed is True
    cancelled = await _get_job(memory_database, uid=uid, job_id=result.job_id)
    current = await _get_record(memory_database, uid=uid, memory_id=record.id)
    assert cancelled is not None
    assert cancelled.status == LongTermMemoryMutationStatus.CANCELLED
    assert current is not None
    assert current.pending_mutation_job_id is None
    assert current.suppress_recall is True
    assert current.suppressed_by_job_id == result.job_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [LongTermMemoryMutationStatus.FAILED, LongTermMemoryMutationStatus.CANCELLED],
)
async def test_resume_current_only_resumes_failed_or_cancelled_job_and_writes_upsert_delta(
    memory_database: async_sessionmaker[AsyncSession],
    terminal_status: LongTermMemoryMutationStatus,
) -> None:
    uid = f"resume-{terminal_status.value}-user"
    migration_job_id = 910
    async with memory_database() as db:
        await _create_store(db, uid=uid, migration_job_id=migration_job_id)
        record = await _create_record(db, uid=uid, memory_key="resume-key", content="resume old")
        await db.commit()
        update_result = await memory_service.update(
            db,
            uid=uid,
            dedupe_key="resume-source-update",
            memory_id=record.id,
            expected_version=1,
            suppress_current=True,
            **_create_kwargs(content="resume new", memory_key="resume-new-key"),
        )

    assert update_result.job_id is not None
    if terminal_status == LongTermMemoryMutationStatus.CANCELLED:
        async with memory_database() as db:
            cancellation = await memory_job_manager.request_cancel(
                db,
                uid=uid,
                job_id=update_result.job_id,
            )
        assert cancellation.changed is True
    else:
        async with memory_database() as db:
            failed = await memory_job_crud.update_status(
                db,
                uid=uid,
                job_id=update_result.job_id,
                status=LongTermMemoryMutationStatus.FAILED,
                clear_active_mutation_key=True,
            )
        assert failed is not None

    async with memory_database() as db:
        resumed = await memory_service.resume_current(
            db,
            uid=uid,
            memory_id=record.id,
            expected_version=1,
        )

    assert resumed.status == MemoryMutationStatus.RESUMED
    current = await _get_record(memory_database, uid=uid, memory_id=record.id)
    assert current is not None
    assert current.pending_mutation_job_id is None
    assert current.suppress_recall is False
    assert current.suppressed_by_job_id is None
    deltas = await _get_deltas(memory_database, uid=uid, migration_job_id=migration_job_id)
    assert [delta.action for delta in deltas] == [
        LongTermMemoryEmbeddingDeltaAction.SUPPRESS,
        LongTermMemoryEmbeddingDeltaAction.UPSERT,
    ]
    assert deltas[-1].sequence == 2


@pytest.mark.asyncio
async def test_resume_current_rejects_nonterminal_pending_job(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "resume-pending-user"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        record = await _create_record(db, uid=uid, memory_key="resume-pending-key", content="old")
        await db.commit()
        update_result = await memory_service.update(
            db,
            uid=uid,
            dedupe_key="resume-pending-update",
            memory_id=record.id,
            expected_version=1,
            suppress_current=True,
            **_create_kwargs(content="new", memory_key="resume-pending-new-key"),
        )
        with pytest.raises(MemoryConflictError):
            await memory_service.resume_current(
                db,
                uid=uid,
                memory_id=record.id,
                expected_version=1,
            )

    assert update_result.job_id is not None


@pytest.mark.asyncio
async def test_delete_tombstones_record_immediately_writes_delete_delta_and_cannot_be_cancelled(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "delete-user"
    migration_job_id = 920
    async with memory_database() as db:
        await _create_store(db, uid=uid, migration_job_id=migration_job_id)
        record = await _create_record(
            db,
            uid=uid,
            memory_key="delete-key",
            content="delete content",
            vector_item_id="delete-vector",
        )
        await db.commit()
        result = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="delete-request",
            memory_id=record.id,
            expected_version=1,
            source=LongTermMemorySource.LLM_TOOL,
            source_id="delete-source",
        )

    assert result.status == MemoryMutationStatus.ACCEPTED
    assert result.job_id is not None
    deleted = await _get_record(memory_database, uid=uid, memory_id=record.id)
    assert deleted is not None
    assert deleted.is_active is False
    assert deleted.deleted_at is not None
    assert deleted.pending_mutation_job_id == result.job_id
    async with memory_database() as db:
        assert await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=[record.id]) == []
        cancellation = await memory_job_manager.request_cancel(db, uid=uid, job_id=result.job_id)
    assert cancellation.accepted is False
    assert cancellation.changed is False
    job = await _get_job(memory_database, uid=uid, job_id=result.job_id)
    assert job is not None
    assert job.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP
    deltas = await _get_deltas(memory_database, uid=uid, migration_job_id=migration_job_id)
    assert len(deltas) == 1
    assert deltas[0].action == LongTermMemoryEmbeddingDeltaAction.DELETE
    assert deltas[0].snapshot["is_active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["memory", "source"])
async def test_delete_same_dedupe_changed_memory_or_source_conflicts_and_retry_reuses_job(
    memory_database: async_sessionmaker[AsyncSession],
    change: str,
) -> None:
    uid = "delete-dedupe-user"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        record = await _create_record(db, uid=uid, memory_key="delete-dedupe-key", content="delete dedupe")
        await db.commit()
        first = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="delete-dedupe",
            memory_id=record.id,
            expected_version=1,
            source_id="original-source",
        )
        retry = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="delete-dedupe",
            memory_id=record.id,
            expected_version=1,
            source_id="original-source",
        )
        with pytest.raises(MemoryConflictError):
            await memory_service.delete(
                db,
                uid=uid,
                dedupe_key="delete-dedupe",
                memory_id=record.id if change == "source" else record.id + 1000,
                expected_version=1,
                source_id="changed-source" if change == "source" else "original-source",
            )

    assert first.status == MemoryMutationStatus.ACCEPTED
    assert retry.status == MemoryMutationStatus.ACCEPTED
    assert retry.job_id == first.job_id
    job = await _get_job(memory_database, uid=uid, job_id=first.job_id)
    assert job is not None
    assert job.payload["source_id"] == "original-source"


@pytest.mark.asyncio
async def test_delete_new_dedupe_on_already_deleted_record_is_unchanged_and_uid_isolated(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    async with memory_database() as db:
        await _create_store(db, uid="deleted-owner")
        await _create_store(db, uid="other-delete-owner")
        record = await _create_record(db, uid="deleted-owner", memory_key="deleted-key", content="deleted")
        await db.commit()
        memory_id = record.id
        assert memory_id is not None
        deleted = await memory_service.delete(
            db,
            uid="deleted-owner",
            dedupe_key="delete-once",
            memory_id=memory_id,
            expected_version=1,
        )
        unchanged = await memory_service.delete(
            db,
            uid="deleted-owner",
            dedupe_key="delete-again",
            memory_id=memory_id,
            expected_version=1,
        )
        with pytest.raises(MemoryNotFoundError):
            await memory_service.delete(
                db,
                uid="other-delete-owner",
                dedupe_key="other-user-delete",
                memory_id=memory_id,
                expected_version=1,
            )

    assert deleted.status == MemoryMutationStatus.ACCEPTED
    assert unchanged.status == MemoryMutationStatus.UNCHANGED
    current = await _get_record(memory_database, uid="deleted-owner", memory_id=memory_id)
    assert current is not None
    assert current.is_active is False
    assert await _get_job(memory_database, uid="deleted-owner", job_id=unchanged.job_id or 0) is None


@pytest.mark.asyncio
async def test_delete_requires_expected_version_before_read_and_rejects_stale_active_record(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "delete-version-user"
    async with memory_database() as db:
        await _create_store(db, uid=uid)
        record = await _create_record(db, uid=uid, version=2, memory_key="version-key", content="version content")
        await db.commit()
        memory_id = record.id
        assert memory_id is not None

        async def unexpected_read(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("missing expected_version must fail before reading the record")

        with monkeypatch.context() as patch:
            patch.setattr(memory_service_module.memory_job_manager, "get_job_by_dedupe_key", unexpected_read)
            with pytest.raises(MemoryValidationError):
                await memory_service.delete(
                    db,
                    uid=uid,
                    dedupe_key="missing-version",
                    memory_id=memory_id,
                    expected_version=None,
                )

        with pytest.raises(MemoryConflictError) as exc_info:
            await memory_service.delete(
                db,
                uid=uid,
                dedupe_key="stale-version",
                memory_id=memory_id,
                expected_version=1,
            )

    assert exc_info.value.message == ERR_MEMORY_VERSION_CONFLICT
    current = await _get_record(memory_database, uid=uid, memory_id=memory_id)
    assert current is not None
    assert current.version == 2
    assert current.is_active is True
    assert current.deleted_at is None
    assert current.pending_mutation_job_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migration_status",
    [
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
        LongTermMemoryMigrationStatus.SUCCEEDED,
        LongTermMemoryMigrationStatus.FAILED,
        LongTermMemoryMigrationStatus.CANCELLED,
    ],
)
async def test_append_memory_embedding_delta_only_appends_during_active_migration(
    memory_database: async_sessionmaker[AsyncSession],
    migration_status: LongTermMemoryMigrationStatus,
) -> None:
    uid = f"delta-{migration_status.value}"
    migration_job_id = 930
    async with memory_database() as db:
        store = await _create_store(
            db,
            uid=uid,
            migration_job_id=migration_job_id,
            migration_status=migration_status,
        )
        delta = await append_memory_embedding_delta(
            db,
            store=store,
            action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
            memory_id=1,
            memory_version=1,
            source_mutation_job_id=2,
            snapshot={"version": 1, "vector_item_id": "memory_1_v1"},
            commit=True,
        )

    if migration_status in {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
    }:
        assert delta is not None
        assert delta.sequence == 1
        assert delta.snapshot["version"] == 1
        deltas = await _get_deltas(memory_database, uid=uid, migration_job_id=migration_job_id)
        assert len(deltas) == 1
        async with memory_database() as db:
            current_store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert current_store is not None
        assert current_store.migration_delta_high_watermark == 1
    else:
        assert delta is None
        assert await _get_deltas(memory_database, uid=uid, migration_job_id=migration_job_id) == []


@pytest.mark.asyncio
async def test_append_memory_embedding_delta_rejects_uid_in_snapshot(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    async with memory_database() as db:
        store = await _create_store(
            db,
            uid="delta-snapshot-user",
            migration_job_id=940,
            migration_status=LongTermMemoryMigrationStatus.PREPARING,
        )
        with pytest.raises(MemoryValidationError):
            await append_memory_embedding_delta(
                db,
                store=store,
                action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
                memory_id=1,
                memory_version=1,
                source_mutation_job_id=None,
                snapshot={"uid": "delta-snapshot-user", "version": 1},
            )
    assert await _get_deltas(memory_database, uid="delta-snapshot-user", migration_job_id=940) == []


class _RecallHit:
    def __init__(
        self,
        item_id: str,
        metadata: dict[str, Any],
        *,
        dense_distance: float | None = None,
        dense_rank: int | None = None,
        sparse_score: float | None = None,
        sparse_rank: int | None = None,
        fusion_score: float | None = None,
    ) -> None:
        self.id = item_id
        self.metadata = metadata
        self.dense_distance = dense_distance
        self.dense_rank = dense_rank
        self.sparse_score = sparse_score
        self.sparse_rank = sparse_rank
        self.fusion_score = fusion_score


def _hit(
    memory_id: int,
    *,
    uid: str,
    embedding_revision: int = 1,
    version: int = 1,
    item_id: str | None = None,
    fusion_score: float | None = None,
) -> _RecallHit:
    return _RecallHit(
        item_id or build_memory_vector_item_id(memory_id, version),
        {
            "uid": uid,
            "memory_id": memory_id,
            "version": version,
            "embedding_revision": embedding_revision,
        },
        fusion_score=fusion_score,
    )


async def _configure_recall_user(
    db: AsyncSession,
    *,
    uid: str,
    content: str = "recall content",
) -> LongTermMemoryRecord:
    await _create_store(db, uid=uid)
    record = await _create_record(db, uid=uid, memory_key="recall-key", content=content)
    await db.commit()
    return record


@pytest.mark.asyncio
async def test_recall_returns_not_configured_or_empty_without_external_calls(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def unexpected_loader(*_args: Any, **_kwargs: Any) -> None:
        calls.append("load")
        raise AssertionError("embedding runtime must not load")

    async def unexpected_embed(*_args: Any, **_kwargs: Any) -> None:
        calls.append("embed")
        raise AssertionError("embedding must not run")

    async def unexpected_query(*_args: Any, **_kwargs: Any) -> None:
        calls.append("query")
        raise AssertionError("hybrid query must not run")

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", unexpected_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", unexpected_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", unexpected_query)

    async with memory_database() as db:
        not_configured = await memory_service.recall(db, uid="missing-recall-user", query="query")
        await _create_store(db, uid="empty-recall-user")
        empty = await memory_service.recall(db, uid="empty-recall-user", query="query")

    assert not_configured.status == MemoryRecallStatus.NOT_CONFIGURED
    assert empty.status == MemoryRecallStatus.EMPTY
    assert calls == []


@pytest.mark.asyncio
async def test_recall_filters_metadata_and_database_state_while_preserving_fusion_order(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "recall-filter-user"
    async with memory_database() as db:
        valid_first = await _configure_recall_user(db, uid=uid, content="valid first")
        valid_second = await _create_record(db, uid=uid, memory_key="valid-second", content="valid second")
        cross_uid = await _create_record(db, uid="other-recall-user", memory_key="cross", content="cross uid")
        deleted = await _create_record(db, uid=uid, memory_key="deleted", content="deleted", is_active=False, deleted=True)
        suppressed = await _create_record(db, uid=uid, memory_key="suppressed", content="suppressed", suppress_recall=True)
        failed_index = await _create_record(
            db,
            uid=uid,
            memory_key="failed-index",
            content="failed index",
            index_status=LongTermMemoryRecordIndexStatus.FAILED,
        )
        old_index = await _create_record(
            db,
            uid=uid,
            memory_key="old-index",
            content="old index",
            indexed_version=0,
        )
        await db.commit()

    async def fake_loader(_db: AsyncSession, channel_id: int, model_id: str) -> object:
        assert channel_id == 7
        assert model_id == "memory-embedding-model"
        return object()

    async def fake_embed(_config: object, texts: list[str], **kwargs: Any) -> list[list[float]]:
        assert texts == ["normalized query"]
        assert kwargs["dimensions"] == 2
        return [[0.1, 0.2]]

    hits = [
        _hit(valid_second.id, uid=uid, fusion_score=0.9),
        _hit(cross_uid.id, uid="different-user", fusion_score=0.8),
        _hit(valid_first.id, uid=uid, embedding_revision=2, fusion_score=0.7),
        _hit(valid_first.id, uid=uid, item_id="wrong-item-id", fusion_score=0.6),
        _hit(deleted.id, uid=uid, fusion_score=0.5),
        _hit(suppressed.id, uid=uid, fusion_score=0.4),
        _hit(failed_index.id, uid=uid, fusion_score=0.3),
        _hit(old_index.id, uid=uid, fusion_score=0.2),
        _hit(valid_first.id, uid=uid, fusion_score=0.1),
    ]

    async def fake_query(collection_name: str, vector: list[float], query: str, limit: int) -> list[_RecallHit]:
        assert collection_name.startswith("memory-test-")
        assert vector == [0.1, 0.2]
        assert query == "normalized query"
        assert limit == 10
        return hits

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", fake_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", fake_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", fake_query)

    async with memory_database() as db:
        result = await memory_service.recall(
            db,
            uid=uid,
            query="  normalized\tquery ",
            top_k=2,
            candidate_k=10,
        )

    assert result.status == MemoryRecallStatus.OK
    assert [item.memory_id for item in result.items] == [valid_second.id, valid_first.id]
    assert [item.content for item in result.items] == ["valid second", "valid first"]
    assert result.items[0].fusion_score == 0.9
    assert result.items[1].fusion_score == 0.1

    recalled_second = await _get_record(memory_database, uid=uid, memory_id=valid_second.id)
    recalled_first = await _get_record(memory_database, uid=uid, memory_id=valid_first.id)
    not_recalled = await _get_record(memory_database, uid=uid, memory_id=deleted.id)
    cross_uid_record = await _get_record(memory_database, uid="other-recall-user", memory_id=cross_uid.id)
    assert recalled_second is not None and recalled_second.last_recalled_at is not None
    assert recalled_first is not None and recalled_first.last_recalled_at is not None
    assert not_recalled is not None and not_recalled.last_recalled_at is None
    assert cross_uid_record is not None and cross_uid_record.last_recalled_at is None


@pytest.mark.asyncio
async def test_touch_last_recalled_at_updates_only_the_requested_uid_and_preserves_updated_at(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    async with memory_database() as db:
        first = await _create_record(db, uid="touch-owner", memory_key="first", content="first")
        other = await _create_record(db, uid="touch-other", memory_key="other", content="other")
        await db.commit()
        first_updated_at = first.updated_at
        other_updated_at = other.updated_at

        updated_count = await memory_record_crud.touch_last_recalled_at(
            db,
            uid="touch-owner",
            memory_ids={first.id, other.id},
            commit=True,
        )

    assert updated_count == 1
    touched = await _get_record(memory_database, uid="touch-owner", memory_id=first.id)
    untouched = await _get_record(memory_database, uid="touch-other", memory_id=other.id)
    assert touched is not None
    assert touched.last_recalled_at is not None
    assert touched.updated_at == first_updated_at
    assert untouched is not None
    assert untouched.last_recalled_at is None
    assert untouched.updated_at == other_updated_at


@pytest.mark.asyncio
async def test_recall_touch_failure_returns_the_original_ok_result(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "recall-touch-failure-user"
    async with memory_database() as db:
        record = await _configure_recall_user(db, uid=uid)

    async def fake_loader(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_embed(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        return [[0.1, 0.2]]

    async def fake_query(*_args: Any, **_kwargs: Any) -> list[_RecallHit]:
        return [_hit(record.id, uid=uid)]

    async def failed_touch(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("touch unavailable")

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", fake_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", fake_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", fake_query)
    monkeypatch.setattr(memory_service_module.memory_record_crud, "touch_last_recalled_at", failed_touch)

    async with memory_database() as db:
        result = await memory_service.recall(db, uid=uid, query="touch failure")

    assert result.status == MemoryRecallStatus.OK
    assert [item.memory_id for item in result.items] == [record.id]
    assert result.error_key is None


@pytest.mark.asyncio
async def test_pin_and_unpin_are_uid_scoped_idempotent_and_do_not_change_publication_state(
    memory_database: async_sessionmaker[AsyncSession],
) -> None:
    async with memory_database() as db:
        owner_record = await _create_record(
            db,
            uid="pin-owner",
            memory_key="owner-key",
            content="owner content",
            version=3,
            pending_mutation_job_id=812,
        )
        foreign_record = await _create_record(
            db,
            uid="pin-foreign",
            memory_key="foreign-key",
            content="foreign content",
        )
        inactive_record = await _create_record(
            db,
            uid="pin-owner",
            memory_key="inactive-key",
            content="inactive content",
            is_active=False,
        )
        deleted_record = await _create_record(
            db,
            uid="pin-owner",
            memory_key="deleted-key",
            content="deleted content",
            is_active=False,
            deleted=True,
        )
        await db.commit()

        owner_memory_id = owner_record.id
        foreign_memory_id = foreign_record.id
        inactive_memory_id = inactive_record.id
        deleted_memory_id = deleted_record.id

        pinned = await memory_service.pin(db, "pin-owner", owner_memory_id)
        pinned_state = pinned.pinned
        pinned_updated_at = pinned.updated_at
        pinned_again = await memory_service.pin(db, "pin-owner", owner_memory_id)
        pinned_again_state = pinned_again.pinned
        pinned_again_updated_at = pinned_again.updated_at
        unpinned = await memory_service.unpin(db, "pin-owner", owner_memory_id)
        unpinned_state = unpinned.pinned

        for foreign_uid, memory_id in (
            ("pin-foreign", owner_memory_id),
            ("pin-owner", inactive_memory_id),
            ("pin-owner", deleted_memory_id),
        ):
            with pytest.raises(MemoryNotFoundError):
                await memory_service.pin(db, foreign_uid, memory_id)

        assert pinned_state is True
        assert pinned_again_state is True
        assert pinned_again_updated_at == pinned_updated_at
        assert unpinned_state is False

    owner_after = await _get_record(memory_database, uid="pin-owner", memory_id=owner_memory_id)
    foreign_after = await _get_record(memory_database, uid="pin-foreign", memory_id=foreign_memory_id)
    assert owner_after is not None
    assert (owner_after.version, owner_after.content, owner_after.pending_mutation_job_id) == (3, "owner content", 812)
    assert owner_after.pinned is False
    assert foreign_after is not None and foreign_after.pinned is False


@pytest.mark.asyncio
async def test_recall_applies_top_k_and_total_character_budget_by_truncating_last_item(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "recall-budget-user"
    async with memory_database() as db:
        first = await _configure_recall_user(db, uid=uid, content="a" * 200)
        second = await _create_record(db, uid=uid, memory_key="budget-second", content="b" * 200)
        await db.commit()

    async def fake_loader(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_embed(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        return [[0.1, 0.2]]

    async def fake_query(*_args: Any, **_kwargs: Any) -> list[_RecallHit]:
        return [_hit(first.id, uid=uid), _hit(second.id, uid=uid)]

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", fake_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", fake_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", fake_query)

    async with memory_database() as db:
        result = await memory_service.recall(
            db,
            uid=uid,
            query="budget",
            top_k=2,
            candidate_k=2,
            result_max_chars=256,
        )

    assert result.status == MemoryRecallStatus.OK
    assert len(result.items) == 2
    assert len(result.items[0].content) == 200
    assert len(result.items[1].content) == 56
    assert result.items[0].truncated is False
    assert result.items[1].truncated is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "dimension"])
async def test_recall_embedding_failure_and_dimension_mismatch_are_degraded(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    uid = f"recall-failure-{failure}"
    async with memory_database() as db:
        await _configure_recall_user(db, uid=uid)

    async def fake_loader(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_embed(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        if failure == "exception":
            raise RuntimeError("embedding unavailable")
        return [[0.1]]

    async def unexpected_query(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise AssertionError("degraded recall must not query the collection")

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", fake_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", fake_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", unexpected_query)

    async with memory_database() as db:
        result = await memory_service.recall(db, uid=uid, query="failure")

    assert result.status == MemoryRecallStatus.DEGRADED
    if failure == "exception":
        assert result.error_key == "ERR_MEMORY_RECALL_UNAVAILABLE"
    else:
        assert result.error_key == "ERR_MEMORY_EMBEDDING_DIMENSION_INVALID"


@pytest.mark.asyncio
async def test_recall_returns_degraded_when_active_configuration_changes_after_external_query(
    memory_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "recall-config-change-user"
    async with memory_database() as db:
        record = await _configure_recall_user(db, uid=uid)

    async def fake_loader(*_args: Any, **_kwargs: Any) -> object:
        return object()

    async def fake_embed(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        return [[0.1, 0.2]]

    async def query_then_change(*_args: Any, **_kwargs: Any) -> list[_RecallHit]:
        async with memory_database() as change_db:
            changed = await memory_store_crud.update_by_uid(
                change_db,
                uid=uid,
                active_collection_name="changed-after-query",
                active_embedding_signature="changed-signature",
                active_embedding_revision=2,
            )
            assert changed is not None
        return [_hit(record.id, uid=uid)]

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", fake_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", fake_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", query_then_change)

    async with memory_database() as db:
        result = await memory_service.recall(db, uid=uid, query="config change")

    assert result.status == MemoryRecallStatus.DEGRADED
    assert result.error_key == "ERR_MEMORY_RECALL_UNAVAILABLE"
