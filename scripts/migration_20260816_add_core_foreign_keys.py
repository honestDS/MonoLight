from collections.abc import Mapping
from typing import Any, NoReturn

from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import AddConstraint, CreateIndex, ForeignKeyConstraint, UniqueConstraint
from sqlmodel import SQLModel

import app.models  # noqa: F401
from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED, ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID
from app.core.i18n import t
from app.core.terminal.schemas import TerminalSessionStatus
from app.models.scheduled_task import ScheduledTaskStatus

MIGRATION_ID = "20260816_add_core_foreign_keys_v1"

_TARGET_TABLE_ORDER = (
    "profile",
    "chat_session",
    "knowledge_base",
    "audit_record",
    "audit_tool_detail",
    "session_reply_work_item",
    "terminal_session",
    "message",
    "session_event",
    "message_platform_outbox",
    "session_reply_sequence",
    "scheduled_task",
    "message_platform",
    "knowledge_base_profile_binding",
    "knowledge_base_document",
    "audit_confirmation_claim",
    "audit_execution_record",
    "context_summary_stage",
    "context_summary_fragment",
    "session_reply_stream_event",
    "terminal_control_command",
    "long_term_memory_store",
    "long_term_memory_embedding_selection_token",
)

_NEW_UNIQUE_CONSTRAINT_NAMES = frozenset(
    {
        "uq_chat_session_session_uid",
        "uq_audit_record_id_uid_session",
        "uq_audit_tool_detail_id_record",
        "uq_session_reply_work_item_id_session_uid",
        "uq_knowledge_base_profile_binding_pair",
    }
)

_TERMINAL_ORPHAN_FINAL_STATUSES = tuple(
    status.name
    for status in (
        TerminalSessionStatus.EXITED,
        TerminalSessionStatus.FAILED,
        TerminalSessionStatus.LOST,
    )
)


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _qualified(connection: Connection, alias: str, column: str) -> str:
    return f"{_quote(connection, alias)}.{_quote(connection, column)}"


def _raise_inconsistent(relation: str, count: int) -> NoReturn:
    raise RuntimeError(t(ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID, relation=relation, count=count))


def _table_exists(connection: Connection, table_name: str) -> bool:
    return table_name in set(inspect(connection).get_table_names())


def _table_has_columns(connection: Connection, table_name: str, columns: tuple[str, ...]) -> bool:
    if not _table_exists(connection, table_name):
        return False
    actual_columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
    return set(columns).issubset(actual_columns)


def _count(connection: Connection, statement: str, parameters: Mapping[str, Any] | None = None) -> int:
    value = connection.execute(text(statement), parameters or {}).scalar_one()
    return int(value or 0)


def _delete(connection: Connection, table_name: str, condition: str) -> None:
    connection.execute(text(f"DELETE FROM {_quote(connection, table_name)} WHERE {condition}"))


def _repair_chat_session_relation(connection: Connection, table_name: str, has_uid: bool) -> None:
    columns = ("session_id", "uid") if has_uid else ("session_id",)
    if not _table_has_columns(connection, table_name, columns) or not _table_has_columns(connection, "chat_session", ("session_id", "uid")):
        return

    child_alias = "child"
    parent_alias = "parent"
    if has_uid:
        mismatch = _count(
            connection,
            f"""
            SELECT COUNT(*)
            FROM {_quote(connection, table_name)} AS {_quote(connection, child_alias)}
            JOIN {_quote(connection, "chat_session")} AS {_quote(connection, parent_alias)}
              ON {_qualified(connection, parent_alias, "session_id")} = {_qualified(connection, child_alias, "session_id")}
            WHERE {_qualified(connection, child_alias, "uid")} <> {_qualified(connection, parent_alias, "uid")}
               OR {_qualified(connection, child_alias, "uid")} IS NULL
               OR {_qualified(connection, parent_alias, "uid")} IS NULL
            """,
        )
        if mismatch:
            _raise_inconsistent(f"{table_name}.session_owner", mismatch)

    parent_condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'chat_session')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'session_id')} = {_qualified(connection, table_name, 'session_id')})"
    _delete(connection, table_name, parent_condition)


