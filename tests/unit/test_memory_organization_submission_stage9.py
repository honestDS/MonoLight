from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.core.crypto as crypto_module
from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_TARGET_BUSY,
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED,
)
from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_record_crud, memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.memory import (
    build_memory_active_mutation_key,
    build_memory_organization_active_mutation_key,
)
from app.core.memory.normalization import build_memory_content_hash
from app.core.memory.organization import (
    MemoryOrganizationPlanInvalidError,
    MemoryOrganizationSnapshotItem,
    build_organization_dedupe_key,
    build_organization_execution_request,
    build_organization_snapshot_digest,
    validate_organization_model_output,
)
from app.core.memory_jobs.manager import (
    MemoryJobManager,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
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
    LongTermMemoryType,
)

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
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=ORGANIZATION_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _chat_model(*, model_id: str = "organization-model", max_tokens: int = 20_000) -> dict[str, object]:
    return {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 64,
        "max_tokens": max_tokens,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
        "description": "organization model",
        "advanced_settings": {"custom_headers": {"x-stage": "stage9"}},
    }


async def _create_channel(
    db: AsyncSession,
    *,
    model_id: str = "organization-model",
    max_tokens: int = 20_000,
) -> ModelChannel:
    return await channel_crud.create_with_plain_api_key(
        db,
        obj_in=ChannelCreate(
            name=f"organization-channel-{model_id}-{uuid4().hex[:8]}",
            api_key="organization-api-key",
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
    auto_organize_enabled: bool = False,
    index_status: LongTermMemoryIndexStatus = LongTermMemoryIndexStatus.READY,
    migration_status: LongTermMemoryMigrationStatus | None = None,
    active_embedding_revision: int = 3,
    index_revision: int = 4,
    **values: object,
) -> LongTermMemoryStore:
    return await memory_store_crud.create(
        db,
        uid=uid,
        active_embedding_channel_id=7,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=3,
        active_embedding_signature="embedding-signature",
        active_embedding_revision=active_embedding_revision,
        active_collection_name=f"memory-{uid}",
        index_revision=index_revision,
        index_status=index_status,
        migration_status=migration_status,
        organization_channel_id=channel_id,
        organization_model_id=model_id,
        auto_organize_enabled=auto_organize_enabled,
        **values,
    )


async def _create_record(
    db: AsyncSession,
    *,
    uid: str,
    memory_key: str,
    content: str,
    memory_type: LongTermMemoryType = LongTermMemoryType.FACT,
    version: int = 1,
    indexed_version: int | None = None,
    is_active: bool = True,
    deleted: bool = False,
    suppress_recall: bool = False,
    index_status: LongTermMemoryRecordIndexStatus = LongTermMemoryRecordIndexStatus.READY,
    vector_item_id: str | None = None,
    with_vector: bool = True,
    pinned: bool = False,
) -> LongTermMemoryRecord:
    if indexed_version is None:
        indexed_version = version if is_active else 0
    if vector_item_id is None and is_active and with_vector:
        vector_item_id = f"vector-{uid}-{memory_key}"
    return await memory_record_crud.create(
        db,
        uid=uid,
        memory_key=memory_key,
        memory_type=memory_type,
        content=content,
        content_token_count=estimate_tokens(content),
        content_hash=build_memory_content_hash(content),
        version=version,
        indexed_version=indexed_version,
        vector_item_id=vector_item_id,
        is_active=is_active,
        suppress_recall=suppress_recall,
        pinned=pinned,
        deleted_at=datetime.now(UTC) if deleted else None,
        index_status=index_status,
        commit=False,
    )


async def _create_ready_setup(
    db: AsyncSession,
    *,
    uid: str,
    auto_organize_enabled: bool = False,
    max_tokens: int = 20_000,
) -> tuple[ModelChannel, LongTermMemoryStore]:
    channel = await _create_channel(db, max_tokens=max_tokens)
    assert channel.id is not None
    store = await _create_store(
        db,
        uid=uid,
        channel_id=channel.id,
        model_id="organization-model",
        auto_organize_enabled=auto_organize_enabled,
    )
    return channel, store


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _all_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


@pytest.mark.asyncio
async def test_empty_snapshot_is_submittable_without_auto_organize_and_freezes_payload(
    db_session: AsyncSession,
) -> None:
    _, store = await _create_ready_setup(db_session, uid="empty-organization-user")
    manager = MemoryJobManager()

    submission = await manager.submit_organization(db_session, uid=store.uid)

    assert submission.created
    assert submission.job.operation == LongTermMemoryMutationOperation.ORGANIZE
    assert submission.job.memory_id is None
    assert submission.job.expected_version is None
    assert submission.job.source_session_id is None
    assert submission.job.source_profile_id is None
    assert submission.job.source_message_id is None
    assert submission.job.active_mutation_key == build_memory_organization_active_mutation_key(store.uid)
    assert set(submission.job.payload) == {"trigger", "snapshot", "organization_model"}
    assert submission.job.payload["trigger"] == "manual"
    assert submission.job.payload["snapshot"]["count"] == 0
    assert submission.job.payload["snapshot"]["items"] == []
    organization_model = submission.job.payload["organization_model"]
    assert set(organization_model) == {
        "channel_id",
        "channel_name",
        "model_id",
        "usage",
        "protocol",
        "base_url",
        "api_key",
        "http_proxy",
        "custom_headers",
        "temperature",
        "top_p",
        "timeout",
        "context_window_k",
        "context_window_tokens",
        "max_tokens",
        "snapshot_count",
        "required_output_tokens",
        "policy_version",
    }
    assert organization_model["api_key"] == "organization-api-key"
    assert organization_model["http_proxy"] == "http://proxy.example:8080"
    assert organization_model["custom_headers"] == {"x-stage": "stage9"}
    assert organization_model["snapshot_count"] == 0
    assert organization_model["policy_version"] == store.organization_policy_version
    assert json.dumps(submission.job.payload)
    assert not _all_keys(submission.job.payload) & {"uid", "session", "profile", "message"}
    assert store.auto_organize_enabled is False

    duplicate = await manager.submit_organization(db_session, uid=store.uid)
    assert not duplicate.created
    assert duplicate.job.id == submission.job.id

    assert submission.job.id is not None
    terminal = await memory_job_crud.update_status(
        db_session,
        uid=store.uid,
        job_id=submission.job.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        clear_active_mutation_key=True,
    )
    assert terminal is not None
    assert terminal.active_mutation_key is None

    after_terminal = await manager.submit_organization(db_session, uid=store.uid)
    assert not after_terminal.created
    assert after_terminal.job.id == submission.job.id


@pytest.mark.asyncio
async def test_snapshot_filters_ready_records_sorts_and_isolates_uid(
    db_session: AsyncSession,
) -> None:
    _, store = await _create_ready_setup(db_session, uid="snapshot-user")
    valid_first = await _create_record(
        db_session,
        uid=store.uid,
        memory_key="z-last-key",
        content="Pinned preference content",
        memory_type=LongTermMemoryType.PREFERENCE,
        version=2,
        pinned=True,
    )
    valid_second = await _create_record(
        db_session,
        uid=store.uid,
        memory_key="a-first-key",
        content="Project content",
        memory_type=LongTermMemoryType.PROJECT,
        version=4,
    )
    await _create_record(
        db_session,
        uid=store.uid,
        memory_key="inactive",
        content="inactive",
        is_active=False,
    )
    await _create_record(
        db_session,
        uid=store.uid,
        memory_key="deleted",
        content="deleted",
        deleted=True,
    )
    await _create_record(
        db_session,
        uid=store.uid,
        memory_key="suppressed",
        content="suppressed",
        suppress_recall=True,
    )
    await _create_record(
        db_session,
        uid=store.uid,
        memory_key="not-ready",
        content="not ready",
        index_status=LongTermMemoryRecordIndexStatus.PENDING,
    )
    await _create_record(
        db_session,
        uid=store.uid,
        memory_key="wrong-version",
        content="wrong version",
        indexed_version=2,
    )
    await _create_record(
        db_session,
        uid=store.uid,
        memory_key="no-vector",
        content="no vector",
        with_vector=False,
    )
    await _create_record(
        db_session,
        uid="other-user",
        memory_key="other-user-key",
        content="other user content",
    )

    submission = await MemoryJobManager().submit_organization(db_session, uid=store.uid)
    items = submission.job.payload["snapshot"]["items"]

    assert [item["memory_id"] for item in items] == [valid_first.id, valid_second.id]
    assert items == [
        {
            "memory_id": valid_first.id,
            "expected_version": 2,
            "memory_key": "z-last-key",
            "memory_type": "preference",
            "content": "Pinned preference content",
            "content_token_count": estimate_tokens("Pinned preference content"),
            "pinned": True,
        },
        {
            "memory_id": valid_second.id,
            "expected_version": 4,
            "memory_key": "a-first-key",
            "memory_type": "project",
            "content": "Project content",
            "content_token_count": estimate_tokens("Project content"),
            "pinned": False,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("record_count", [0, 1, 45, 50, 51])
async def test_organization_submission_preserves_complete_snapshot_at_record_count_boundaries(
    db_session: AsyncSession,
    record_count: int,
) -> None:
    _, store = await _create_ready_setup(db_session, uid=f"snapshot-boundary-{record_count}", max_tokens=20_000)
    records: list[LongTermMemoryRecord] = []
    for index in range(record_count):
        records.append(
            await _create_record(
                db_session,
                uid=store.uid,
                memory_key=f"memory-{index:02d}",
                content=f"content {index}",
                memory_type=(LongTermMemoryType.PREFERENCE if index % 2 else LongTermMemoryType.FACT),
                version=index + 1,
                pinned=index % 2 == 1,
            )
        )

    submission = await MemoryJobManager().submit_organization(db_session, uid=store.uid)

    snapshot = submission.job.payload["snapshot"]
    expected_items = [
        {
            "memory_id": record.id,
            "expected_version": record.version,
            "memory_key": record.memory_key,
            "memory_type": record.memory_type.value,
            "content": record.content,
            "content_token_count": record.content_token_count,
            "pinned": record.pinned,
        }
        for record in sorted(records, key=lambda item: item.id or 0)
    ]
    assert snapshot["count"] == record_count
    assert len(snapshot["items"]) == record_count
    assert [item["memory_id"] for item in snapshot["items"]] == sorted(record.id for record in records)
    assert snapshot["items"] == expected_items


@pytest.mark.asyncio
async def test_organization_snapshot_and_model_output_are_uid_isolated(
    db_session: AsyncSession,
) -> None:
    target_uid = "organization-isolation-target"
    other_uid = "organization-isolation-other"
    _, store = await _create_ready_setup(db_session, uid=target_uid)
    target_record = await _create_record(
        db_session,
        uid=target_uid,
        memory_key="target-memory",
        content="target uid content",
        version=3,
        pinned=True,
    )
    other_record = await _create_record(
        db_session,
        uid=other_uid,
        memory_key="other-memory",
        content="other uid private content",
        version=7,
    )
    assert target_record.id is not None
    assert other_record.id is not None

    submission = await MemoryJobManager().submit_organization(db_session, uid=target_uid)

    snapshot_items = submission.job.payload["snapshot"]["items"]
    assert snapshot_items == [
        {
            "memory_id": target_record.id,
            "expected_version": target_record.version,
            "memory_key": target_record.memory_key,
            "memory_type": target_record.memory_type.value,
            "content": target_record.content,
            "content_token_count": target_record.content_token_count,
            "pinned": target_record.pinned,
        }
    ]
    payload_text = json.dumps(submission.job.payload, ensure_ascii=False)
    assert other_record.content not in payload_text

    request = build_organization_execution_request(submission.job.payload)
    user_items = json.loads(request.messages[1].content or "")
    assert user_items == snapshot_items
    assert other_record.content not in request.messages[1].content

    model_output = json.dumps(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {
                        "memory_id": other_record.id,
                        "expected_version": other_record.version,
                    },
                }
            ]
        },
        separators=(",", ":"),
    )
    with pytest.raises(MemoryOrganizationPlanInvalidError) as exc_info:
        validate_organization_model_output(model_output, request.snapshot)

    assert "source_unknown_memory_id" in {error["code"] for error in exc_info.value.validation_errors}
    assert other_record.content not in json.dumps(exc_info.value.data, ensure_ascii=False)


