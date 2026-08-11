from typing import Any

from sqlalchemy import Column, Index, Integer, MetaData, Table, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateColumn, CreateIndex

MIGRATION_ID = "20260811_add_memory_job_parent_v1"

_JOB_TABLE = "long_term_memory_mutation_job"
_PARENT_COLUMN = "parent_job_id"
_PARENT_INDEX = "ix_long_term_memory_mutation_job_parent_job_id"


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


def _ensure_schema(connection: Any) -> None:
    existing_tables = set(inspect(connection).get_table_names())
    if _JOB_TABLE not in existing_tables:
        return

    table = Table(_JOB_TABLE, MetaData(), autoload_with=connection)
    if _PARENT_COLUMN not in table.c:
        column = Column(_PARENT_COLUMN, Integer, nullable=True)
        connection.execute(text(_compile_add_column_ddl(_JOB_TABLE, column, connection.dialect)))

    index_names = {index.get("name") for index in inspect(connection).get_indexes(_JOB_TABLE)}
    if _PARENT_INDEX in index_names:
        return

    table = Table(_JOB_TABLE, MetaData(), autoload_with=connection)
    if _PARENT_COLUMN not in table.c:
        return
    index = Index(_PARENT_INDEX, table.c[_PARENT_COLUMN])
    connection.execute(CreateIndex(index))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_schema)
