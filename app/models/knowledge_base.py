from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ConfigDict
from sqlalchemy import Text
from sqlmodel import JSON, Column, DateTime, Field, Relationship, SQLModel

from app.core.utils.time import get_local_time

if TYPE_CHECKING:
    from app.models.profile import Profile


class KnowledgeBaseCore(SQLModel):
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, max_length=500, description="知识库描述")
    profile_id: int = Field(foreign_key="profile.id", nullable=False, description="绑定的配置文件ID，创建后不可修改")
    collection_name: str = Field(unique=True, index=True, nullable=False, max_length=100, description="ChromaDB 中的 collection 名称")


class KnowledgeBase(KnowledgeBaseCore, table=True):
    __tablename__ = "knowledge_base"

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
    updated_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), onupdate=get_local_time),
    )

    profile: Optional["Profile"] = Relationship()


class KnowledgeBaseDocument(SQLModel, table=True):
    """知识库文档，保存原文和对应的向量分块信息"""

    __tablename__ = "knowledge_base_document"

    id: int | None = Field(default=None, primary_key=True, index=True)
    knowledge_base_id: int = Field(foreign_key="knowledge_base.id", nullable=False, index=True, description="所属知识库ID")
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


class KnowledgeBaseCreate(SQLModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="知识库描述")
    profile_id: int = Field(..., gt=0, description="配置文件的ID")


class KnowledgeBaseResponse(KnowledgeBaseCore):
    id: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBaseProfileOption(SQLModel):
    id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBaseListResponse(SQLModel):
    items: list[KnowledgeBaseResponse]
    total: int
    profiles: list[KnowledgeBaseProfileOption]
    available_profiles: list[KnowledgeBaseProfileOption]


class KnowledgeBaseUpdate(SQLModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="知识库描述")


class KnowledgeBaseQueryTestRequest(SQLModel):
    query: str = Field(..., min_length=1, max_length=5000, description="检索词")
    top_k: int = Field(5, ge=1, le=50, description="返回最相似的结果数量")


class KnowledgeBaseQueryTestItem(SQLModel):
    id: str
    content: str
    distance: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseQueryTestResponse(SQLModel):
    items: list[KnowledgeBaseQueryTestItem]


class KnowledgeBaseDocumentResponse(SQLModel):
    id: int
    knowledge_base_id: int
    filename: str
    chunk_size: int
    chunk_overlap: int
    batch_size: int
    chunk_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBaseDocumentListResponse(SQLModel):
    items: list[KnowledgeBaseDocumentResponse]
    total: int


class KnowledgeBaseDocumentContentResponse(KnowledgeBaseDocumentResponse):
    content: str
