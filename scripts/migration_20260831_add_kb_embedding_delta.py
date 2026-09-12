from sqlalchemy import (
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

MIGRATION_ID = "20260831_add_kb_embedding_delta_v1"


metadata = MetaData()
knowledge_base = Table(
    "knowledge_base",
    metadata,
    Column("id", Integer),
    Column("uid", String(50)),
)

knowledge_base_migration_source_type = Enum(
    "USER_DOCUMENT",
    "MANAGED_KNOWLEDGE",
    name="knowledgebasemigrationsourcetype",
)
knowledge_base_migration_delta_action = Enum(
    "UPSERT",
    "DELETE",
    name="knowledgebasemigrationdeltaaction",
)
knowledge_base_migration_delta_status = Enum(
    "PENDING",
    "APPLIED",
    name="knowledgebasemigrationdeltastatus",
)

knowledge_base_embedding_delta = Table(
    "knowledge_base_embedding_delta",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(50), nullable=False),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("migration_job_id", Integer, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("source_type", knowledge_base_migration_source_type, nullable=False),
    Column("source_id", Integer, nullable=False),
    Column("source_version", Integer),
    Column("action", knowledge_base_migration_delta_action, nullable=False),
    Column("status", knowledge_base_migration_delta_status, nullable=False),
    Column("error", Text),
    Column("applied_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "migration_job_id",
        "sequence",
        name="uq_kb_embedding_delta_job_sequence",
    ),
    ForeignKeyConstraint(
        ["knowledge_base_id", "uid"],
        ["knowledge_base.id", "knowledge_base.uid"],
        name="fk_kb_embedding_delta_kb_owner",
        ondelete="CASCADE",
    ),
)
Index("ix_knowledge_base_embedding_delta_id", knowledge_base_embedding_delta.c.id)
Index("ix_knowledge_base_embedding_delta_uid", knowledge_base_embedding_delta.c.uid)
Index("ix_knowledge_base_embedding_delta_knowledge_base_id", knowledge_base_embedding_delta.c.knowledge_base_id)
Index("ix_knowledge_base_embedding_delta_migration_job_id", knowledge_base_embedding_delta.c.migration_job_id)
Index("ix_knowledge_base_embedding_delta_sequence", knowledge_base_embedding_delta.c.sequence)
Index("ix_knowledge_base_embedding_delta_source_type", knowledge_base_embedding_delta.c.source_type)
Index("ix_knowledge_base_embedding_delta_source_id", knowledge_base_embedding_delta.c.source_id)
Index("ix_knowledge_base_embedding_delta_source_version", knowledge_base_embedding_delta.c.source_version)
Index("ix_knowledge_base_embedding_delta_action", knowledge_base_embedding_delta.c.action)
Index("ix_knowledge_base_embedding_delta_status", knowledge_base_embedding_delta.c.status)
Index("ix_knowledge_base_embedding_delta_applied_at", knowledge_base_embedding_delta.c.applied_at)
Index("ix_knowledge_base_embedding_delta_created_at", knowledge_base_embedding_delta.c.created_at)
Index(
    "ix_kb_embedding_delta_uid_job_sequence",
    knowledge_base_embedding_delta.c.uid,
    knowledge_base_embedding_delta.c.migration_job_id,
    knowledge_base_embedding_delta.c.sequence,
)


def _ensure_knowledge_base_embedding_delta_table(connection) -> None:
    knowledge_base_embedding_delta.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_knowledge_base_embedding_delta_table)
