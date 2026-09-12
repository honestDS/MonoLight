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

MIGRATION_ID = "20260910_add_knowledge_organization_stage_v1"


metadata = MetaData()
knowledge_base = Table(
    "knowledge_base",
    metadata,
    Column("id", Integer),
    Column("uid", String(50)),
)

knowledge_organization_stage_status = Enum(
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "INVALIDATED",
    name="knowledgeorganizationstagestatus",
)
knowledge_organization_fragment_status = Enum(
    "COMPLETED",
    "INVALIDATED",
    name="knowledgeorganizationfragmentstatus",
)

knowledge_organization_snapshot = Table(
    "knowledge_organization_snapshot",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(50), nullable=False),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("snapshot_key", String(64), nullable=False),
    Column("boundary_revision_id", Integer, nullable=False),
    Column("active_embedding_revision", Integer, nullable=False),
    Column("index_revision", Integer, nullable=False),
    Column("item_count", Integer, nullable=False),
    Column("items", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "uid",
        "knowledge_base_id",
        "snapshot_key",
        name="uq_knowledge_organization_snapshot_identity",
    ),
    ForeignKeyConstraint(
        ["knowledge_base_id", "uid"],
        ["knowledge_base.id", "knowledge_base.uid"],
        name="fk_knowledge_organization_snapshot_kb_owner",
        ondelete="CASCADE",
    ),
)
Index("ix_knowledge_organization_snapshot_id", knowledge_organization_snapshot.c.id)
Index("ix_knowledge_organization_snapshot_uid", knowledge_organization_snapshot.c.uid)
Index("ix_knowledge_organization_snapshot_knowledge_base_id", knowledge_organization_snapshot.c.knowledge_base_id)
Index("ix_knowledge_organization_snapshot_snapshot_key", knowledge_organization_snapshot.c.snapshot_key)
Index("ix_knowledge_organization_snapshot_created_at", knowledge_organization_snapshot.c.created_at)
Index(
    "ix_knowledge_organization_snapshot_kb_created",
    knowledge_organization_snapshot.c.knowledge_base_id,
    knowledge_organization_snapshot.c.created_at,
)

knowledge_organization_stage = Table(
    "knowledge_organization_stage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(50), nullable=False),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("snapshot_id", Integer, nullable=False),
    Column("work_key", String(64), nullable=False),
    Column("snapshot_key", String(64), nullable=False),
    Column("stage_key", String(64), nullable=False),
    Column("stage_index", Integer, nullable=False),
    Column("lower_stage_key", String(64)),
    Column("model_key", String(64), nullable=False),
    Column("model_snapshot", JSON, nullable=False),
    Column("expected_fragment_count", Integer, nullable=False),
    Column("succeeded_fragment_count", Integer, nullable=False),
    Column("status", knowledge_organization_stage_status, nullable=False),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True)),
    UniqueConstraint(
        "work_key",
        "stage_key",
        name="uq_knowledge_organization_stage_work_stage",
    ),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["knowledge_organization_snapshot.id"],
        ondelete="CASCADE",
    ),
)
Index("ix_knowledge_organization_stage_id", knowledge_organization_stage.c.id)
Index("ix_knowledge_organization_stage_uid", knowledge_organization_stage.c.uid)
Index("ix_knowledge_organization_stage_knowledge_base_id", knowledge_organization_stage.c.knowledge_base_id)
Index("ix_knowledge_organization_stage_snapshot_id", knowledge_organization_stage.c.snapshot_id)
Index("ix_knowledge_organization_stage_work_key", knowledge_organization_stage.c.work_key)
Index("ix_knowledge_organization_stage_snapshot_key", knowledge_organization_stage.c.snapshot_key)
Index("ix_knowledge_organization_stage_stage_key", knowledge_organization_stage.c.stage_key)
Index("ix_knowledge_organization_stage_lower_stage_key", knowledge_organization_stage.c.lower_stage_key)
Index("ix_knowledge_organization_stage_model_key", knowledge_organization_stage.c.model_key)
Index("ix_knowledge_organization_stage_status", knowledge_organization_stage.c.status)
Index("ix_knowledge_organization_stage_created_at", knowledge_organization_stage.c.created_at)
Index("ix_knowledge_organization_stage_completed_at", knowledge_organization_stage.c.completed_at)
Index(
    "ix_knowledge_organization_stage_kb_status",
    knowledge_organization_stage.c.knowledge_base_id,
    knowledge_organization_stage.c.status,
    knowledge_organization_stage.c.created_at,
)

knowledge_organization_fragment = Table(
    "knowledge_organization_fragment",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("dedupe_key", String(64), nullable=False),
    Column("uid", String(50), nullable=False),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("snapshot_id", Integer, nullable=False),
    Column("stage_id", Integer, nullable=False),
    Column("work_key", String(64), nullable=False),
    Column("snapshot_key", String(64), nullable=False),
    Column("stage_key", String(64), nullable=False),
    Column("model_key", String(64), nullable=False),
    Column("fragment_index", Integer, nullable=False),
    Column("candidate_scope", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("status", knowledge_organization_fragment_status, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "work_key",
        "stage_key",
        "fragment_index",
        name="uq_knowledge_organization_fragment_work_stage_index",
    ),
    UniqueConstraint(
        "dedupe_key",
        name="uq_knowledge_organization_fragment_dedupe",
    ),
    ForeignKeyConstraint(
        ["stage_id"],
        ["knowledge_organization_stage.id"],
        ondelete="CASCADE",
    ),
)
Index("ix_knowledge_organization_fragment_id", knowledge_organization_fragment.c.id)
Index("ix_knowledge_organization_fragment_dedupe_key", knowledge_organization_fragment.c.dedupe_key)
Index("ix_knowledge_organization_fragment_uid", knowledge_organization_fragment.c.uid)
Index("ix_knowledge_organization_fragment_knowledge_base_id", knowledge_organization_fragment.c.knowledge_base_id)
Index("ix_knowledge_organization_fragment_snapshot_id", knowledge_organization_fragment.c.snapshot_id)
Index("ix_knowledge_organization_fragment_stage_id", knowledge_organization_fragment.c.stage_id)
Index("ix_knowledge_organization_fragment_work_key", knowledge_organization_fragment.c.work_key)
Index("ix_knowledge_organization_fragment_snapshot_key", knowledge_organization_fragment.c.snapshot_key)
Index("ix_knowledge_organization_fragment_stage_key", knowledge_organization_fragment.c.stage_key)
Index("ix_knowledge_organization_fragment_model_key", knowledge_organization_fragment.c.model_key)
Index("ix_knowledge_organization_fragment_status", knowledge_organization_fragment.c.status)
Index("ix_knowledge_organization_fragment_created_at", knowledge_organization_fragment.c.created_at)
Index(
    "ix_knowledge_organization_fragment_stage_index",
    knowledge_organization_fragment.c.stage_id,
    knowledge_organization_fragment.c.fragment_index,
)


def _ensure_knowledge_organization_tables(connection) -> None:
    knowledge_organization_snapshot.create(bind=connection, checkfirst=True)
    knowledge_organization_stage.create(bind=connection, checkfirst=True)
    knowledge_organization_fragment.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_knowledge_organization_tables)
