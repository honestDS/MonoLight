from datetime import datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TIME_TYPE_INVALID
from app.core.i18n import t


async def get_database_time(db: AsyncSession) -> datetime:
    result = await db.execute(select(func.current_timestamp()))
    now = result.scalar_one()
    if not isinstance(now, datetime):
        raise TypeError(t(ERR_DATABASE_TIME_TYPE_INVALID, expected_type="datetime"))
    return now


async def get_database_timestamp(db: AsyncSession) -> int:
    bind = db.get_bind()
    timestamp_expression = func.unixepoch() if bind.dialect.name == "sqlite" else cast(func.extract("epoch", func.current_timestamp()), Integer)
    result = await db.execute(select(timestamp_expression))
    timestamp = result.scalar_one()
    if not isinstance(timestamp, int):
        raise TypeError(t(ERR_DATABASE_TIME_TYPE_INVALID, expected_type="integer"))
    return timestamp
