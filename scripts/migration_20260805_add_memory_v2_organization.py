from typing import Any

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, MetaData, String, Table, Text, false, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateColumn, CreateIndex

from app.core.constants import (
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_POLICY_VERSION,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.utils.tokenizer import estimate_tokens
from app.models.memory import LongTermMemoryCapacityStatus

MIGRATION_ID = "20260805_add_memory_v2_organization_v1"

_STORE_TABLE = "long_term_memory_store"
_RECORD_TABLE = "long_term_memory_record"
_REVISION_TABLE = "long_term_memory_revision"

_MEMORY_V2_INDEX_DEFINITIONS = (
    (
        _STORE_TABLE,
        "ix_long_term_memory_store_organization_channel_id",
        ("organization_channel_id",),
    ),
    (
        _STORE_TABLE,
        "ix_long_term_memory_store_organization_model_id",
        ("organization_model_id",),
    ),
    (
        _STORE_TABLE,
        "ix_long_term_memory_store_organization_last_job_id",
        ("organization_last_job_id",),
    ),
    (
        _STORE_TABLE,
        "ix_long_term_memory_store_capacity_status",
        ("capacity_status",),
    ),
    (
        _RECORD_TABLE,
        "ix_ltm_record_eviction_candidate",
        ("uid", "is_active", "pinned", "last_recalled_at", "updated_at", "id"),
    ),
)


def _memory_v2_column_definitions() -> tuple[tuple[str, tuple[Column, ...]], ...]:
    return (
        (
            _STORE_TABLE,
            (
                Column(
                    "organize_trigger_records",
                    Integer,
                    nullable=False,
                    server_default=text(str(MEMORY_ORGANIZE_TRIGGER_RECORDS)),
                ),
                Column("auto_organize_enabled", Boolean, nullable=False, server_default=false()),
                Column("organization_channel_id", Integer),
                Column("organization_model_id", String(255)),
                Column(
                    "organization_policy_version",
                    Integer,
                    nullable=False,
                    server_default=text(str(MEMORY_ORGANIZE_POLICY_VERSION)),
                ),
                Column("organization_last_job_id", Integer),
                Column("organization_last_run_at", DateTime(timezone=True)),
                Column("organization_error", Text),
                Column(
                    "capacity_status",
                    String(20),
                    nullable=False,
                    server_default=text(f"'{LongTermMemoryCapacityStatus.NORMAL.name}'"),
                ),
            ),
        ),
        (
            _RECORD_TABLE,
            (
                Column("content_token_count", Integer, nullable=False, server_default=text("0")),
                Column("pinned", Boolean, nullable=False, server_default=false()),
                Column("last_recalled_at", DateTime(timezone=True)),
            ),
        ),
        (
            _REVISION_TABLE,
            (Column("content_token_count", Integer, nullable=False, server_default=text("0")),),
        ),
    )


def _compile_add_column_ddl(table_name: str, column: Column, dialect: Any) -> str:
    preparer = dialect.identifier_preparer
    column_definition = str(CreateColumn(column).compile(dialect=dialect))
    return f"ALTER TABLE {preparer.quote(table_name)} ADD COLUMN {column_definition}"


def _compile_index_ddl(
    table_name: str,
    index_name: str,
    column_names: tuple[str, ...],
    dialect: Any,
) -> str:
    metadata = MetaData()
    table = Table(table_name, metadata, *(Column(column_name, Integer) for column_name in column_names))
    index = Index(index_name, *(table.c[column_name] for column_name in column_names))
    return str(CreateIndex(index).compile(dialect=dialect))


def _ensure_schema(connection: Any) -> dict[str, Table]:
    existing_tables = set(inspect(connection).get_table_names())

    for table_name, columns in _memory_v2_column_definitions():
        if table_name not in existing_tables:
            continue
        table = Table(table_name, MetaData(), autoload_with=connection)
        for column in columns:
            if column.name in table.c:
                continue
            connection.execute(text(_compile_add_column_ddl(table_name, column, connection.dialect)))

    index_metadata = MetaData()
    for table_name, index_name, column_names in _MEMORY_V2_INDEX_DEFINITIONS:
        if table_name not in existing_tables:
            continue
        table = Table(table_name, index_metadata, autoload_with=connection)
        if index_name in {index.get("name") for index in inspect(connection).get_indexes(table_name)}:
            continue
        if not all(column_name in table.c for column_name in column_names):
            continue
        index = Index(index_name, *(table.c[column_name] for column_name in column_names))
        connection.execute(CreateIndex(index))

    data_metadata = MetaData()
    return {table_name: Table(table_name, data_metadata, autoload_with=connection) for table_name in (_STORE_TABLE, _RECORD_TABLE, _REVISION_TABLE) if table_name in existing_tables}


async def _backfill_record_tokens(session: AsyncSession, tables: dict[str, Table]) -> None:
    record_table = tables.get(_RECORD_TABLE)
    if record_table is None:
        return

    result = await session.execute(
        select(record_table.c.id, record_table.c.content).where(
            record_table.c.is_active.is_(True),
            record_table.c.deleted_at.is_(None),
        )
    )
    for row in result.mappings().all():
        await session.execute(update(record_table).where(record_table.c.id == row["id"]).values(content_token_count=estimate_tokens(row["content"] or "")))


async def _backfill_revision_tokens(session: AsyncSession, tables: dict[str, Table]) -> None:
    revision_table = tables.get(_REVISION_TABLE)
    if revision_table is None:
        return

    result = await session.execute(select(revision_table.c.id, revision_table.c.content))
    for row in result.mappings().all():
        await session.execute(update(revision_table).where(revision_table.c.id == row["id"]).values(content_token_count=estimate_tokens(row["content"] or "")))


async def _normalize_store_capacity(session: AsyncSession, tables: dict[str, Table]) -> None:
    store_table = tables.get(_STORE_TABLE)
    if store_table is None:
        return

    await session.execute(
        update(store_table).values(
            max_active_records=MEMORY_MAX_ACTIVE_RECORDS,
            organize_trigger_records=MEMORY_ORGANIZE_TRIGGER_RECORDS,
            capacity_status=LongTermMemoryCapacityStatus.NORMAL.name,
        )
    )

    record_table = tables.get(_RECORD_TABLE)
    if record_table is None:
        return

    result = await session.execute(
        select(record_table.c.uid, record_table.c.content_token_count).where(
            record_table.c.is_active.is_(True),
            record_table.c.deleted_at.is_(None),
        )
    )
    active_counts: dict[str, int] = {}
    over_token_uids: set[str] = set()
    for row in result.mappings().all():
        uid = row["uid"]
        active_counts[uid] = active_counts.get(uid, 0) + 1
        if (row["content_token_count"] or 0) > MEMORY_CONTENT_MAX_TOKENS:
            over_token_uids.add(uid)

    over_limit_uids = {uid for uid, active_count in active_counts.items() if active_count > MEMORY_MAX_ACTIVE_RECORDS or uid in over_token_uids}
    if over_limit_uids:
        await session.execute(update(store_table).where(store_table.c.uid.in_(over_limit_uids)).values(capacity_status=LongTermMemoryCapacityStatus.OVER_LIMIT.name))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    tables = await connection.run_sync(_ensure_schema)
    await _backfill_record_tokens(session, tables)
    await _backfill_revision_tokens(session, tables)
    await _normalize_store_capacity(session, tables)
