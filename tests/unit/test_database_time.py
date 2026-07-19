from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.sql import select

from app.providers.database.time import build_database_timestamp_expression


def test_database_timestamp_expression_uses_sqlite_unixepoch():
    sql = str(select(build_database_timestamp_expression("sqlite")).compile(dialect=sqlite.dialect()))

    assert "unixepoch()" in sql.lower()
    assert "extract" not in sql.lower()


def test_database_timestamp_expression_uses_mysql_unix_timestamp():
    sql = str(select(build_database_timestamp_expression("mysql")).compile(dialect=mysql.dialect()))

    assert "unix_timestamp" in sql.lower()
    assert "current_timestamp" in sql.lower()
    assert "extract" not in sql.lower()


def test_database_timestamp_expression_uses_postgresql_extract_epoch():
    sql = str(select(build_database_timestamp_expression("postgresql")).compile(dialect=postgresql.dialect()))

    assert "extract(epoch" in sql.lower()
    assert "current_timestamp" in sql.lower()
