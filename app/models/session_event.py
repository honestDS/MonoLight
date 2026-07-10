from datetime import datetime
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class SessionEvent(SQLModel, table=True):
    __tablename__ = "session_event"

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    event: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
