from sqlalchemy import JSON, Column, ForeignKey, Integer, MetaData, Table, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261104_01_split_session_reply_checkpoint"

_BATCH_SIZE = 25

_METADATA = MetaData()
_WORK_ITEM = Table(
    "session_reply_work_item",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("execution_state", JSON),
)
_WORK_CHECKPOINT = Table(
    "session_reply_work_checkpoint",
    _METADATA,
    Column(
        "work_id",
        Integer,
        ForeignKey("session_reply_work_item.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("checkpoint", JSON, nullable=False),
)


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: _WORK_CHECKPOINT.create(sync_connection, checkfirst=True))

    last_id: int | None = None
    while True:
        statement = select(_WORK_ITEM.c.id, _WORK_ITEM.c.execution_state).order_by(_WORK_ITEM.c.id.asc()).limit(_BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(_WORK_ITEM.c.id > last_id)

        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return

        for row in rows:
            work_id = row["id"]
            state = row["execution_state"]
            if state is None:
                continue
            if not isinstance(state, dict):
                raise RuntimeError(f"{MIGRATION_ID}: work item {work_id} has a non-object execution_state")
            if "dispatcher_checkpoint" not in state:
                continue

            checkpoint = state["dispatcher_checkpoint"]
            if not isinstance(checkpoint, dict):
                raise RuntimeError(f"{MIGRATION_ID}: work item {work_id} has a non-object dispatcher_checkpoint")

            existing = (await session.execute(select(_WORK_CHECKPOINT.c.checkpoint).where(_WORK_CHECKPOINT.c.work_id == work_id))).mappings().first()
            if existing is None:
                await session.execute(insert(_WORK_CHECKPOINT).values(work_id=work_id, checkpoint=checkpoint))
            elif existing["checkpoint"] != checkpoint:
                raise RuntimeError(f"{MIGRATION_ID}: work item {work_id} has a conflicting checkpoint")

            updated_state = dict(state)
            del updated_state["dispatcher_checkpoint"]
            await session.execute(update(_WORK_ITEM).where(_WORK_ITEM.c.id == work_id).values(execution_state=updated_state))

        last_id = rows[-1]["id"]
