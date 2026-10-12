from sqlalchemy import text

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t

MIGRATION_ID = "20261013_01_add_user_file_cleanup"


async def migrate(session):
    dialect_name = session.get_bind().dialect.name
    if dialect_name not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=dialect_name))

    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_file_cleanup (
                uid VARCHAR(50) NOT NULL PRIMARY KEY,
                legacy_session_ids JSON NOT NULL
            )
            """
        )
    )