def test_snapshot_digest_and_dedupe_key_bind_snapshot_identity() -> None:
    first = MemoryOrganizationSnapshotItem(
        memory_id=1,
        expected_version=2,
        memory_key="first",
        memory_type=LongTermMemoryType.FACT,
        content="first content",
        content_token_count=2,
        pinned=False,
    )
    second = MemoryOrganizationSnapshotItem(
        memory_id=2,
        expected_version=1,
        memory_key="second",
        memory_type=LongTermMemoryType.CONSTRAINT,
        content="second content",
        content_token_count=2,
        pinned=True,
    )
    digest = build_organization_snapshot_digest(
        [second, first],
        active_embedding_revision=3,
        index_revision=4,
        policy_version=1,
    )
    same_digest = build_organization_snapshot_digest(
        [first, second],
        active_embedding_revision=3,
        index_revision=4,
        policy_version=1,
    )
    changed_digest = build_organization_snapshot_digest(
        [first.model_copy(update={"pinned": True}), second],
        active_embedding_revision=3,
        index_revision=4,
        policy_version=1,
    )
    changed_version_digest = build_organization_snapshot_digest(
        [first.model_copy(update={"expected_version": 3}), second],
        active_embedding_revision=3,
        index_revision=4,
        policy_version=1,
    )
    changed_embedding_revision_digest = build_organization_snapshot_digest(
        [first, second],
        active_embedding_revision=4,
        index_revision=4,
        policy_version=1,
    )

    assert digest == same_digest
    assert digest != changed_digest
    assert digest != changed_version_digest
    assert digest != changed_embedding_revision_digest
    assert build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=1) == build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=1)
    assert build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=1) != build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=2)
    assert build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=1, caller_dedupe_key="manual-a") != build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=1, caller_dedupe_key="manual-b")
    assert len(build_organization_dedupe_key("snapshot-user", snapshot_digest=digest, policy_version=1)) <= 255
    assert build_memory_organization_active_mutation_key("snapshot-user") != build_memory_active_mutation_key("snapshot-user", memory_id=1)


