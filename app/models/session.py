from datetime import datetime
from sqlmodel import Column, DateTime, Field, SQLModel
from app.core.utils.dt import get_local_time

class ChatSession(SQLModel, table=True):
    """
    会话元数据表，用于存储 LLM 生成的标题等信息。
    与 Message 表通过 session_id 逻辑关联。
    """
    __tablename__ = "chat_session"

    session_id: str = Field(primary_key=True, max_length=100, index=True)
    uid: str = Field(index=True, max_length=100)
    title: str | None = Field(default=None, max_length=255)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