def _repair_terminal_session(connection: Connection) -> None:
    if not _table_has_columns(connection, "terminal_session", ("session_id", "uid", "status")) or not _table_has_columns(
        connection,
        "chat_session",
        ("session_id", "uid"),
    ):
        return

    child_alias = "child"
    parent_alias = "parent"
    placeholders = ", ".join(f":terminal_status_{index}" for index in range(len(_TERMINAL_ORPHAN_FINAL_STATUSES)))
    orphan_condition = (
        f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'chat_session')} AS {_quote(connection, parent_alias)} "
        f"WHERE {_qualified(connection, parent_alias, 'session_id')} = {_qualified(connection, child_alias, 'session_id')} "
        f"AND {_qualified(connection, parent_alias, 'uid')} = {_qualified(connection, child_alias, 'uid')})"
    )
    invalid_status = f"({_qualified(connection, child_alias, 'status')} IS NULL OR {_qualified(connection, child_alias, 'status')} NOT IN ({placeholders}))"
    invalid_count = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {_quote(connection, "terminal_session")} AS {_quote(connection, child_alias)}
        WHERE {orphan_condition} AND {invalid_status}
        """,
        {f"terminal_status_{index}": value for index, value in enumerate(_TERMINAL_ORPHAN_FINAL_STATUSES)},
    )
    if invalid_count:
        _raise_inconsistent("terminal_session.session_owner", invalid_count)

    _delete(
        connection,
        "terminal_session",
        orphan_condition.replace(f"{_quote(connection, child_alias)}.", f"{_quote(connection, 'terminal_session')}."),
    )


def _repair_terminal_control_command(connection: Connection) -> None:
    if not _table_has_columns(connection, "terminal_control_command", ("terminal_session_id",)) or not _table_has_columns(
        connection,
        "terminal_session",
        ("terminal_session_id",),
    ):
        return

    condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'terminal_session')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'terminal_session_id')} = {_qualified(connection, 'terminal_control_command', 'terminal_session_id')})"
    _delete(connection, "terminal_control_command", condition)


def _repair_work_owner_relation(connection: Connection, table_name: str) -> None:
    required_columns = ("work_id", "session_id", "uid")
    if not _table_has_columns(connection, table_name, required_columns) or not _table_has_columns(
        connection,
        "session_reply_work_item",
        ("id", "session_id", "uid"),
    ):
        return

    child_alias = "child"
    parent_alias = "parent"
    mismatch = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {_quote(connection, table_name)} AS {_quote(connection, child_alias)}
        JOIN {_quote(connection, "session_reply_work_item")} AS {_quote(connection, parent_alias)}
          ON {_qualified(connection, parent_alias, "id")} = {_qualified(connection, child_alias, "work_id")}
        WHERE {_qualified(connection, child_alias, "session_id")} <> {_qualified(connection, parent_alias, "session_id")}
           OR {_qualified(connection, child_alias, "uid")} <> {_qualified(connection, parent_alias, "uid")}
           OR {_qualified(connection, child_alias, "session_id")} IS NULL
           OR {_qualified(connection, child_alias, "uid")} IS NULL
           OR {_qualified(connection, parent_alias, "session_id")} IS NULL
           OR {_qualified(connection, parent_alias, "uid")} IS NULL
        """,
    )
    if mismatch:
        _raise_inconsistent(f"{table_name}.work_owner", mismatch)

    condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'session_reply_work_item')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, table_name, 'work_id')})"
    _delete(connection, table_name, condition)


def _repair_stream_event(connection: Connection) -> None:
    if not _table_has_columns(connection, "session_reply_stream_event", ("work_id",)) or not _table_has_columns(
        connection,
        "session_reply_work_item",
        ("id",),
    ):
        return

    condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'session_reply_work_item')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, 'session_reply_stream_event', 'work_id')})"
    _delete(connection, "session_reply_stream_event", condition)


