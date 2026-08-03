from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260803_add_memory_embedding_selection_token"


metadata = MetaData()


long_term_memory_embedding_selection_token = Table(
    "long_term_memory_embedding_selection_token",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("profile_id", Integer, nullable=False),
    Column("token_digest", String(64), nullable=False),
    Column("profile_config_digest", String(64), nullable=False),
    Column("active_embedding_revision", Integer, nullable=False, server_default=text("0")),
    Column("target_embedding_channel_id", Integer, nullable=False),
    Column("target_embedding_model_id", String(255), nullable=False),
    Column("target_embedding_dimensions", Integer, nullable=False),
    Column("target_embedding_signature", String(128), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("token_digest", name="uq_ltm_embedding_selection_token_digest"),
)
Index("ix_ltm_embedding_selection_token_id", long_term_memory_embedding_selection_token.c.id)
Index("ix_ltm_embedding_selection_token_uid", long_term_memory_embedding_selection_token.c.uid)
Index("ix_ltm_embedding_selection_token_profile_id", long_term_memory_embedding_selection_token.c.profile_id)
Index(
    "ix_ltm_embedding_selection_token_uid_profile",
    long_term_memory_embedding_selection_token.c.uid,
    long_term_memory_embedding_selection_token.c.profile_id,
)
Index("ix_ltm_embedding_selection_token_expires_at", long_term_memory_embedding_selection_token.c.expires_at)
Index("ix_ltm_embedding_selection_token_consumed_at", long_term_memory_embedding_selection_token.c.consumed_at)
Index("ix_ltm_embedding_selection_token_created_at", long_term_memory_embedding_selection_token.c.created_at)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: metadata.create_all(sync_connection, checkfirst=True))
