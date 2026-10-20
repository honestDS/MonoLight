from datetime import datetime

from sqlalchemy import DDL, Integer, Text, event
from sqlmodel import Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time

from .base import KnowledgeBase


class KnowledgeBaseCollectionOwner(SQLModel, table=True):
    __tablename__ = "knowledge_base_collection_owner"

    collection_name: str = Field(primary_key=True, max_length=255)
    knowledge_base_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="knowledge_base.id",
        ondelete="SET NULL",
    )
    cleanup_revision: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default="0"),
    )
    cleanup_attempt_count: int = Field(default=0, ge=0)
    cleanup_error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), onupdate=get_local_time),
    )


_COLLECTION_OWNER_FIELDS = (
    "collection_name",
    "active_collection_name",
    "target_collection_name",
    "old_collection_name",
)
_COLLECTION_OWNER_TRIGGER_NAMES = (
    "trg_knowledge_base_collection_owner_before_insert",
    "trg_knowledge_base_collection_owner_after_insert",
    "trg_knowledge_base_collection_owner_before_update",
    "trg_knowledge_base_collection_owner_after_update",
)
_COLLECTION_OWNER_TRIGGER_ERROR = "knowledge_base.collection_owner"


def _collection_owner_quote(connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _collection_owner_qualified(connection, alias: str, column: str) -> str:
    return f"{_collection_owner_quote(connection, alias)}.{_collection_owner_quote(connection, column)}"


def _collection_owner_new_expression(connection, prefix: str, column: str) -> str:
    return f"{prefix}.{_collection_owner_quote(connection, column)}"


def _collection_owner_nonempty(expression: str) -> str:
    return f"{expression} IS NOT NULL AND TRIM({expression}) <> ''"


def _collection_owner_columns_sql(connection) -> str:
    return ", ".join(
        _collection_owner_quote(connection, column)
        for column in (
            "collection_name",
            "knowledge_base_id",
            "cleanup_revision",
            "cleanup_attempt_count",
            "cleanup_error",
            "created_at",
            "updated_at",
        )
    )


def _collection_owner_conflict_condition(connection, *, updating: bool) -> str:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    owner_alias = _collection_owner_quote(connection, "owner_row")
    checks = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, "NEW", field)
        owner_condition = f"{_collection_owner_qualified(connection, 'owner_row', 'collection_name')} = {expression}"
        if updating:
            owner_condition += f" AND ({_collection_owner_qualified(connection, 'owner_row', 'knowledge_base_id')} IS NULL OR {_collection_owner_qualified(connection, 'owner_row', 'knowledge_base_id')} <> OLD.{_collection_owner_quote(connection, 'id')})"
        checks.append(f"({_collection_owner_nonempty(expression)} AND EXISTS (SELECT 1 FROM {owner} AS {owner_alias} WHERE {owner_condition}))")
    return " OR ".join(checks)


def _collection_owner_cleanup_statement(connection) -> str:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    keep_conditions = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, "NEW", field)
        keep_conditions.append(f"({_collection_owner_nonempty(expression)} AND {_collection_owner_quote(connection, 'collection_name')} = {expression})")
    return (
        f"UPDATE {owner} SET "
        f"{_collection_owner_quote(connection, 'knowledge_base_id')} = NULL, "
        f"{_collection_owner_quote(connection, 'cleanup_revision')} = {_collection_owner_quote(connection, 'cleanup_revision')} + 1, "
        f"{_collection_owner_quote(connection, 'cleanup_attempt_count')} = 0, "
        f"{_collection_owner_quote(connection, 'cleanup_error')} = NULL, "
        f"{_collection_owner_quote(connection, 'updated_at')} = CURRENT_TIMESTAMP "
        f"WHERE {_collection_owner_quote(connection, 'knowledge_base_id')} = NEW.{_collection_owner_quote(connection, 'id')} "
        f"AND NOT ({' OR '.join(keep_conditions)})"
    )