@pytest.mark.asyncio
async def test_organization_rejects_missing_store_or_model_configuration(
    db_session: AsyncSession,
) -> None:
    manager = MemoryJobManager()

    with pytest.raises(ParameterException) as missing_store:
        await manager.submit_organization(db_session, uid="missing-store")
    assert missing_store.value.message == ERR_MEMORY_NOT_CONFIGURED

    await _create_store(
        db_session,
        uid="missing-active-config",
        channel_id=None,
        model_id=None,
        active_embedding_revision=0,
    )
    with pytest.raises(ParameterException) as missing_active_config:
        await manager.submit_organization(db_session, uid="missing-active-config")
    assert missing_active_config.value.message == ERR_MEMORY_NOT_CONFIGURED

    await _create_store(
        db_session,
        uid="missing-model",
        channel_id=None,
        model_id=None,
    )
    with pytest.raises(ParameterException) as missing_model:
        await manager.submit_organization(db_session, uid="missing-model")
    assert missing_model.value.message == ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED

    await _create_store(
        db_session,
        uid="invalid-model",
        channel_id=999,
        model_id="missing-channel-model",
    )
    with pytest.raises(ParameterException) as invalid_model:
        await manager.submit_organization(db_session, uid="invalid-model")
    assert invalid_model.value.message == ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "maintenance_state",
    [
        (LongTermMemoryIndexStatus.REINDEXING, None),
        (LongTermMemoryIndexStatus.READY, LongTermMemoryMigrationStatus.PREPARING),
        (LongTermMemoryIndexStatus.READY, LongTermMemoryMigrationStatus.BUILDING),
        (LongTermMemoryIndexStatus.READY, LongTermMemoryMigrationStatus.CATCHING_UP),
        (LongTermMemoryIndexStatus.READY, LongTermMemoryMigrationStatus.VALIDATING),
        (LongTermMemoryIndexStatus.READY, LongTermMemoryMigrationStatus.SWITCHING),
    ],
)
async def test_organization_rejects_reindex_and_active_migration_states(
    db_session: AsyncSession,
    maintenance_state: tuple[LongTermMemoryIndexStatus, LongTermMemoryMigrationStatus | None],
) -> None:
    index_status, migration_status = maintenance_state
    uid = f"maintenance-{index_status.value}-{migration_status.value if migration_status else 'reindex'}"
    channel = await _create_channel(db_session)
    assert channel.id is not None
    await _create_store(
        db_session,
        uid=uid,
        channel_id=channel.id,
        model_id="organization-model",
        index_status=index_status,
        migration_status=migration_status,
    )

    with pytest.raises(ParameterException) as exc_info:
        await MemoryJobManager().submit_organization(db_session, uid=uid)
    assert exc_info.value.message == ERR_MEMORY_MAINTENANCE_STATE_CONFLICT


