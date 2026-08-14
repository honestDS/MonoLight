import pytest
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.sql import select

from app.providers.database.time import build_database_timestamp_expression


@pytest.mark.parametrize(
    ("database_type", "dialect", "required_fragments", "forbidden_fragments"),
    [
        ("sqlite", sqlite.dialect(), ["unixepoch()"], ["extract"]),
        ("mysql", mysql.dialect(), ["unix_timestamp", "current_timestamp"], ["extract"]),
    ],
)
def test_database_timestamp_expression_uses_dialect_specific_sql(
    database_type,
    dialect,
    required_fragments,
    forbidden_fragments,
):
    sql = str(select(build_database_timestamp_expression(database_type)).compile(dialect=dialect)).lower()

    for fragment in required_fragments:
        assert fragment in sql
    for fragment in forbidden_fragments:
        assert fragment not in sql