def _sqlite_collection_owner_registration_statements(connection, prefix: str) -> list[str]:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    columns = _collection_owner_columns_sql(connection)
    statements = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, prefix, field)
        statements.append(f"INSERT OR IGNORE INTO {owner} ({columns}) SELECT {expression}, {_collection_owner_new_expression(connection, prefix, 'id')}, 0, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP WHERE {_collection_owner_nonempty(expression)}")
    return statements


def _mysql_collection_owner_registration_statements(connection, prefix: str) -> list[str]:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    owner_alias = _collection_owner_quote(connection, "owner_row")
    owner_collection = _collection_owner_qualified(connection, "owner_row", "collection_name")
    owner_knowledge_base = _collection_owner_qualified(connection, "owner_row", "knowledge_base_id")
    columns = _collection_owner_columns_sql(connection)
    statements = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, prefix, field)
        knowledge_base_id = _collection_owner_new_expression(connection, prefix, "id")
        statements.append(
            f"IF {_collection_owner_nonempty(expression)} THEN "
            f"INSERT IGNORE INTO {owner} ({columns}) VALUES ({expression}, {knowledge_base_id}, 0, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP); "
            f"IF NOT EXISTS (SELECT 1 FROM {owner} AS {owner_alias} WHERE {owner_collection} = {expression} AND {owner_knowledge_base} = {knowledge_base_id}) THEN "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_COLLECTION_OWNER_TRIGGER_ERROR}'; "
            f"END IF; "
            f"END IF;"
        )
    return statements


def _collection_owner_trigger_statements(connection) -> list[str]:
    knowledge_base = _collection_owner_quote(connection, KnowledgeBase.__table__.name)
    before_insert_condition = _collection_owner_conflict_condition(connection, updating=False)
    before_update_condition = _collection_owner_conflict_condition(connection, updating=True)
    cleanup = _collection_owner_cleanup_statement(connection)

    if connection.dialect.name == "sqlite":
        registration = ";\n".join(_sqlite_collection_owner_registration_statements(connection, "NEW"))
        return [
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[0])} BEFORE INSERT ON {knowledge_base} FOR EACH ROW WHEN ({before_insert_condition}) BEGIN SELECT RAISE(ABORT, '{_COLLECTION_OWNER_TRIGGER_ERROR}'); END",
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[1])} AFTER INSERT ON {knowledge_base} FOR EACH ROW BEGIN {registration}; END",
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[2])} BEFORE UPDATE ON {knowledge_base} FOR EACH ROW WHEN ({before_update_condition}) BEGIN SELECT RAISE(ABORT, '{_COLLECTION_OWNER_TRIGGER_ERROR}'); END",
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[3])} AFTER UPDATE ON {knowledge_base} FOR EACH ROW BEGIN {registration}; {cleanup}; END",
        ]

    registration = " ".join(_mysql_collection_owner_registration_statements(connection, "NEW"))
    return [
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[0])} BEFORE INSERT ON {knowledge_base} FOR EACH ROW BEGIN IF ({before_insert_condition}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_COLLECTION_OWNER_TRIGGER_ERROR}'; END IF; END",
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[1])} AFTER INSERT ON {knowledge_base} FOR EACH ROW BEGIN {registration} END",
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[2])} BEFORE UPDATE ON {knowledge_base} FOR EACH ROW BEGIN IF ({before_update_condition}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_COLLECTION_OWNER_TRIGGER_ERROR}'; END IF; END",
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[3])} AFTER UPDATE ON {knowledge_base} FOR EACH ROW BEGIN {registration} {cleanup}; END",
    ]


@event.listens_for(KnowledgeBaseCollectionOwner.__table__, "after_create")
def _install_collection_owner_triggers(target, connection, **kwargs) -> None:
    install_knowledge_base_collection_owner_triggers(connection)


def install_knowledge_base_collection_owner_triggers(connection) -> None:
    if connection.dialect.name not in {"sqlite", "mysql"}:
        return

    for trigger_name in _COLLECTION_OWNER_TRIGGER_NAMES:
        connection.execute(DDL(f"DROP TRIGGER IF EXISTS {_collection_owner_quote(connection, trigger_name)}"))
    for statement in _collection_owner_trigger_statements(connection):
        connection.execute(DDL(statement))
