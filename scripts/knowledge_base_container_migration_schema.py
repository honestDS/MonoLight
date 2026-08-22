from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)


class KnowledgeBaseType(StrEnum):
    USER = "user"
    LLM_MANAGED = "llm_managed"


class KnowledgeBaseMigrationStatus(StrEnum):
    PREPARING = "preparing"
    BUILDING = "building"
    CATCHING_UP = "catching_up"
    VALIDATING = "validating"
    SWITCHING = "switching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeBaseOldCollectionCleanupStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class KnowledgeBaseIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    REINDEXING = "reindexing"
    FAILED = "failed"


OWNER_CHECK_SQL: Final[str] = "(knowledge_base_type = 'USER' AND managed_profile_id IS NULL) OR (knowledge_base_type = 'LLM_MANAGED' AND managed_profile_id IS NOT NULL)"
PROFILE_OWNER_KEY_COLUMNS: Final[tuple[str, str]] = ("id", "uid")
PROFILE_OWNER_UNIQUE_NAME: Final[str] = "uq_profile_id_uid"
MANAGED_PROFILE_FOREIGN_KEY_COLUMNS: Final[tuple[str, str]] = ("managed_profile_id", "uid")
MANAGED_PROFILE_FOREIGN_KEY_NAME: Final[str] = "fk_kb_managed_profile"
MANAGED_PROFILE_FOREIGN_KEY_ONDELETE: Final[str] = "CASCADE"

REFERENCE_SPECS: Final[dict[str, str]] = {
    "channel": "channel.id",
    "profile": "profile.id",
}

FOREIGN_KEY_SPECS: Final[dict[str, dict[str, str]]] = {
    "embedding_channel_id": {
        "target": REFERENCE_SPECS["channel"],
        "ondelete": "RESTRICT",
        "name": "fk_kb_embedding_channel",
    },
    "active_embedding_channel_id": {
        "target": REFERENCE_SPECS["channel"],
        "ondelete": "RESTRICT",
        "name": "fk_kb_active_channel",
    },
    "target_embedding_channel_id": {
        "target": REFERENCE_SPECS["channel"],
        "ondelete": "RESTRICT",
        "name": "fk_kb_target_channel",
    },
}

LEGACY_COLUMN_NAMES: Final[tuple[str, ...]] = (
    "uid",
    "name",
    "description",
    "embedding_channel_id",
    "embedding_model_id",
    "embedding_dimensions",
    "collection_name",
    "id",
    "created_at",
    "updated_at",
)

NEW_COLUMN_NAMES: Final[tuple[str, ...]] = (
    "knowledge_base_type",
    "managed_profile_id",
    "active_embedding_channel_id",
    "active_embedding_model_id",
    "active_embedding_dimensions",
    "active_embedding_signature",
    "active_embedding_revision",
    "active_collection_name",
    "target_embedding_channel_id",
    "target_embedding_model_id",
    "target_embedding_dimensions",
    "target_embedding_signature",
    "target_embedding_revision",
    "target_collection_name",
    "migration_job_id",
    "migration_status",
    "migration_snapshot_boundary",
    "migration_cursor",
    "migration_total_count",
    "migration_success_count",
    "migration_failure_count",
    "migration_delta_high_watermark",
    "migration_delta_applied_watermark",
    "migration_error",
    "migration_started_at",
    "migration_finished_at",
    "old_collection_name",
    "old_collection_cleanup_status",
    "old_collection_cleanup_job_id",
    "old_collection_cleanup_error",
    "old_collection_cleanup_at",
    "index_revision",
    "index_status",
)

TARGET_COLUMN_NAMES: Final[tuple[str, ...]] = (
    *LEGACY_COLUMN_NAMES,
    *NEW_COLUMN_NAMES,
)

ACTIVE_LEGACY_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("active_embedding_channel_id", "embedding_channel_id"),
    ("active_embedding_model_id", "embedding_model_id"),
    ("active_embedding_dimensions", "embedding_dimensions"),
    ("active_collection_name", "collection_name"),
)

MYSQL_REQUIRED_DEFAULTS: Final[dict[str, str]] = {
    "knowledge_base_type": "'USER'",
    "active_embedding_revision": "0",
    "migration_total_count": "0",
    "migration_success_count": "0",
    "migration_failure_count": "0",
    "migration_delta_high_watermark": "0",
    "migration_delta_applied_watermark": "0",
    "old_collection_cleanup_status": "'NONE'",
    "index_revision": "0",
    "index_status": "'PENDING'",
}


def _enum_type(enum_class: type[StrEnum]) -> SAEnum:
    return SAEnum(
        enum_class,
        name=enum_class.__name__.lower(),
    )


def _foreign_key(column_name: str) -> ForeignKey:
    spec = FOREIGN_KEY_SPECS[column_name]
    return ForeignKey(
        spec["target"],
        name=spec["name"],
        ondelete=spec["ondelete"],
    )


TARGET_METADATA: Final[MetaData] = MetaData()

CHANNEL_TABLE: Final[Table] = Table(
    "channel",
    TARGET_METADATA,
    Column("id", Integer, primary_key=True),
)

PROFILE_TABLE: Final[Table] = Table(
    "profile",
    TARGET_METADATA,
    Column("id", Integer, primary_key=True),
    Column("uid", String(50), nullable=True),
    UniqueConstraint(
        *PROFILE_OWNER_KEY_COLUMNS,
        name=PROFILE_OWNER_UNIQUE_NAME,
    ),
)

