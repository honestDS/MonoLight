from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKeyConstraint
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class SessionEvent(SQLModel, table=True):
    __tablename__ = "session_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "uid"],
            ["chat_session.session_id", "chat_session.uid"],
            name="fk_session_event_session_owner",
            ondelete="CASCADE",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    dedupe_key: str = Field(unique=True, max_length=64)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    event: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
