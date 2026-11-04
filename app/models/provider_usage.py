from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Column, Date, DateTime, Index, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlmodel import Field, SQLModel


class ProviderUsageType(StrEnum):
    MAIN_DIALOGUE = "main_dialogue"
    BACKGROUND_SUMMARY = "background_summary"
    SCHEDULED_SUMMARY = "scheduled_summary"
    SCHEDULED_TASK = "scheduled_task"
    AUDIT_AUXILIARY = "audit_auxiliary"
    CONFIRMED_AUXILIARY = "confirmed_auxiliary"
    LEGACY_UNKNOWN = "legacy_unknown"
    CONTEXT_SUMMARY = "context_summary"
    SESSION_TITLE = "session_title"
    MEMORY_RECALL_PRECHECK = "memory_recall_precheck"
    SAFETY_AUDIT = "safety_audit"
    TASK_AUTHORIZATION = "task_authorization"
    MEMORY_ORGANIZATION = "memory_organization"
    KNOWLEDGE_ORGANIZATION = "knowledge_organization"
    CHANNEL_TEST = "channel_test"
    PLATFORM_NOTIFICATION = "platform_notification"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    IMAGE_GENERATION = "image_generation"
    OTHER_AUXILIARY = "other_auxiliary"

    @property
    def contributes_to_session_totals(self) -> bool:
        return self is ProviderUsageType.MAIN_DIALOGUE


class ProviderUsageBase(SQLModel):
    type: str = Field(max_length=40, nullable=False)
    channel_key: str | None = Field(default=None, max_length=32)
    channel_id: int | None = Field(default=None)
    channel_name: str | None = Field(default=None, max_length=100)
    model_id: str | None = Field(default=None, sa_type=Text().with_variant(LONGTEXT(), "mysql"))
    response_model_id: str | None = Field(default=None, sa_type=Text().with_variant(LONGTEXT(), "mysql"))
    protocol: str | None = Field(default=None, max_length=40)


class ProviderUsageDetail(ProviderUsageBase, table=True):
    __tablename__ = "provider_usage_detail"

    provider_request_id: str = Field(primary_key=True, max_length=64)
    created_at: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=False, index=True))
    uid: str | None = Field(default=None, index=True, max_length=100)
    session_id: str | None = Field(default=None, max_length=100)
    work_id: int | None = Field(default=None)
    input_tokens: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    cached_tokens: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    output_tokens: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    image_count: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    rerank_search_units: int | None = Field(default=None, sa_column=Column(BigInteger, nullable=True))


class ProviderUsageDaily(ProviderUsageBase, table=True):
    __tablename__ = "provider_usage_daily"
    __table_args__ = (
        Index("ix_provider_usage_daily_usage_date_type", "usage_date", "type"),
        Index("ix_provider_usage_daily_channel_key_usage_date", "channel_key", "usage_date"),
    )

    bucket_key: str = Field(primary_key=True, max_length=64)
    usage_date: date = Field(sa_column=Column(Date, nullable=False))
    input_tokens: int = Field(sa_column=Column(BigInteger, nullable=False))
    cached_tokens: int = Field(sa_column=Column(BigInteger, nullable=False))
    output_tokens: int = Field(sa_column=Column(BigInteger, nullable=False))
    image_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    rerank_search_units: int = Field(sa_column=Column(BigInteger, nullable=False))
    request_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    input_tokens_reported_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    cached_tokens_reported_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    output_tokens_reported_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    image_count_reported_count: int = Field(sa_column=Column(BigInteger, nullable=False))
    rerank_search_units_reported_count: int = Field(sa_column=Column(BigInteger, nullable=False))
