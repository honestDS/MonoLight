from datetime import datetime
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class SessionReplyStreamEvent(SQLModel, table=True):
    __tablename__ = "session_reply_stream_event"
    __table_args__ = (UniqueConstraint("work_id", "sequence_no", name="uq_session_reply_stream_event_sequence"),)

    id: int | None = Field(default=None, primary_key=True, index=True)
    work_id: int = Field(index=True)
    sequence_no: int = Field(index=True)
    event: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