def _repair_audit_tool_detail(connection: Connection) -> None:
    if not _table_has_columns(connection, "audit_tool_detail", ("audit_record_id",)) or not _table_has_columns(
        connection,
        "audit_record",
        ("id",),
    ):
        return

    condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'audit_record')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, 'audit_tool_detail', 'audit_record_id')})"
    _delete(connection, "audit_tool_detail", condition)


def _repair_audit_confirmation_claim(connection: Connection) -> None:
    required_columns = ("audit_record_id", "uid", "session_id")
    if not _table_has_columns(connection, "audit_confirmation_claim", required_columns) or not _table_has_columns(
        connection,
        "audit_record",
        ("id", "uid", "session_id"),
    ):
        return

    child_alias = "child"
    parent_alias = "parent"
    mismatch = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {_quote(connection, "audit_confirmation_claim")} AS {_quote(connection, child_alias)}
        JOIN {_quote(connection, "audit_record")} AS {_quote(connection, parent_alias)}
          ON {_qualified(connection, parent_alias, "id")} = {_qualified(connection, child_alias, "audit_record_id")}
        WHERE {_qualified(connection, child_alias, "uid")} <> {_qualified(connection, parent_alias, "uid")}
           OR {_qualified(connection, child_alias, "session_id")} <> {_qualified(connection, parent_alias, "session_id")}
           OR {_qualified(connection, child_alias, "uid")} IS NULL
           OR {_qualified(connection, child_alias, "session_id")} IS NULL
           OR {_qualified(connection, parent_alias, "uid")} IS NULL
           OR {_qualified(connection, parent_alias, "session_id")} IS NULL
        """,
    )
    if mismatch:
        _raise_inconsistent("audit_confirmation_claim.record_owner", mismatch)

    condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'audit_record')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, 'audit_confirmation_claim', 'audit_record_id')})"
    _delete(connection, "audit_confirmation_claim", condition)


def _repair_audit_execution_record(connection: Connection) -> None:
    required_columns = ("audit_record_id", "audit_tool_detail_id")
    if (
        not _table_has_columns(connection, "audit_execution_record", required_columns)
        or not _table_has_columns(
            connection,
            "audit_record",
            ("id",),
        )
        or not _table_has_columns(connection, "audit_tool_detail", ("id", "audit_record_id"))
    ):
        return

    child_alias = "child"
    detail_alias = "detail"
    mismatch = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {_quote(connection, "audit_execution_record")} AS {_quote(connection, child_alias)}
        JOIN {_quote(connection, "audit_tool_detail")} AS {_quote(connection, detail_alias)}
          ON {_qualified(connection, detail_alias, "id")} = {_qualified(connection, child_alias, "audit_tool_detail_id")}
        WHERE {_qualified(connection, child_alias, "audit_record_id")} <> {_qualified(connection, detail_alias, "audit_record_id")}
           OR {_qualified(connection, child_alias, "audit_record_id")} IS NULL
           OR {_qualified(connection, detail_alias, "audit_record_id")} IS NULL
        """,
    )
    if mismatch:
        _raise_inconsistent("audit_execution_record.detail_record", mismatch)

    condition = (
        f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'audit_record')} AS {_quote(connection, 'record')} "
        f"WHERE {_qualified(connection, 'record', 'id')} = {_qualified(connection, 'audit_execution_record', 'audit_record_id')}) "
        f"OR NOT EXISTS (SELECT 1 FROM {_quote(connection, 'audit_tool_detail')} AS {_quote(connection, 'detail')} "
        f"WHERE {_qualified(connection, 'detail', 'id')} = {_qualified(connection, 'audit_execution_record', 'audit_tool_detail_id')})"
    )
    _delete(connection, "audit_execution_record", condition)


