from sqlalchemy import (
    JSON,
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

MIGRATION_ID = "20260828_add_knowledge_job_v1"


metadata = MetaData()
knowledge_base = Table(
    "knowledge_base",
    metadata,
    Column("id", Integer),
    Column("uid", String(50)),
)

knowledge_job_operation = Enum(
    "MANAGED_CREATE",
    "MANAGED_UPDATE",
    "MANAGED_DELETE_CLEANUP",
    "MANAGED_VECTOR_CLEANUP",
    "USER_DOCUMENT_INDEX",
    "USER_DOCUMENT_DELETE_CLEANUP",
    "EMBEDDING_MIGRATION",
    "REINDEX",
    "OLD_COLLECTION_CLEANUP",
    "AUTO_ORGANIZE",
    "MANUAL_ORGANIZE",
    "ORGANIZE_MUTATION",
    name="knowledgejoboperation",
)
knowledge_job_status = Enum(
    "PENDING",
    "RUNNING",
    "RETRY",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    name="knowledgejobstatus",
)

knowledge_job = Table(
    "knowledge_job",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(50), nullable=False),
    Column("parent_job_id", Integer),
    Column("operation", knowledge_job_operation, nullable=False),
    Column("dedupe_key", String(255), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("active_change_key", String(255)),
    Column("status", knowledge_job_status, nullable=False),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("knowledge_id", Integer),
    Column("expected_version", Integer),
    Column("payload", JSON, nullable=False),
    Column("result", JSON),
    Column("error", Text),
    Column("source_session_id", String(100)),
    Column("source_profile_id", Integer),
    Column("source_message_id", Integer),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("locked_by", String(100)),
    Column("lock_until", DateTime(timezone=True)),
    Column("cancel_requested_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("uid", "dedupe_key", name="uq_knowledge_job_uid_dedupe"),
    UniqueConstraint("uid", "active_change_key", name="uq_knowledge_job_uid_active_change"),
    ForeignKeyConstraint(
        ["knowledge_base_id", "uid"],
        ["knowledge_base.id", "knowledge_base.uid"],
        name="fk_knowledge_job_kb_owner",
        ondelete="CASCADE",
    ),
)
Index("ix_knowledge_job_id", knowledge_job.c.id)
Index("ix_knowledge_job_uid", knowledge_job.c.uid)
Index("ix_knowledge_job_parent_job_id", knowledge_job.c.parent_job_id)
Index("ix_knowledge_job_operation", knowledge_job.c.operation)
Index("ix_knowledge_job_dedupe_key", knowledge_job.c.dedupe_key)
Index("ix_knowledge_job_request_hash", knowledge_job.c.request_hash)
Index("ix_knowledge_job_active_change_key", knowledge_job.c.active_change_key)
Index("ix_knowledge_job_status", knowledge_job.c.status)
Index("ix_knowledge_job_knowledge_base_id", knowledge_job.c.knowledge_base_id)
Index("ix_knowledge_job_knowledge_id", knowledge_job.c.knowledge_id)
Index("ix_knowledge_job_source_session_id", knowledge_job.c.source_session_id)
Index("ix_knowledge_job_source_profile_id", knowledge_job.c.source_profile_id)
Index("ix_knowledge_job_source_message_id", knowledge_job.c.source_message_id)
Index("ix_knowledge_job_available_at", knowledge_job.c.available_at)
Index("ix_knowledge_job_attempt_count", knowledge_job.c.attempt_count)
Index("ix_knowledge_job_locked_by", knowledge_job.c.locked_by)
Index("ix_knowledge_job_lock_until", knowledge_job.c.lock_until)
Index("ix_knowledge_job_cancel_requested_at", knowledge_job.c.cancel_requested_at)
Index("ix_knowledge_job_created_at", knowledge_job.c.created_at)
Index("ix_knowledge_job_updated_at", knowledge_job.c.updated_at)
Index("ix_knowledge_job_started_at", knowledge_job.c.started_at)
Index("ix_knowledge_job_finished_at", knowledge_job.c.finished_at)
Index(
    "ix_knowledge_job_uid_status_available",
    knowledge_job.c.uid,
    knowledge_job.c.status,
    knowledge_job.c.available_at,
)


def _ensure_knowledge_job_table(connection) -> None:
    knowledge_job.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_knowledge_job_table)
