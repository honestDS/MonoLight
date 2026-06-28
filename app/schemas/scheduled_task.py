from pydantic import BaseModel, Field

from app.models.scheduled_task import ScheduledTaskStatus


class ScheduledTaskCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)
    interval_seconds: int = Field(ge=60)


class ScheduledTaskUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    message: str | None = Field(default=None, min_length=1)
    interval_seconds: int | None = Field(default=None, ge=60)
    status: ScheduledTaskStatus | None = None
