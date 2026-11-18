from collections.abc import Iterable

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t
from app.models.subagent import SessionSubagentBoardRevision


async def bump_subagent_board_revisions(db: AsyncSession, session_ids: Iterable[str]) -> None:
    unique_session_ids = sorted(set(session_ids))
    if not unique_session_ids:
        return

    table = SessionSubagentBoardRevision.__table__
    values = [{"session_id": session_id, "revision": 1} for session_id in unique_session_ids]
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "sqlite":
        statement = (
            sqlite_insert(table)
            .values(values)
            .on_conflict_do_update(
                index_elements=[table.c.session_id],
                set_={"revision": table.c.revision + 1},
            )
        )
    elif dialect_name in {"mysql", "mariadb"}:
        statement = mysql_insert(table).values(values).on_duplicate_key_update(revision=table.c.revision + 1)
    else:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=dialect_name))

    await db.execute(statement)
