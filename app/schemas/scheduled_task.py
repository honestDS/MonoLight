from pydantic import BaseModel, Field, model_validator

from app.core.utils.scheduled_time import validate_schedule
from app.models.scheduled_task import ScheduledTaskStatus


class ScheduledTaskSchedule(BaseModel):
    interval_seconds: int | None = Field(default=None, ge=60)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    run_once: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_schedule_fields(self) -> "ScheduledTaskSchedule":
        self.cron_expression = validate_schedule(
            interval_seconds=self.interval_seconds,
            cron_expression=self.cron_expression,
            timezone=self.timezone,
            run_once=self.run_once,
        )
        return self


class ScheduledTaskCreateRequest(ScheduledTaskSchedule):
    name: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)


class ScheduledTaskUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    message: str | None = Field(default=None, min_length=1)
    interval_seconds: int | None = Field(default=None, ge=60)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    run_once: bool | None = Field(default=None, strict=True)
    status: ScheduledTaskStatus | None = None
