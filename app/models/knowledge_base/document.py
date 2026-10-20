from datetime import datetime
from typing import Any

from sqlalchemy import Text
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class KnowledgeBaseDocument(SQLModel, table=True):
    """知识库文档，保存原文和对应的向量分块信息"""

    __tablename__ = "knowledge_base_document"

    id: int | None = Field(default=None, primary_key=True, index=True)
    knowledge_base_id: int = Field(
        nullable=False,
        index=True,
        foreign_key="knowledge_base.id",
        ondelete="CASCADE",
        description="所属知识库ID",
    )
    filename: str = Field(nullable=False, max_length=255, description="导入的文件名")
    content: str = Field(sa_column=Column(Text, nullable=False), description="文档原文")
    chunk_size: int = Field(nullable=False, description="分块大小")
    chunk_overlap: int = Field(nullable=False, description="分块重叠")
    batch_size: int = Field(nullable=False, description="批处理大小")
    chunk_count: int = Field(default=0, nullable=False, description="生成的分块数量")
    chunk_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON), description="向量库中的分块ID列表")
    metadata_: dict[str, Any] = Field(default_factory=dict, sa_column=Column("metadata", JSON), description="文档元数据")
    created_at: datetime | None = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime | None = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), onupdate=get_local_time))
