from typing import Any

from pydantic import BaseModel, Field


class BackgroundTaskResult(BaseModel):
    status: str
    tool_name: str
    summary: str
    content: Any = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
