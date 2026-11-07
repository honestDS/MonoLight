from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import ForeignKeyConstraint
from sqlmodel import JSON, Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class Subagent(SQLModel, table=True):
    __tablename__ = "subagent"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "uid"],
            ["chat_session.session_id", "chat_session.uid"],
            name="fk_subagent_session_owner",
            ondelete="CASCADE",
        ),
    )

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True, max_length=32)
    uid: str = Field(index=True, max_length=100, nullable=False)
    session_id: str = Field(index=True, max_length=100, nullable=False)
    profile_id: int = Field(index=True, foreign_key="profile.id", ondelete="CASCADE", nullable=False)
    name: str = Field(min_length=1, max_length=32, nullable=False)
    active_child_work_id: int | None = Field(default=None, index=True)
    latest_child_work_id: int | None = Field(default=None, index=True)
    last_called_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


class SubagentWork(SQLModel, table=True):
    __tablename__ = "subagent_work"

    child_work_id: int = Field(
        primary_key=True,
        foreign_key="session_reply_work_item.id",
        ondelete="CASCADE",
    )
    subagent_id: str = Field(
        index=True,
        max_length=32,
        foreign_key="subagent.id",
        ondelete="CASCADE",
        nullable=False,
    )
    current_work_id: int = Field(index=True, nullable=False)
    waiting_confirmation: bool = Field(default=False, nullable=False)
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


class SubagentToolCall(SQLModel, table=True):
    __tablename__ = "subagent_tool_call"
    __table_args__ = (
        UniqueConstraint(
            "parent_work_id",
            "tool_call_id",
            name="uq_subagent_tool_call_parent_call",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    subagent_id: str = Field(
        index=True,
        max_length=32,
        foreign_key="subagent.id",
        ondelete="CASCADE",
        nullable=False,
    )
    child_work_id: int = Field(
        index=True,
        foreign_key="subagent_work.child_work_id",
        ondelete="CASCADE",
        nullable=False,
    )
    parent_work_id: int = Field(
        index=True,
        foreign_key="session_reply_work_item.id",
        ondelete="CASCADE",
        nullable=False,
    )
    tool_call_id: str = Field(max_length=100, nullable=False)
    arguments: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    tool_result_message_id: int | None = Field(default=None, index=True)
    response_id: str | None = Field(default=None, max_length=100)
    delivered: bool = Field(default=False, nullable=False)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
