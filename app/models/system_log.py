from datetime import datetime

from sqlalchemy import Index
from sqlmodel import (
    Column,
    DateTime,
    Field,
    SQLModel,
)

from app.core.utils.time import get_local_time


class SystemLogBase(SQLModel):
    level: str = Field(index=True, max_length=20)
    module: str = Field(index=True, max_length=100)
    message: str = Field(nullable=False)
    uid: str | None = Field(default=None, index=True, max_length=50)
    session_id: str | None = Field(default=None, index=True, max_length=100)
    extra: str | None = Field(default=None)  # JSON string for extra data


class SystemLog(SystemLogBase, table=True):
    __tablename__ = "system_log"
    __table_args__ = (
        Index("ix_system_log_created_at_id", "created_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )


class SystemLogCreate(SystemLogBase):
    created_at: datetime | None = None


class SystemLogResponse(SystemLogBase):
    id: int
    created_at: datetime