def _repair_knowledge_base_binding(connection: Connection) -> None:
    required_columns = ("knowledge_base_id", "profile_id")
    if not _table_has_columns(connection, "knowledge_base_profile_binding", required_columns):
        return

    binding_alias = "binding"
    duplicates_alias = "duplicates"
    duplicate_count = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {_quote(connection, "knowledge_base_profile_binding")} AS {_quote(connection, binding_alias)}
        JOIN (
            SELECT {_quote(connection, "knowledge_base_id")}, {_quote(connection, "profile_id")}
            FROM {_quote(connection, "knowledge_base_profile_binding")}
            GROUP BY {_quote(connection, "knowledge_base_id")}, {_quote(connection, "profile_id")}
            HAVING COUNT(*) > 1
        ) AS {_quote(connection, duplicates_alias)}
          ON {_qualified(connection, binding_alias, "knowledge_base_id")} = {_qualified(connection, duplicates_alias, "knowledge_base_id")}
         AND {_qualified(connection, binding_alias, "profile_id")} = {_qualified(connection, duplicates_alias, "profile_id")}
        """,
    )
    if duplicate_count:
        _raise_inconsistent("knowledge_base_profile_binding.pair", duplicate_count)

    if not _table_has_columns(connection, "knowledge_base", ("id",)) or not _table_has_columns(connection, "profile", ("id",)):
        return
    condition = (
        f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'knowledge_base')} AS {_quote(connection, 'knowledge_base_parent')} "
        f"WHERE {_qualified(connection, 'knowledge_base_parent', 'id')} = {_qualified(connection, 'knowledge_base_profile_binding', 'knowledge_base_id')}) "
        f"OR NOT EXISTS (SELECT 1 FROM {_quote(connection, 'profile')} AS {_quote(connection, 'profile_parent')} "
        f"WHERE {_qualified(connection, 'profile_parent', 'id')} = {_qualified(connection, 'knowledge_base_profile_binding', 'profile_id')})"
    )
    _delete(connection, "knowledge_base_profile_binding", condition)


def _repair_knowledge_base_document(connection: Connection) -> None:
    if not _table_has_columns(connection, "knowledge_base_document", ("knowledge_base_id",)) or not _table_has_columns(
        connection,
        "knowledge_base",
        ("id",),
    ):
        return

    condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'knowledge_base')} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, 'knowledge_base_document', 'knowledge_base_id')})"
    _delete(connection, "knowledge_base_document", condition)


def _repair_ltm_selection_token(connection: Connection) -> None:
    required_columns = ("profile_id", "target_embedding_channel_id")
    if not _table_has_columns(connection, "long_term_memory_embedding_selection_token", required_columns):
        return
    if not _table_has_columns(connection, "profile", ("id",)) or not _table_has_columns(connection, "channel", ("id",)):
        return

    condition = (
        f"NOT EXISTS (SELECT 1 FROM {_quote(connection, 'profile')} AS {_quote(connection, 'profile_parent')} "
        f"WHERE {_qualified(connection, 'profile_parent', 'id')} = {_qualified(connection, 'long_term_memory_embedding_selection_token', 'profile_id')}) "
        f"OR NOT EXISTS (SELECT 1 FROM {_quote(connection, 'channel')} AS {_quote(connection, 'channel_parent')} "
        f"WHERE {_qualified(connection, 'channel_parent', 'id')} = "
        f"{_qualified(connection, 'long_term_memory_embedding_selection_token', 'target_embedding_channel_id')})"
    )
    _delete(connection, "long_term_memory_embedding_selection_token", condition)


def _set_null_for_invalid_reference(connection: Connection, table_name: str, column_name: str, parent_table: str) -> None:
    if not _table_has_columns(connection, table_name, (column_name,)) or not _table_has_columns(connection, parent_table, ("id",)):
        return

    connection.execute(
        text(
            f"UPDATE {_quote(connection, table_name)} SET {_quote(connection, column_name)} = NULL "
            f"WHERE {_quote(connection, column_name)} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {_quote(connection, parent_table)} AS {_quote(connection, 'parent')} "
            f"WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, table_name, column_name)})"
        )
    )


def _repair_scheduled_task_profile(connection: Connection) -> None:
    if not _table_has_columns(connection, "scheduled_task", ("profile_id", "status")) or not _table_has_columns(
        connection,
        "profile",
        ("id",),
    ):
        return

    connection.execute(
        text(
            f"UPDATE {_quote(connection, 'scheduled_task')} "
            f"SET {_quote(connection, 'profile_id')} = NULL, {_quote(connection, 'status')} = :disabled_status "
            f"WHERE {_quote(connection, 'profile_id')} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {_quote(connection, 'profile')} AS {_quote(connection, 'parent')} "
            f"WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, 'scheduled_task', 'profile_id')})"
        ),
        {"disabled_status": ScheduledTaskStatus.DISABLED.name},
    )


def _raise_for_invalid_required_reference(
    connection: Connection,
    table_name: str,
    column_name: str,
    parent_table: str,
    allow_null: bool = False,
) -> None:
    if not _table_has_columns(connection, table_name, (column_name,)) or not _table_has_columns(connection, parent_table, ("id",)):
        return

    missing_parent_condition = f"NOT EXISTS (SELECT 1 FROM {_quote(connection, parent_table)} AS {_quote(connection, 'parent')} WHERE {_qualified(connection, 'parent', 'id')} = {_qualified(connection, 'child', column_name)})"
    if allow_null:
        invalid_condition = f"{_qualified(connection, 'child', column_name)} IS NOT NULL AND {missing_parent_condition}"
    else:
        invalid_condition = f"{_qualified(connection, 'child', column_name)} IS NULL OR {missing_parent_condition}"
    invalid_count = _count(
        connection,
        f"""
        SELECT COUNT(*)
        FROM {_quote(connection, table_name)} AS {_quote(connection, "child")}
        WHERE ({invalid_condition})
        """,
    )
    if invalid_count:
        _raise_inconsistent(f"{table_name}.{column_name}", invalid_count)


def _raise_for_invalid_nullable_references(connection: Connection) -> None:
    if _table_has_columns(connection, "knowledge_base", ("embedding_channel_id",)) and _table_has_columns(
        connection,
        "channel",
        ("id",),
    ):
        _raise_for_invalid_required_reference(connection, "knowledge_base", "embedding_channel_id", "channel")

    for column_name in ("active_embedding_channel_id", "target_embedding_channel_id", "organization_channel_id"):
        _raise_for_invalid_required_reference(connection, "long_term_memory_store", column_name, "channel", allow_null=True)


def _repair_data(connection: Connection) -> None:
    _repair_work_owner_relation(connection, "context_summary_stage")
    _repair_work_owner_relation(connection, "context_summary_fragment")
    _repair_stream_event(connection)
    _repair_audit_execution_record(connection)
    _repair_audit_confirmation_claim(connection)
    _repair_audit_tool_detail(connection)
    _repair_terminal_control_command(connection)
    _repair_terminal_session(connection)
    _repair_terminal_control_command(connection)

    for table_name in (
        "message",
        "session_event",
        "message_platform_outbox",
        "session_reply_work_item",
        "scheduled_task",
    ):
        _repair_chat_session_relation(connection, table_name, has_uid=True)
    _repair_chat_session_relation(connection, "session_reply_sequence", has_uid=False)
    _repair_work_owner_relation(connection, "context_summary_stage")
    _repair_work_owner_relation(connection, "context_summary_fragment")
    _repair_stream_event(connection)

    _repair_knowledge_base_binding(connection)
    _repair_knowledge_base_document(connection)
    _repair_ltm_selection_token(connection)
    _set_null_for_invalid_reference(connection, "profile", "prompt_id", "prompt")
    _set_null_for_invalid_reference(connection, "chat_session", "profile_override_id", "profile")
    _set_null_for_invalid_reference(connection, "message_platform", "profile_id", "profile")
    _repair_scheduled_task_profile(connection)
    _raise_for_invalid_nullable_references(connection)


def _normalize_ondelete(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _normalize_foreign_key_signature(foreign_key: Mapping[str, Any]) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    options = foreign_key.get("options") or {}
    return (
        tuple(str(column) for column in foreign_key.get("constrained_columns") or ()),
        str(foreign_key.get("referred_table") or ""),
        tuple(str(column) for column in foreign_key.get("referred_columns") or ()),
        _normalize_ondelete(options.get("ondelete", foreign_key.get("ondelete"))),
    )


def _metadata_foreign_key_signature(constraint: ForeignKeyConstraint) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    return (
        tuple(element.parent.name for element in constraint.elements),
        constraint.elements[0].column.table.name,
        tuple(element.column.name for element in constraint.elements),
        _normalize_ondelete(constraint.ondelete),
    )


def _target_table(table_name: str) -> Table | None:
    table = SQLModel.metadata.tables.get(table_name)
    return table if isinstance(table, Table) else None


def _target_foreign_key_constraints(table: Table) -> tuple[ForeignKeyConstraint, ...]:
    return tuple(constraint for constraint in table.foreign_key_constraints if isinstance(constraint, ForeignKeyConstraint))


def _target_new_unique_constraints(table: Table) -> tuple[UniqueConstraint, ...]:
    return tuple(constraint for constraint in table.constraints if isinstance(constraint, UniqueConstraint) and constraint.name in _NEW_UNIQUE_CONSTRAINT_NAMES)


def _sqlite_table_matches_target(connection: Connection, table_name: str) -> bool:
    target = _target_table(table_name)
    if target is None or not _table_exists(connection, table_name):
        return False

    inspector = inspect(connection)
    actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
    if not {column.name for column in target.columns}.issubset(actual_columns):
        return False

    actual_foreign_keys = {_normalize_foreign_key_signature(foreign_key) for foreign_key in inspector.get_foreign_keys(table_name)}
    target_foreign_keys = {_metadata_foreign_key_signature(constraint) for constraint in _target_foreign_key_constraints(target)}
    if actual_foreign_keys != target_foreign_keys:
        return False

    actual_unique_constraints = {unique_constraint.get("name"): tuple(unique_constraint.get("column_names") or ()) for unique_constraint in inspector.get_unique_constraints(table_name)}
    return all(actual_unique_constraints.get(constraint.name) == tuple(column.name for column in constraint.columns) for constraint in _target_new_unique_constraints(target))


def _copy_foreign_key_parents(table: Table, metadata: MetaData, copied: set[str]) -> None:
    for constraint in table.foreign_key_constraints:
        for element in constraint.elements:
            parent = element.column.table
            parent_key = parent.fullname
            if parent_key in copied:
                continue
            copied.add(parent_key)
            _copy_foreign_key_parents(parent, metadata, copied)
            parent.to_metadata(metadata)


def _rebuild_sqlite_table(connection: Connection, table_name: str) -> None:
    target = _target_table(table_name)
    if target is None or not _table_exists(connection, table_name):
        return

    old_metadata = MetaData()
    old_table = Table(table_name, old_metadata, autoload_with=connection)
    missing_columns = [column.name for column in target.columns if column.name not in old_table.c]
    if missing_columns:
        _raise_inconsistent(f"{table_name}.columns", len(missing_columns))

    temporary_name = f"{table_name}__foreign_key_new"
    connection.execute(text(f"DROP TABLE IF EXISTS {_quote(connection, temporary_name)}"))

    new_metadata = MetaData()
    copied_parents: set[str] = set()
    _copy_foreign_key_parents(target, new_metadata, copied_parents)
    new_table = target.to_metadata(new_metadata, name=temporary_name)
    index_statements = tuple(str(CreateIndex(index).compile(dialect=connection.dialect)) for index in target.indexes if index.name)
    for index in tuple(new_table.indexes):
        new_table.indexes.remove(index)

    new_metadata.create_all(connection, tables=[new_table], checkfirst=False)
    columns = tuple(column.name for column in target.columns)
    column_list = ", ".join(_quote(connection, column) for column in columns)
    connection.execute(text(f"INSERT INTO {_quote(connection, temporary_name)} ({column_list}) SELECT {column_list} FROM {_quote(connection, table_name)}"))
    connection.execute(text(f"DROP TABLE {_quote(connection, table_name)}"))
    connection.execute(text(f"ALTER TABLE {_quote(connection, temporary_name)} RENAME TO {_quote(connection, table_name)}"))
    for index_statement in index_statements:
        connection.execute(text(index_statement))


def _migrate_sqlite(connection: Connection) -> None:
    foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar())
    if foreign_keys_enabled:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    _repair_data(connection)
    for table_name in _TARGET_TABLE_ORDER:
        if not _sqlite_table_matches_target(connection, table_name):
            _rebuild_sqlite_table(connection, table_name)

    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        _raise_inconsistent("sqlite.foreign_key_check", len(violations))


def _mysql_unique_signatures(connection: Connection, table_name: str) -> tuple[set[str], set[tuple[str, ...]]]:
    inspector = inspect(connection)
    names: set[str] = set()
    signatures: set[tuple[str, ...]] = set()
    for unique_constraint in inspector.get_unique_constraints(table_name):
        name = unique_constraint.get("name")
        if name:
            names.add(str(name))
        signatures.add(tuple(str(column) for column in unique_constraint.get("column_names") or ()))
    for index in inspector.get_indexes(table_name):
        if index.get("unique"):
            name = index.get("name")
            if name:
                names.add(str(name))
            signatures.add(tuple(str(column) for column in index.get("column_names") or ()))
    return names, signatures


def _add_mysql_constraints(connection: Connection, table_name: str) -> None:
    target = _target_table(table_name)
    if target is None or not _table_exists(connection, table_name):
        return

    unique_names, unique_signatures = _mysql_unique_signatures(connection, table_name)
    for constraint in _target_new_unique_constraints(target):
        signature = tuple(column.name for column in constraint.columns)
        if constraint.name in unique_names or signature in unique_signatures:
            continue
        connection.execute(AddConstraint(constraint, isolate_from_table=False))
        if constraint.name:
            unique_names.add(constraint.name)
        unique_signatures.add(signature)

    actual_foreign_keys = {_normalize_foreign_key_signature(foreign_key) for foreign_key in inspect(connection).get_foreign_keys(table_name)}
    for constraint in _target_foreign_key_constraints(target):
        signature = _metadata_foreign_key_signature(constraint)
        if signature in actual_foreign_keys:
            continue
        connection.execute(AddConstraint(constraint, isolate_from_table=False))
        actual_foreign_keys.add(signature)


def _migrate_mysql(connection: Connection) -> None:
    _repair_data(connection)
    for table_name in _TARGET_TABLE_ORDER:
        _add_mysql_constraints(connection, table_name)


def _migrate_sync(connection: Connection) -> None:
    database_type = connection.dialect.name
    if database_type == "sqlite":
        _migrate_sqlite(connection)
    elif database_type == "mysql":
        _migrate_mysql(connection)
    else:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    foreign_keys_enabled = False
    if database_type == "sqlite":
        foreign_keys_enabled = bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar())

    try:
        await connection.run_sync(_migrate_sync)
    except BaseException:
        if database_type == "sqlite" and foreign_keys_enabled:
            await session.rollback()
        raise
    else:
        if database_type == "sqlite" and foreign_keys_enabled:
            await session.commit()
    finally:
        if database_type == "sqlite" and foreign_keys_enabled:
            connection = await session.connection()
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