TARGET_TABLE: Final[Table] = Table(
    "knowledge_base",
    TARGET_METADATA,
    Column("uid", String(50), nullable=False, index=True),
    Column("name", String(100), nullable=False, index=True),
    Column("description", String(500), nullable=True),
    Column(
        "embedding_channel_id",
        Integer,
        _foreign_key("embedding_channel_id"),
        nullable=False,
        index=True,
    ),
    Column("embedding_model_id", String(255), nullable=False),
    Column("embedding_dimensions", Integer, nullable=True),
    Column(
        "collection_name",
        String(100),
        nullable=False,
        index=True,
        unique=True,
    ),
    Column("id", Integer, primary_key=True, index=True),
    Column("created_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=True),
    Column(
        "knowledge_base_type",
        _enum_type(KnowledgeBaseType),
        nullable=False,
        default=KnowledgeBaseType.USER,
        index=True,
    ),
    Column(
        "managed_profile_id",
        Integer,
        nullable=True,
    ),
    Column(
        "active_embedding_channel_id",
        Integer,
        _foreign_key("active_embedding_channel_id"),
        nullable=True,
        index=True,
    ),
    Column(
        "active_embedding_model_id",
        String(255),
        nullable=True,
        index=True,
    ),
    Column("active_embedding_dimensions", Integer, nullable=True),
    Column(
        "active_embedding_signature",
        String(128),
        nullable=True,
        index=True,
    ),
    Column(
        "active_embedding_revision",
        Integer,
        nullable=False,
        default=0,
        index=True,
    ),
    Column(
        "active_collection_name",
        String(255),
        nullable=True,
        index=True,
        unique=True,
    ),
    Column(
        "target_embedding_channel_id",
        Integer,
        _foreign_key("target_embedding_channel_id"),
        nullable=True,
        index=True,
    ),
    Column(
        "target_embedding_model_id",
        String(255),
        nullable=True,
        index=True,
    ),
    Column("target_embedding_dimensions", Integer, nullable=True),
    Column(
        "target_embedding_signature",
        String(128),
        nullable=True,
        index=True,
    ),
    Column(
        "target_embedding_revision",
        Integer,
        nullable=True,
        index=True,
    ),
    Column(
        "target_collection_name",
        String(255),
        nullable=True,
        index=True,
        unique=True,
    ),
    Column(
        "migration_job_id",
        Integer,
        nullable=True,
        index=True,
    ),
    Column(
        "migration_status",
        _enum_type(KnowledgeBaseMigrationStatus),
        nullable=True,
        index=True,
    ),
    Column("migration_snapshot_boundary", Integer, nullable=True),
    Column("migration_cursor", Integer, nullable=True),
    Column("migration_total_count", Integer, nullable=False, default=0),
    Column("migration_success_count", Integer, nullable=False, default=0),
    Column("migration_failure_count", Integer, nullable=False, default=0),
    Column(
        "migration_delta_high_watermark",
        Integer,
        nullable=False,
        default=0,
    ),
    Column(
        "migration_delta_applied_watermark",
        Integer,
        nullable=False,
        default=0,
    ),
    Column("migration_error", Text, nullable=True),
    Column("migration_started_at", DateTime(timezone=True), nullable=True),
    Column("migration_finished_at", DateTime(timezone=True), nullable=True),
    Column(
        "old_collection_name",
        String(255),
        nullable=True,
        index=True,
        unique=True,
    ),
    Column(
        "old_collection_cleanup_status",
        _enum_type(KnowledgeBaseOldCollectionCleanupStatus),
        nullable=False,
        default=KnowledgeBaseOldCollectionCleanupStatus.NONE,
        index=True,
    ),
    Column(
        "old_collection_cleanup_job_id",
        Integer,
        nullable=True,
        index=True,
    ),
    Column("old_collection_cleanup_error", Text, nullable=True),
    Column(
        "old_collection_cleanup_at",
        DateTime(timezone=True),
        nullable=True,
    ),
    Column("index_revision", Integer, nullable=False, default=0, index=True),
    Column(
        "index_status",
        _enum_type(KnowledgeBaseIndexStatus),
        nullable=False,
        default=KnowledgeBaseIndexStatus.PENDING,
        index=True,
    ),
    UniqueConstraint(
        "managed_profile_id",
        name="uq_knowledge_base_managed_profile",
    ),
    ForeignKeyConstraint(
        MANAGED_PROFILE_FOREIGN_KEY_COLUMNS,
        tuple(f"profile.{column_name}" for column_name in PROFILE_OWNER_KEY_COLUMNS),
        name=MANAGED_PROFILE_FOREIGN_KEY_NAME,
        ondelete=MANAGED_PROFILE_FOREIGN_KEY_ONDELETE,
    ),
    CheckConstraint(
        OWNER_CHECK_SQL,
        name="ck_knowledge_base_type_profile_owner",
    ),
    UniqueConstraint(
        "id",
        "uid",
        name="uq_knowledge_base_id_uid",
    ),
)


def copy_target_table(metadata: MetaData, *, name: str) -> Table:
    for parent_table in (CHANNEL_TABLE, PROFILE_TABLE):
        if parent_table.name not in metadata.tables:
            parent_table.to_metadata(metadata)

    copied_table = TARGET_TABLE.to_metadata(metadata, name=name)
    for index in tuple(copied_table.indexes):
        copied_table.indexes.remove(index)
    return copied_table
