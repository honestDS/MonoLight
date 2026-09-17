from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKeyConstraint
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class SessionTodoPlan(SQLModel, table=True):
    __tablename__ = "session_todo_plan"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "uid"],
            ["chat_session.session_id", "chat_session.uid"],
            name="fk_session_todo_plan_session_owner",
            ondelete="CASCADE",
        ),
    )

    session_id: str = Field(primary_key=True, max_length=100)
    uid: str = Field(index=True, max_length=100)
    todos: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    revision: int = Field(default=0, ge=0, nullable=False)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
