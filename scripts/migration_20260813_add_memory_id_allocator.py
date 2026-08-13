from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateIndex

MIGRATION_ID = "20260813_add_memory_id_allocator"

_MEMORY_ID_SOURCES = (
    ("long_term_memory_record", "id"),
    ("long_term_memory_revision", "memory_id"),
    ("long_term_memory_mutation_job", "memory_id"),
    ("long_term_memory_embedding_delta", "memory_id"),
)


def _sequence_updater(dialect_name: str):
    return {
        "sqlite": _raise_sqlite_sequence,
        "mysql": _raise_mysql_sequence,
        "postgresql": _raise_postgresql_sequence,
    }.get(dialect_name)


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _sqlite_record_has_autoincrement(connection: Connection) -> bool:
    row = connection.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'long_term_memory_record'")).first()
    return row is not None and isinstance(row[0], str) and "AUTOINCREMENT" in row[0].upper()


def _rebuild_sqlite_record(connection: Connection) -> None:
    old_metadata = MetaData()
    old_table = Table("long_term_memory_record", old_metadata, autoload_with=connection)
    temporary_name = "long_term_memory_record__memory_id_allocator_new"
    new_metadata = MetaData()
    new_table = old_table.to_metadata(new_metadata, name=temporary_name)
    new_table.dialect_options["sqlite"]["autoincrement"] = True
    indexes = tuple(str(CreateIndex(index).compile(dialect=connection.dialect)) for index in old_table.indexes if index.name)
    for index in tuple(new_table.indexes):
        new_table.indexes.remove(index)

    new_metadata.create_all(connection, tables=[new_table], checkfirst=False)
    columns = tuple(column.name for column in old_table.columns)
    column_sql = ", ".join(_quote(connection, column) for column in columns)
    connection.execute(text(f"INSERT INTO {_quote(connection, temporary_name)} ({column_sql}) SELECT {column_sql} FROM {_quote(connection, 'long_term_memory_record')}"))
    connection.execute(text(f"DROP TABLE {_quote(connection, 'long_term_memory_record')}"))
    connection.execute(text(f"ALTER TABLE {_quote(connection, temporary_name)} RENAME TO {_quote(connection, 'long_term_memory_record')}"))
    for index_sql in indexes:
        connection.execute(text(index_sql))


def _ensure_sqlite_record_autoincrement(connection: Connection) -> None:
    inspector = inspect(connection)
    if "long_term_memory_record" not in inspector.get_table_names():
        return
    if not _sqlite_record_has_autoincrement(connection):
        _rebuild_sqlite_record(connection)


def _global_next_id(connection: Connection) -> int:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    maximum = 0
    for table_name, column_name in _MEMORY_ID_SOURCES:
        if table_name not in table_names or column_name not in {column["name"] for column in inspector.get_columns(table_name)}:
            continue
        value = connection.execute(text(f"SELECT MAX({_quote(connection, column_name)}) FROM {_quote(connection, table_name)}")).scalar_one()
        if value is not None:
            maximum = max(maximum, int(value))
    return maximum + 1


def _raise_sqlite_sequence(connection: Connection, next_id: int) -> None:
    sequence_value = next_id - 1
    existing = connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name = 'long_term_memory_record'")).scalar_one_or_none()
    if existing is None:
        connection.execute(
            text("INSERT INTO sqlite_sequence(name, seq) VALUES ('long_term_memory_record', :seq)"),
            {"seq": sequence_value},
        )
    elif int(existing) < sequence_value:
        connection.execute(
            text("UPDATE sqlite_sequence SET seq = :seq WHERE name = 'long_term_memory_record'"),
            {"seq": sequence_value},
        )


def _raise_mysql_sequence(connection: Connection, next_id: int) -> None:
    connection.execute(text(f"ALTER TABLE {_quote(connection, 'long_term_memory_record')} AUTO_INCREMENT = {int(next_id)}"))


def _raise_postgresql_sequence(connection: Connection, next_id: int) -> None:
    sequence_name = connection.execute(text("SELECT pg_get_serial_sequence('long_term_memory_record', 'id')")).scalar_one_or_none()
    if not sequence_name:
        return
    if next_id == 1:
        connection.execute(
            text("SELECT setval(CAST(:sequence_name AS regclass), :next_id, false)"),
            {"sequence_name": sequence_name, "next_id": next_id},
        )
    else:
        connection.execute(
            text("SELECT setval(CAST(:sequence_name AS regclass), :last_id, true)"),
            {"sequence_name": sequence_name, "last_id": next_id - 1},
        )


def _migrate_sync(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        _ensure_sqlite_record_autoincrement(connection)
    next_id = _global_next_id(connection)
    inspector = inspect(connection)
    if "long_term_memory_record" not in inspector.get_table_names():
        return
    sequence_updater = _sequence_updater(connection.dialect.name)
    if sequence_updater is not None:
        sequence_updater(connection, next_id)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_migrate_sync)
