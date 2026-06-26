from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class BackgroundTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackgroundTaskReplyStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BackgroundTaskBase(SQLModel):
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    profile_id: int = Field(foreign_key="profile.id", index=True)
    tool_call_id: str = Field(index=True, max_length=100)
    tool_name: str = Field(index=True, max_length=100)
    status: BackgroundTaskStatus = Field(default=BackgroundTaskStatus.PENDING, index=True, max_length=20)
    arguments: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None)
    auto_reply: bool = Field(default=True)
    reply_status: BackgroundTaskReplyStatus = Field(default=BackgroundTaskReplyStatus.NONE, index=True, max_length=20)
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    attempt_count: int = Field(default=0, ge=0)
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class BackgroundTask(BackgroundTaskBase, table=True):
    __tablename__ = "background_task"

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class BackgroundTaskCreate(BackgroundTaskBase):
    pass


class BackgroundTaskResponse(BackgroundTaskBase):
    id: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)
