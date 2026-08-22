from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, DateTime, Enum, UniqueConstraint
from sqlalchemy.dialects import mysql, sqlite

from app.models.knowledge_base import KnowledgeBase
from app.providers.database.bootstrap import iter_migration_scripts
from scripts.knowledge_base_container_migration_mysql import mysql_add_column_ddl
from scripts.knowledge_base_container_migration_schema import (
    NEW_COLUMN_NAMES,
    TARGET_TABLE,
)
from scripts.migration_20260815_expand_knowledge_base_container import MIGRATION_ID


def _column_signature(column) -> tuple[object, ...]:
    type_ = column.type
    signature: tuple[object, ...] = (
        str(type_.compile(dialect=sqlite.dialect())),
        column.nullable,
        column.primary_key,
    )
    if isinstance(type_, Enum):
        signature += (tuple(type_.enums),)
    if isinstance(type_, DateTime):
        signature += (type_.timezone,)
    return signature


def _column_signatures(table) -> dict[str, tuple[object, ...]]:
    return {column.name: _column_signature(column) for column in table.columns}


def _foreign_key_signatures(table) -> set[tuple[object, ...]]:
    signatures = set()
    for constraint in table.foreign_key_constraints:
        columns = tuple(
            sorted(
                (
                    element.parent.name,
                    element.column.table.name,
                    element.column.name,
                )
                for element in constraint.elements
            )
        )
        ondelete = getattr(constraint, "ondelete", None)
        if ondelete is None and constraint.elements:
            ondelete = constraint.elements[0].ondelete
        signatures.add((columns, str(ondelete).upper() if ondelete else None))
    return signatures


def _index_signatures(table) -> set[tuple[object, ...]]:
    return {
        (
            index.name,
            tuple(column.name for column in index.columns),
            bool(index.unique),
        )
        for index in table.indexes
        if index.name
    }


def _normalize_sql(expression: str) -> str:
    expression = expression.replace("`", "").replace('"', "")
    return re.sub(r"\s+", " ", expression).strip().upper()


def _constraint_signatures(table) -> set[tuple[object, ...]]:
    signatures = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name:
            signatures.add(
                (
                    "unique",
                    constraint.name,
                    tuple(column.name for column in constraint.columns),
                )
            )
        elif isinstance(constraint, CheckConstraint) and constraint.name:
            signatures.add(("check", constraint.name, _normalize_sql(str(constraint.sqltext))))
    return signatures


def _assert_column_names_match(model_table) -> None:
    target_names = set(TARGET_TABLE.columns.keys())
    model_names = set(model_table.columns.keys())
    assert target_names == model_names
    for table in (TARGET_TABLE, model_table):
        assert "embedding_model_id" in table.columns
        assert "embedding_dimensions" in table.columns
        assert "model_id" not in table.columns
        assert "dimensions" not in table.columns


def _script_name(script) -> str:
    value = getattr(script, "__file__", None) or getattr(script, "path", None) or getattr(script, "name", None) or getattr(script, "__name__", None) or script
    name = Path(str(value)).name
    return name if name.endswith(".py") else f"{name.rsplit('.', 1)[-1]}.py"


def _mysql_column_ddl(column_name: str) -> str:
    return mysql_add_column_ddl(TARGET_TABLE.c[column_name], mysql.dialect()).upper()


def test_frozen_target_matches_knowledge_base_model() -> None:
    model_table = KnowledgeBase.__table__
    _assert_column_names_match(model_table)
    assert _column_signatures(TARGET_TABLE) == _column_signatures(model_table)
    assert _foreign_key_signatures(TARGET_TABLE) == _foreign_key_signatures(model_table)
    assert _index_signatures(TARGET_TABLE) == _index_signatures(model_table)
    assert _constraint_signatures(TARGET_TABLE) == _constraint_signatures(model_table)


@pytest.mark.parametrize(
    "column_name",
    ["active_collection_name", "target_collection_name", "old_collection_name"],
)
def test_collection_name_indexes_are_unique(column_name: str) -> None:
    expected = (f"ix_knowledge_base_{column_name}", (column_name,), True)
    for table in (TARGET_TABLE, KnowledgeBase.__table__):
        assert expected in _index_signatures(table)


def test_frozen_enum_storage_names_and_lengths() -> None:
    expected = {
        "knowledge_base_type": ("USER", "LLM_MANAGED"),
        "old_collection_cleanup_status": (
            "NONE",
            "PENDING",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
        ),
        "index_status": ("PENDING", "READY", "REINDEXING", "FAILED"),
    }
    for column_name, names in expected.items():
        target_type = TARGET_TABLE.c[column_name].type
        model_type = KnowledgeBase.__table__.c[column_name].type
        assert tuple(target_type.enums) == names
        assert tuple(model_type.enums) == names
        assert target_type.length == model_type.length == max(map(len, names))


def test_migration_id_is_frozen() -> None:
    assert MIGRATION_ID == "20260815_expand_knowledge_base_container_v1"


def test_migration_scan_order_excludes_schema_helpers() -> None:
    names = [_script_name(script) for script in iter_migration_scripts()]
    entry = "migration_20260815_expand_knowledge_base_container.py"
    core_foreign_keys = "migration_20260816_add_core_foreign_keys.py"
    assert names.index(entry) < names.index(core_foreign_keys)
    assert not any(name.startswith("knowledge_base_container_migration_") for name in names)


@pytest.mark.parametrize("column_name", sorted(NEW_COLUMN_NAMES))
def test_mysql_add_column_ddl_is_column_only(column_name: str) -> None:
    column = TARGET_TABLE.c[column_name]
    ddl = _mysql_column_ddl(column_name)
    assert "ADD COLUMN" in ddl
    assert "FOREIGN KEY" not in ddl
    if column.nullable:
        assert "DEFAULT" not in ddl
    else:
        assert "DEFAULT" in ddl


def test_mysql_enum_defaults_use_database_member_names() -> None:
    assert "DEFAULT 'USER'" in _mysql_column_ddl("knowledge_base_type")
    assert "DEFAULT 'NONE'" in _mysql_column_ddl("old_collection_cleanup_status")
    assert "DEFAULT 'PENDING'" in _mysql_column_ddl("index_status")
