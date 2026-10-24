"""Schema for terminal output watch requests."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import (
    ERR_TERMINAL_WATCH_CONDITION_INVALID,
    TERMINAL_WATCH_MAX_OUTPUT_BYTES,
)
from app.core.i18n import t
from app.core.terminal.schemas import TerminalSessionId


class TerminalWatchRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )

    terminal_session_id: TerminalSessionId
    after_offset: int = Field(ge=0)
    condition: Literal["output", "process_exit", "output_contains", "quiet_for"] = "output"
    text: str | None = Field(default=None, min_length=1, max_length=512)
    settle_seconds: float = Field(default=2, ge=1, le=60)
    max_bytes: int = Field(default=TERMINAL_WATCH_MAX_OUTPUT_BYTES, ge=1, le=TERMINAL_WATCH_MAX_OUTPUT_BYTES)

    @model_validator(mode="after")
    def validate_condition_text(self) -> "TerminalWatchRequest":
        if (self.condition == "output_contains") != (self.text is not None):
            raise ValueError(
                t(
                    ERR_TERMINAL_WATCH_CONDITION_INVALID,
                    detail="text is required only for output_contains",
                )
            )
        return self
