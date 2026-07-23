from datetime import datetime

from sqlmodel import Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class ChatSession(SQLModel, table=True):
    """
    会话元数据表，用于存储 LLM 生成的标题等信息。
    与 Message 表通过 session_id 逻辑关联。
    """

    __tablename__ = "chat_session"

    session_id: str = Field(primary_key=True, max_length=100, index=True)
    uid: str = Field(index=True, max_length=100)
    profile_id: int | None = Field(default=None, index=True)
    source: str = Field(default="http", max_length=50, index=True)
    reply_target_source: str = Field(
        default="http",
        max_length=50,
        index=True,
        description="预留兼容字段，当前不参与会话来源判断、消息投递或网页通信模式切换",
    )
    title: str | None = Field(default=None, max_length=255)
    enable_markdown: bool = Field(default=False)
    context_summary: str | None = Field(default=None)
    context_summary_message_id: int | None = Field(default=None, index=True)
    context_summary_revision: int = Field(default=0, ge=0)
    context_content_revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
