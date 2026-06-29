from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict
from sqlmodel import Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class ScheduledTaskStatus(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class ScheduledTaskBase(SQLModel):
    name: str = Field(index=True, max_length=100)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    message: str
    interval_seconds: int = Field(ge=60)
    status: ScheduledTaskStatus = Field(default=ScheduledTaskStatus.ENABLED, index=True, max_length=20)
    next_run_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    last_run_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    last_message_id: int | None = Field(default=None, index=True)
    run_count: int = Field(default=0, ge=0)


class ScheduledTask(ScheduledTaskBase, table=True):
    __tablename__ = "scheduled_task"

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))


class ScheduledTaskCreate(ScheduledTaskBase):
    pass


class ScheduledTaskResponse(ScheduledTaskBase):
    id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