@pytest.mark.asyncio
async def test_organization_is_uid_scoped_and_different_dedupe_is_blocked_only_per_uid(
    db_session: AsyncSession,
) -> None:
    _, first_store = await _create_ready_setup(db_session, uid="active-organization-a")
    _, second_store = await _create_ready_setup(db_session, uid="active-organization-b")
    first_uid = first_store.uid
    second_uid = second_store.uid
    manager = MemoryJobManager()

    first = await manager.submit_organization(db_session, uid=first_uid, dedupe_key="first")
    first_job_id = first.job.id
    assert first_job_id is not None
    with pytest.raises(MemoryJobTargetBusyError) as busy:
        await manager.submit_organization(db_session, uid=first_uid, dedupe_key="second")
    assert busy.value.args[0]

    other_uid = await manager.submit_organization(db_session, uid=second_uid, dedupe_key="second")
    assert other_uid.created
    assert other_uid.job.id != first_job_id


@pytest.mark.asyncio
async def test_organization_does_not_reserve_records_and_update_can_follow(
    db_session: AsyncSession,
) -> None:
    _, store = await _create_ready_setup(db_session, uid="organization-update-user")
    record = await _create_record(
        db_session,
        uid=store.uid,
        memory_key="ordinary-update",
        content="ordinary update content",
    )
    manager = MemoryJobManager()
    organization = await manager.submit_organization(db_session, uid=store.uid)

    current = await memory_record_crud.get_by_id(db_session, uid=store.uid, memory_id=record.id)
    assert current is not None
    assert current.pending_mutation_job_id is None

    update = await manager.submit(
        db_session,
        uid=store.uid,
        operation=LongTermMemoryMutationOperation.UPDATE,
        dedupe_key="ordinary-update-job",
        active_mutation_key=build_memory_active_mutation_key(store.uid, memory_id=record.id),
        memory_id=record.id,
        expected_version=record.version,
        payload={"kind": "ordinary-update"},
    )
    assert update.created
    assert organization.job.id != update.job.id


