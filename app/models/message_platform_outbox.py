from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class MessagePlatformOutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class MessagePlatformOutbox(SQLModel, table=True):
    __tablename__ = "message_platform_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "uid"],
            ["chat_session.session_id", "chat_session.uid"],
            name="fk_message_platform_outbox_session_owner",
            ondelete="CASCADE",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    dedupe_key: str = Field(unique=True, index=True, max_length=64)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    source: str = Field(index=True, max_length=50)
    event: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: MessagePlatformOutboxStatus = Field(default=MessagePlatformOutboxStatus.PENDING, index=True, max_length=20)
    attempt_count: int = Field(default=0, ge=0)
    next_attempt_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    last_error: str | None = Field(default=None, max_length=1000)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    sent_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
