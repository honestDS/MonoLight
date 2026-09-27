from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKeyConstraint
from sqlmodel import Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class SessionReplyProviderRequestPurpose(StrEnum):
    MAIN_DIALOGUE = "main_dialogue"
    BACKGROUND_SUMMARY = "background_summary"
    SCHEDULED_SUMMARY = "scheduled_summary"
    AUDIT_AUXILIARY = "audit_auxiliary"
    CONFIRMED_AUXILIARY = "confirmed_auxiliary"
    LEGACY_UNKNOWN = "legacy_unknown"

    @property
    def contributes_to_session_totals(self) -> bool:
        return self is SessionReplyProviderRequestPurpose.MAIN_DIALOGUE


class SessionReplyProviderUsage(SQLModel, table=True):
    __tablename__ = "session_reply_provider_usage"
    __table_args__ = (
        UniqueConstraint("provider_request_id", name="uq_session_reply_provider_usage_request"),
        ForeignKeyConstraint(
            ["work_id", "session_id", "uid"],
            [
                "session_reply_work_item.id",
                "session_reply_work_item.session_id",
                "session_reply_work_item.uid",
            ],
            name="fk_session_reply_provider_usage_work_owner",
            ondelete="CASCADE",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    provider_request_id: str = Field(index=True, max_length=64)
    work_id: int = Field(index=True, ge=1)
    session_id: str = Field(index=True, max_length=100)
    uid: str = Field(index=True, max_length=100)
    input_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    request_purpose: SessionReplyProviderRequestPurpose = Field(index=True, max_length=40)
    output_tokens: int = Field(ge=0)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
