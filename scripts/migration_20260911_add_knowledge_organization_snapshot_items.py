from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKeyConstraint, Index, Integer, MetaData, String, Table, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260911_add_knowledge_organization_snapshot_items_v1"


metadata = MetaData()
knowledge_organization_snapshot = Table(
    "knowledge_organization_snapshot",
    metadata,
    Column("id", Integer, primary_key=True),
)

knowledge_organization_snapshot_item = Table(
    "knowledge_organization_snapshot_item",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("snapshot_id", Integer, nullable=False),
    Column("uid", String(50), nullable=False),
    Column("knowledge_base_id", Integer, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("knowledge_id", Integer, nullable=False),
    Column("expected_version", Integer, nullable=False),
    Column("knowledge_key", String(255), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("content_token_count", Integer, nullable=False),
    Column("revision_id", Integer, nullable=False),
    Column("source_type", String(30), nullable=False),
    Column("source_reference", JSON),
    Column("llm_maintainable", Boolean, nullable=False),
    Column("indexed_version", Integer, nullable=False),
    Column("vector_item_ids", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("snapshot_id", "sequence", name="uq_knowledge_organization_snapshot_item_sequence"),
    UniqueConstraint("snapshot_id", "knowledge_id", name="uq_knowledge_organization_snapshot_item_knowledge"),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["knowledge_organization_snapshot.id"],
        name="fk_knowledge_organization_snapshot_item_snapshot",
        ondelete="CASCADE",
    ),
)
Index("ix_knowledge_organization_snapshot_item_id", knowledge_organization_snapshot_item.c.id)
Index("ix_knowledge_organization_snapshot_item_snapshot_id", knowledge_organization_snapshot_item.c.snapshot_id)
Index("ix_knowledge_organization_snapshot_item_uid", knowledge_organization_snapshot_item.c.uid)
Index("ix_knowledge_organization_snapshot_item_knowledge_base_id", knowledge_organization_snapshot_item.c.knowledge_base_id)
Index("ix_knowledge_organization_snapshot_item_knowledge_id", knowledge_organization_snapshot_item.c.knowledge_id)
Index("ix_knowledge_organization_snapshot_item_revision_id", knowledge_organization_snapshot_item.c.revision_id)
Index("ix_knowledge_organization_snapshot_item_created_at", knowledge_organization_snapshot_item.c.created_at)
Index("ix_knowledge_organization_snapshot_item_snapshot_sequence", knowledge_organization_snapshot_item.c.snapshot_id, knowledge_organization_snapshot_item.c.sequence)


def _ensure_snapshot_item_table(connection) -> None:
    knowledge_organization_snapshot_item.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_snapshot_item_table)
    if connection.dialect.name == "mysql":
        await session.execute(text("ALTER TABLE managed_knowledge_item MODIFY COLUMN content LONGTEXT NOT NULL"))
