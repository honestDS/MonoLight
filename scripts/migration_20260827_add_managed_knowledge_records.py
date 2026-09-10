from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260827_add_managed_knowledge_records_v1"


metadata = MetaData()
knowledge_base = Table(
    "knowledge_base",
    metadata,
    Column("id", Integer),
    Column("uid", String(50)),
)

managed_knowledge_source_type = Enum(
    "LLM_TOOL",
    "USER_API",
    "AUTO_ORGANIZE",
    "SYSTEM",
    name="managedknowledgesourcetype",
)
managed_knowledge_actor_type = Enum(
    "LLM",
    "USER",
    "SYSTEM",
    name="managedknowledgeactortype",
)
managed_knowledge_revision_operation = Enum(
    "CREATE",
    "UPDATE",
    "DELETE",
    name="managedknowledgerevisionoperation",
)

managed_knowledge_item = Table(
    "managed_knowledge_item",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("uid", String(50), nullable=False),
    Column("knowledge_key", String(255), nullable=False),
    Column("content", Text, nullable=False),
    Column("content_token_count", Integer, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("source_type", managed_knowledge_source_type, nullable=False),
    Column("source_reference", JSON),
    Column("source_job_id", Integer),
    Column("created_by", managed_knowledge_actor_type, nullable=False),
    Column("last_modified_by", managed_knowledge_actor_type, nullable=False),
    Column("llm_maintainable", Boolean, nullable=False),
    Column("indexed_version", Integer, nullable=False),
    Column("vector_item_ids", JSON, nullable=False),
    Column("is_recallable", Boolean, nullable=False),
    Column("pending_job_id", Integer),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
    Column("last_recalled_at", DateTime(timezone=True)),
    UniqueConstraint("knowledge_base_id", "knowledge_key", name="uq_managed_knowledge_item_kb_key"),
    UniqueConstraint("knowledge_base_id", "content_hash", name="uq_managed_knowledge_item_kb_content_hash"),
    ForeignKeyConstraint(
        ["knowledge_base_id", "uid"],
        ["knowledge_base.id", "knowledge_base.uid"],
        name="fk_managed_knowledge_item_kb_owner",
        ondelete="CASCADE",
    ),
    sqlite_autoincrement=True,
)
Index("ix_managed_knowledge_item_id", managed_knowledge_item.c.id)
Index("ix_managed_knowledge_item_knowledge_base_id", managed_knowledge_item.c.knowledge_base_id)
Index("ix_managed_knowledge_item_uid", managed_knowledge_item.c.uid)
Index("ix_managed_knowledge_item_knowledge_key", managed_knowledge_item.c.knowledge_key)
Index("ix_managed_knowledge_item_content_hash", managed_knowledge_item.c.content_hash)
Index("ix_managed_knowledge_item_version", managed_knowledge_item.c.version)
Index("ix_managed_knowledge_item_source_type", managed_knowledge_item.c.source_type)
Index("ix_managed_knowledge_item_source_job_id", managed_knowledge_item.c.source_job_id)
Index("ix_managed_knowledge_item_created_by", managed_knowledge_item.c.created_by)
Index("ix_managed_knowledge_item_last_modified_by", managed_knowledge_item.c.last_modified_by)
Index("ix_managed_knowledge_item_llm_maintainable", managed_knowledge_item.c.llm_maintainable)
Index("ix_managed_knowledge_item_indexed_version", managed_knowledge_item.c.indexed_version)
Index("ix_managed_knowledge_item_is_recallable", managed_knowledge_item.c.is_recallable)
Index("ix_managed_knowledge_item_pending_job_id", managed_knowledge_item.c.pending_job_id)
Index("ix_managed_knowledge_item_created_at", managed_knowledge_item.c.created_at)
Index("ix_managed_knowledge_item_updated_at", managed_knowledge_item.c.updated_at)
Index("ix_managed_knowledge_item_deleted_at", managed_knowledge_item.c.deleted_at)
Index("ix_managed_knowledge_item_last_recalled_at", managed_knowledge_item.c.last_recalled_at)
Index(
    "ix_managed_knowledge_item_kb_recallable",
    managed_knowledge_item.c.knowledge_base_id,
    managed_knowledge_item.c.is_recallable,
    managed_knowledge_item.c.deleted_at,
)
Index(
    "ix_managed_knowledge_item_kb_updated",
    managed_knowledge_item.c.knowledge_base_id,
    managed_knowledge_item.c.updated_at,
    managed_knowledge_item.c.id,
)

managed_knowledge_revision = Table(
    "managed_knowledge_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("uid", String(50), nullable=False),
    Column("knowledge_id", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("operation", managed_knowledge_revision_operation, nullable=False),
    Column("before_snapshot", JSON),
    Column("after_snapshot", JSON, nullable=False),
    Column("source_type", managed_knowledge_source_type, nullable=False),
    Column("source_reference", JSON),
    Column("source_job_id", Integer),
    Column("modified_by", managed_knowledge_actor_type, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "knowledge_base_id",
        "knowledge_id",
        "version",
        name="uq_managed_knowledge_revision_kb_knowledge_version",
    ),
    ForeignKeyConstraint(
        ["knowledge_base_id", "uid"],
        ["knowledge_base.id", "knowledge_base.uid"],
        name="fk_managed_knowledge_revision_kb_owner",
        ondelete="CASCADE",
    ),
)
Index("ix_managed_knowledge_revision_id", managed_knowledge_revision.c.id)
Index("ix_managed_knowledge_revision_knowledge_base_id", managed_knowledge_revision.c.knowledge_base_id)
Index("ix_managed_knowledge_revision_uid", managed_knowledge_revision.c.uid)
Index("ix_managed_knowledge_revision_knowledge_id", managed_knowledge_revision.c.knowledge_id)
Index("ix_managed_knowledge_revision_version", managed_knowledge_revision.c.version)
Index("ix_managed_knowledge_revision_operation", managed_knowledge_revision.c.operation)
Index("ix_managed_knowledge_revision_source_type", managed_knowledge_revision.c.source_type)
Index("ix_managed_knowledge_revision_source_job_id", managed_knowledge_revision.c.source_job_id)
Index("ix_managed_knowledge_revision_modified_by", managed_knowledge_revision.c.modified_by)
Index("ix_managed_knowledge_revision_created_at", managed_knowledge_revision.c.created_at)
Index(
    "ix_managed_knowledge_revision_history",
    managed_knowledge_revision.c.knowledge_base_id,
    managed_knowledge_revision.c.knowledge_id,
    managed_knowledge_revision.c.version,
)


def _ensure_managed_knowledge_tables(connection) -> None:
    managed_knowledge_item.create(bind=connection, checkfirst=True)
    managed_knowledge_revision.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_managed_knowledge_tables)
