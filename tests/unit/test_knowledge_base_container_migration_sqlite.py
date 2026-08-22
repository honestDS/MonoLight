"""知识库容器 SQLite 迁移测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    insert,
    select,
    text,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDocument,
)
from scripts.knowledge_base_container_migration_sqlite import (
    sqlite_table_matches_target,
)
from scripts.migration_20260815_expand_knowledge_base_container import migrate

FIXED_DATETIME = datetime(2026, 8, 21, 2, 0, 0)


def _legacy_tables():
    metadata = MetaData()
    channel = Table("channel", metadata, Column("id", Integer, primary_key=True))
    profile = Table(
        "profile",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("uid", String(50), nullable=True),
    )
    knowledge_base = Table(
        "knowledge_base",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("uid", String(50), nullable=False),
        Column("name", String(100), nullable=False),
        Column("description", String(500), nullable=True),
        Column("embedding_channel_id", Integer, ForeignKey("channel.id", ondelete="RESTRICT"), nullable=False),
        Column("embedding_model_id", String(255), nullable=False),
        Column("embedding_dimensions", Integer, nullable=True),
        Column("collection_name", String(100), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=True),
        Column("updated_at", DateTime(timezone=True), nullable=True),
        Index("ix_knowledge_base_id", "id"),
        Index("ix_knowledge_base_uid", "uid"),
        Index("ix_knowledge_base_name", "name"),
        Index("ix_knowledge_base_embedding_channel_id", "embedding_channel_id"),
        Index("ix_knowledge_base_collection_name", "collection_name", unique=True),
    )
    return metadata, channel, profile, knowledge_base


LEGACY_METADATA, CHANNEL, PROFILE, KNOWLEDGE_BASE = _legacy_tables()
BINDING = Table(
    "knowledge_base_profile_binding",
    LEGACY_METADATA,
    Column("id", Integer, primary_key=True),
    Column(
        "knowledge_base_id",
        Integer,
        ForeignKey("knowledge_base.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "profile_id",
        Integer,
        ForeignKey("profile.id", ondelete="CASCADE"),
        nullable=False,
    ),
)
DOCUMENT = KnowledgeBaseDocument.__table__


@pytest.fixture()
def sqlite_database(tmp_path: Path):
    database_path = tmp_path / "knowledge-base-migration.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    LEGACY_METADATA.create_all(engine, tables=[CHANNEL, PROFILE, KNOWLEDGE_BASE])
    yield engine, database_path, KNOWLEDGE_BASE
    engine.dispose()


def _seed_parent_rows(engine):
    now = FIXED_DATETIME
    with engine.begin() as connection:
        channel_id = connection.execute(insert(CHANNEL).values()).inserted_primary_key[0]
        profile_uid = "profile-user"
        profile_id = connection.execute(insert(PROFILE).values(uid=profile_uid)).inserted_primary_key[0]
    return {"channel_id": channel_id, "profile_id": profile_id, "profile_uid": profile_uid, "now": now}


def _seed_old_knowledge_base(engine, parent):
    values = {
        "uid": f"kb-{uuid4().hex[:12]}",
        "name": "Legacy KB",
        "description": "Legacy description",
        "embedding_channel_id": parent["channel_id"],
        "embedding_model_id": "legacy-embedding-model",
        "embedding_dimensions": 1536,
        "collection_name": f"legacy-{uuid4().hex[:12]}",
        "created_at": parent["now"],
        "updated_at": parent["now"],
    }
    with engine.begin() as connection:
        row = connection.execute(insert(KNOWLEDGE_BASE).values(**values))
        values["id"] = row.inserted_primary_key[0]
    return values


def _create_old_children(engine, *, with_foreign_keys=True):
    with engine.begin() as connection:
        if with_foreign_keys:
            connection.execute(CreateTable(BINDING))
            connection.execute(CreateTable(DOCUMENT))
        else:
            connection.execute(CreateTable(BINDING, include_foreign_key_constraints=set()))
            connection.execute(CreateTable(DOCUMENT, include_foreign_key_constraints=set()))


def _document_values(kb_id):
    values = {
        "knowledge_base_id": kb_id,
        "filename": "legacy.md",
        "content": "Document body with preserved chunks",
        "chunk_size": 800,
        "chunk_overlap": 80,
        "batch_size": 32,
        "chunk_count": 2,
        "chunk_ids": ["chunk-1", "chunk-2"],
        "metadata": {"source": "sqlite", "page": 7},
    }
    now = FIXED_DATETIME
    for field in ("created_at", "updated_at"):
        column = DOCUMENT.c.get(field)
        if column is not None and not column.nullable:
            values[field] = now
    return values


class _FakeChromaCollection:
    def __init__(self, metadata_by_id):
        self.metadata_by_id = dict(metadata_by_id)
        self.ids = set(self.metadata_by_id)
        self.get_calls = []
        self.delete_calls = []

    def get(self, *, ids, include):
        requested_ids = list(ids)
        self.get_calls.append((requested_ids, list(include)))
        returned_ids = [chunk_id for chunk_id in requested_ids if chunk_id in self.ids]
        return {
            "ids": returned_ids,
            "metadatas": [self.metadata_by_id[chunk_id] for chunk_id in returned_ids],
        }

    def delete(self, *, ids):
        deleted_ids = list(ids)
        self.delete_calls.append(deleted_ids)
        self.ids.difference_update(deleted_ids)
        for chunk_id in deleted_ids:
            self.metadata_by_id.pop(chunk_id, None)


class _FakeChromaClient:
    def __init__(self, collections, *, list_collections_callback=None):
        self.collections = collections
        self.list_collections_callback = list_collections_callback

    def list_collections(self):
        if self.list_collections_callback is not None:
            self.list_collections_callback()
        return self.collections


def _seed_binding_and_document(engine, *, kb_id, profile_id):
    with engine.begin() as connection:
        binding_id = connection.execute(insert(BINDING).values(knowledge_base_id=kb_id, profile_id=profile_id)).inserted_primary_key[0]
        document_id = connection.execute(insert(DOCUMENT).values(**_document_values(kb_id))).inserted_primary_key[0]
    return binding_id, document_id


def _assert_document(engine, document_id, kb_id):
    with engine.connect() as connection:
        row = connection.execute(select(DOCUMENT).where(DOCUMENT.c.id == document_id)).mappings().one()
    assert (
        row["knowledge_base_id"],
        row["filename"],
        row["content"],
        row["chunk_size"],
        row["chunk_overlap"],
        row["batch_size"],
        row["chunk_count"],
        row["chunk_ids"],
        row["metadata"],
    ) == (
        kb_id,
        "legacy.md",
        "Document body with preserved chunks",
        800,
        80,
        32,
        2,
        ["chunk-1", "chunk-2"],
        {"source": "sqlite", "page": 7},
    )


def _seed_partial_upgrade(engine, legacy_id, parent, signature):
    definitions = (
        "knowledge_base_type VARCHAR(20)",
        "managed_profile_id INTEGER",
        "target_embedding_channel_id INTEGER",
        "target_embedding_model_id VARCHAR(255)",
        "target_embedding_dimensions INTEGER",
        "target_embedding_signature VARCHAR(128)",
        "target_embedding_revision INTEGER",
        "target_collection_name VARCHAR(255)",
        "migration_job_id INTEGER",
        "migration_status VARCHAR(20)",
        "index_revision INTEGER",
        "index_status VARCHAR(20)",
    )
    with engine.begin() as connection:
        for definition in definitions:
            connection.execute(text(f"ALTER TABLE knowledge_base ADD COLUMN {definition}"))
        connection.execute(
            text(
                "UPDATE knowledge_base SET uid=:uid, knowledge_base_type='LLM_MANAGED', managed_profile_id=:owner, "
                "target_embedding_channel_id=:channel, target_embedding_model_id='new-model', "
                "target_embedding_dimensions=3072, target_embedding_signature=:signature, "
                "target_embedding_revision=4, target_collection_name='managed-collection', "
                "migration_job_id=123, migration_status='building', index_revision=0, "
                "index_status='PENDING' WHERE id=:id"
            ),
            {
                "uid": parent["profile_uid"],
                "owner": parent["profile_id"],
                "channel": parent["channel_id"],
                "signature": signature,
                "id": legacy_id,
            },
        )


async def _migrate(database_path: Path):
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(async_engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_factory() as session:
        await migrate(session)
    await async_engine.dispose()


def _run_migration(database_path: Path):
    asyncio.run(_migrate(database_path))


def _state_snapshot(engine):
    columns = """knowledge_base_type, managed_profile_id, active_embedding_channel_id,
        active_embedding_model_id, active_embedding_dimensions, active_embedding_signature,
        active_embedding_revision, active_collection_name, target_embedding_channel_id,
        target_embedding_model_id, target_embedding_dimensions, target_embedding_signature,
        target_embedding_revision, target_collection_name, migration_job_id, migration_status,
        migration_snapshot_boundary, migration_cursor, migration_total_count,
        migration_success_count, migration_failure_count, migration_delta_high_watermark,
        migration_delta_applied_watermark, migration_error, migration_started_at,
        migration_finished_at, old_collection_name, old_collection_cleanup_status,
        old_collection_cleanup_job_id, old_collection_cleanup_error, old_collection_cleanup_at,
        index_revision, index_status"""
    with engine.connect() as connection:
        state = connection.execute(text(f"SELECT {columns} FROM knowledge_base")).mappings().one()
        counts = {table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() for table in ("knowledge_base_profile_binding", "knowledge_base_document")}
    return state, counts


def _enum_name(value):
    return str(getattr(value, "name", value)).upper()


def _enum_value(value):
    return str(getattr(value, "value", value)).lower()


def _assert_fields(entity, expected):
    for field, value in expected.items():
        assert getattr(entity, field) == value


def _assert_default_state(state, parent, legacy):
    assert _enum_name(state["knowledge_base_type"]) == "USER"
    assert state["managed_profile_id"] is None
    assert state["active_embedding_channel_id"] == parent["channel_id"]
    assert state["active_embedding_model_id"] == legacy["embedding_model_id"]
    assert state["active_embedding_dimensions"] == legacy["embedding_dimensions"]
    assert state["active_embedding_signature"] is None
    assert state["active_embedding_revision"] == 1
    assert state["active_collection_name"] == legacy["collection_name"]
    null_fields = (
        "target_embedding_channel_id target_embedding_model_id target_embedding_dimensions "
        "target_embedding_signature target_embedding_revision target_collection_name "
        "migration_job_id migration_status migration_snapshot_boundary migration_cursor "
        "migration_error migration_started_at migration_finished_at old_collection_name "
        "old_collection_cleanup_job_id old_collection_cleanup_error old_collection_cleanup_at"
    ).split()
    for field in null_fields:
        assert state[field] is None
    for field in (
        "migration_total_count",
        "migration_success_count",
        "migration_failure_count",
        "migration_delta_high_watermark",
        "migration_delta_applied_watermark",
    ):
        assert state[field] == 0
    assert _enum_name(state["old_collection_cleanup_status"]) == "NONE"
    assert state["index_revision"] == 1
    assert _enum_name(state["index_status"]) == "READY"


def _assert_sqlite_is_healthy(engine):
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []


def test_valid_legacy_data_is_preserved_and_upgrade_is_idempotent(sqlite_database):
    engine, database_path, knowledge_base = sqlite_database
    parent = _seed_parent_rows(engine)
    legacy = _seed_old_knowledge_base(engine, parent)
    _create_old_children(engine)
    _, document_id = _seed_binding_and_document(engine=engine, kb_id=legacy["id"], profile_id=parent["profile_id"])
    _run_migration(database_path)
    first_state, first_counts = _state_snapshot(engine)
    _run_migration(database_path)
    second_state, second_counts = _state_snapshot(engine)
    with engine.connect() as connection:
        row = connection.execute(select(knowledge_base)).mappings().one()
    for field in legacy:
        assert row[field] == legacy[field]
    _assert_default_state(first_state, parent, legacy)
    assert first_state == second_state
    assert (
        first_counts
        == second_counts
        == {
            "knowledge_base_profile_binding": 1,
            "knowledge_base_document": 1,
        }
    )
    _assert_document(engine, document_id, legacy["id"])
    _assert_sqlite_is_healthy(engine)
    with engine.connect() as connection:
        assert sqlite_table_matches_target(connection) is True


def test_orphan_bindings_and_documents_are_removed(sqlite_database, monkeypatch):
    engine, database_path, _ = sqlite_database
    parent = _seed_parent_rows(engine)
    legacy = _seed_old_knowledge_base(engine, parent)
    _create_old_children(engine, with_foreign_keys=False)
    valid_binding_id, valid_document_id = _seed_binding_and_document(engine=engine, kb_id=legacy["id"], profile_id=parent["profile_id"])
    orphan_document = _document_values(999991)
    orphan_document["content"] = "orphan document"
    orphan_chunk_ids = [
        "orphan-chunk-int",
        "orphan-chunk-string",
        "orphan-chunk-shared",
        "orphan-chunk-missing",
        "orphan-chunk-missing-field",
        "orphan-chunk-bool",
        "orphan-chunk-invalid",
        "orphan-chunk-other",
    ]
    orphan_document["chunk_ids"] = orphan_chunk_ids
    orphan_document["chunk_count"] = len(orphan_chunk_ids)
    with engine.begin() as connection:
        connection.execute(insert(BINDING).values(knowledge_base_id=legacy["id"], profile_id=999992))
        connection.execute(insert(BINDING).values(knowledge_base_id=999991, profile_id=parent["profile_id"]))
        connection.execute(insert(DOCUMENT).values(**orphan_document))
    orphan_collection = _FakeChromaCollection(
        {
            "orphan-chunk-int": {"knowledge_base_id": 999991},
            "orphan-chunk-string": {"knowledge_base_id": "999991"},
            "orphan-chunk-shared": {"knowledge_base_id": 999991},
            "orphan-chunk-missing": None,
            "orphan-chunk-missing-field": {"source": "missing-id"},
            "orphan-chunk-bool": {"knowledge_base_id": True},
            "orphan-chunk-invalid": {"knowledge_base_id": "not-an-id"},
            "orphan-chunk-other": {"knowledge_base_id": legacy["id"] + 1},
            "unrelated": {"knowledge_base_id": 999991},
        }
    )
    valid_collection = _FakeChromaCollection({"orphan-chunk-shared": {"knowledge_base_id": 999991}})
    collections = [orphan_collection, valid_collection]
    list_collections_calls = []

    chroma_client = _FakeChromaClient(collections, list_collections_callback=lambda: list_collections_calls.append(True))
    monkeypatch.setattr("app.providers.vector.get_chroma_client", lambda: chroma_client)
    _run_migration(database_path)
    with engine.connect() as connection:
        bindings = connection.execute(select(BINDING)).mappings().all()
        documents = connection.execute(select(DOCUMENT)).mappings().all()
    assert [(row["id"], row["knowledge_base_id"], row["profile_id"]) for row in bindings] == [(valid_binding_id, legacy["id"], parent["profile_id"])]
    assert [row["id"] for row in documents] == [valid_document_id]
    assert list_collections_calls == []
    assert orphan_collection.get_calls == []
    assert orphan_collection.delete_calls == []
    assert valid_collection.delete_calls == []
    assert valid_collection.get_calls == []
    assert orphan_collection.ids == set(orphan_chunk_ids) | {"unrelated"}
    assert valid_collection.ids == {"orphan-chunk-shared"}
    _assert_document(engine, valid_document_id, legacy["id"])
    _assert_sqlite_is_healthy(engine)


def test_partial_upgrade_columns_are_preserved(sqlite_database):
    engine, database_path, _ = sqlite_database
    parent = _seed_parent_rows(engine)
    legacy = _seed_old_knowledge_base(engine, parent)
    _create_old_children(engine)
    target_signature = "target-signature"
    _seed_partial_upgrade(engine, legacy["id"], parent, target_signature)
    _run_migration(database_path)
    with Session(engine) as session:
        migrated = session.get(KnowledgeBase, legacy["id"])
        assert migrated is not None
        assert _enum_name(migrated.knowledge_base_type) == "LLM_MANAGED"
        _assert_fields(
            migrated,
            {
                "managed_profile_id": parent["profile_id"],
                "target_embedding_channel_id": parent["channel_id"],
                "target_embedding_model_id": "new-model",
                "target_embedding_dimensions": 3072,
                "target_embedding_signature": target_signature,
                "target_embedding_revision": 4,
                "target_collection_name": "managed-collection",
                "migration_job_id": 123,
            },
        )
        assert _enum_value(migrated.migration_status) == "building"
        assert migrated.index_revision == 1
        assert _enum_name(migrated.index_status) == "READY"
        _assert_fields(
            migrated,
            {
                "active_embedding_channel_id": parent["channel_id"],
                "active_embedding_model_id": legacy["embedding_model_id"],
                "active_embedding_dimensions": legacy["embedding_dimensions"],
                "active_collection_name": legacy["collection_name"],
                "active_embedding_signature": None,
                "active_embedding_revision": 1,
            },
        )
    state, _ = _state_snapshot(engine)
    assert _enum_name(state["knowledge_base_type"]) == "LLM_MANAGED"
    assert state["managed_profile_id"] == parent["profile_id"]
    assert state["target_embedding_model_id"] == "new-model"
    assert _enum_name(state["migration_status"]) == "BUILDING"
    _assert_sqlite_is_healthy(engine)


def test_partial_upgrade_rejects_managed_profile_uid_mismatch(sqlite_database):
    engine, database_path, _ = sqlite_database
    parent = _seed_parent_rows(engine)
    legacy = _seed_old_knowledge_base(engine, parent)
    _create_old_children(engine)
    _seed_partial_upgrade(engine, legacy["id"], parent, "target-signature")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE knowledge_base SET uid=:uid WHERE id=:id"),
            {"uid": "different-user", "id": legacy["id"]},
        )

    with pytest.raises(RuntimeError) as error:
        _run_migration(database_path)

    assert "knowledge_base.managed_profile_owner->profile.id/uid" in str(error.value)


def test_migrated_managed_profile_foreign_key_rejects_uid_change(sqlite_database):
    engine, database_path, _ = sqlite_database
    parent = _seed_parent_rows(engine)
    legacy = _seed_old_knowledge_base(engine, parent)
    _create_old_children(engine)
    _seed_partial_upgrade(engine, legacy["id"], parent, "target-signature")
    _run_migration(database_path)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE knowledge_base SET uid=:uid WHERE id=:id"),
                {"uid": "different-user", "id": legacy["id"]},
            )

    _assert_sqlite_is_healthy(engine)
