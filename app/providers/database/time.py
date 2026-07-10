from datetime import datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def get_database_time(db: AsyncSession) -> datetime:
    result = await db.execute(select(func.current_timestamp()))
    now = result.scalar_one()
    if not isinstance(now, datetime):
        raise TypeError("Database current timestamp did not return a datetime")
    return now


async def get_database_timestamp(db: AsyncSession) -> int:
    bind = db.get_bind()
    timestamp_expression = func.unixepoch() if bind.dialect.name == "sqlite" else cast(func.extract("epoch", func.current_timestamp()), Integer)
    result = await db.execute(select(timestamp_expression))
    timestamp = result.scalar_one()
    if not isinstance(timestamp, int):
        raise TypeError("Database current timestamp did not return an integer")
    return timestamp