@pytest.mark.asyncio
async def test_terminal_organization_with_changed_pinned_snapshot_creates_new_job_for_same_caller_dedupe(
    db_session: AsyncSession,
) -> None:
    _, store = await _create_ready_setup(db_session, uid="organization-pinned-identity-user")
    record = await _create_record(
        db_session,
        uid=store.uid,
        memory_key="pinned-identity",
        content="pinned identity content",
    )
    manager = MemoryJobManager()

    first = await manager.submit_organization(db_session, uid=store.uid, dedupe_key="same-caller-dedupe")
    assert first.job.id is not None
    assert first.job.payload["snapshot"]["items"][0]["pinned"] is False

    terminal = await memory_job_crud.update_status(
        db_session,
        uid=store.uid,
        job_id=first.job.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        clear_active_mutation_key=True,
    )
    assert terminal is not None
    assert terminal.active_mutation_key is None

    updated_record = await memory_record_crud.set_pinned(
        db_session,
        uid=store.uid,
        memory_id=record.id,
        pinned=True,
    )
    assert updated_record is not None
    assert updated_record.pinned is True

    second = await manager.submit_organization(db_session, uid=store.uid, dedupe_key="same-caller-dedupe")
    assert second.created
    assert second.job.id != first.job.id
    assert second.job.payload["snapshot"]["items"][0]["pinned"] is True


@pytest.mark.asyncio
async def test_generic_organization_submission_enforces_uid_key_and_source_boundaries(
    db_session: AsyncSession,
) -> None:
    manager = MemoryJobManager()
    uid = "generic-organization-user"
    organization_key = build_memory_organization_active_mutation_key(uid)

    with pytest.raises(MemoryJobValidationError):
        await manager.submit(
            db_session,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key="missing-key",
            payload={},
        )
    with pytest.raises(MemoryJobValidationError) as memory_id_error:
        await manager.submit(
            db_session,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key="memory-id",
            active_mutation_key=organization_key,
            memory_id=1,
            payload={},
        )
    assert memory_id_error.value.args[0]
    with pytest.raises(MemoryJobValidationError):
        await manager.submit(
            db_session,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key="expected-version",
            active_mutation_key=organization_key,
            expected_version=1,
            payload={},
        )
    with pytest.raises(MemoryJobValidationError) as source_error:
        await manager.submit(
            db_session,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key="source",
            active_mutation_key=organization_key,
            source_session_id="session-must-be-empty",
            payload={},
        )
    assert source_error.value.args[0]

    valid = await manager.submit(
        db_session,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key="valid-organize",
        active_mutation_key=organization_key,
        payload={},
    )
    assert valid.created


@pytest.mark.asyncio
async def test_organization_dedupe_identity_conflict_is_not_silently_reused(
    db_session: AsyncSession,
) -> None:
    manager = MemoryJobManager()
    first = await manager.submit(
        db_session,
        uid="dedupe-conflict-user",
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key="same-final-key",
        active_mutation_key=build_memory_organization_active_mutation_key("dedupe-conflict-user"),
        payload={"snapshot": {"count": 1}},
    )
    assert first.created

    with pytest.raises(MemoryJobValidationError) as exc_info:
        await manager.submit(
            db_session,
            uid="dedupe-conflict-user",
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key="same-final-key",
            active_mutation_key=build_memory_organization_active_mutation_key("dedupe-conflict-user"),
            payload={"snapshot": {"count": 2}},
        )
    assert str(exc_info.value) == t(ERR_MEMORY_JOB_DEDUPE_CONFLICT)
    assert t(ERR_MEMORY_JOB_ACTIVE_TARGET_BUSY) not in str(exc_info.value)
    assert t(ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN) not in str(exc_info.value)
