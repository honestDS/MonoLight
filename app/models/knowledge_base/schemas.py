from datetime import datetime
from typing import Annotated, Any

from pydantic import ConfigDict, StrictStr, StringConstraints, model_validator
from sqlmodel import Field, SQLModel

from .base import KnowledgeBaseCore, KnowledgeBaseIndexStatus
from .job import KnowledgeJobStatus
from .managed import (
    ManagedKnowledgeActorType,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)


class KnowledgeBaseCreate(SQLModel):
    uid: StrictStr | None = Field(default=None, min_length=1, max_length=50, description="超管可指定的知识库所属用户ID；普通用户由登录身份确定")
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="知识库描述")
    embedding_channel_id: int = Field(..., gt=0, description="向量化使用的渠道ID")
    embedding_model_id: str = Field(..., min_length=1, max_length=255, description="向量化使用的模型ID")


class KnowledgeBaseResponse(KnowledgeBaseCore):
    id: int
    username: str | None = None
    profile_ids: list[int] = Field(default_factory=list)
    profile_names: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def migrate_legacy_fields(self) -> "KnowledgeBaseResponse":
        if not self.active_collection_name and self.collection_name:
            self.active_embedding_channel_id = self.embedding_channel_id
            self.active_embedding_model_id = self.embedding_model_id
            self.active_embedding_dimensions = self.embedding_dimensions
            self.active_collection_name = self.collection_name
            self.active_embedding_revision = max(self.active_embedding_revision, 1)
            self.index_revision = max(self.index_revision, 1)
            if self.index_status == KnowledgeBaseIndexStatus.PENDING:
                self.index_status = KnowledgeBaseIndexStatus.READY
        return self


class KnowledgeBaseRepairRequest(SQLModel):
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=255)

    model_config = ConfigDict(extra="forbid")


class KnowledgeBaseRepairResponse(SQLModel):
    items: list[KnowledgeBaseResponse]
    total: int


class KnowledgeBaseListResponse(SQLModel):
    items: list[KnowledgeBaseResponse]
    total: int
    embedding_models: list[dict[str, Any]]
    repair_required_count: int = Field(default=0, ge=0)


class KnowledgeBaseUpdate(SQLModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="知识库描述")


class KnowledgeBaseEmbeddingMigrationRequest(SQLModel):
    embedding_channel_id: int = Field(..., gt=0, description="目标向量化渠道ID")
    embedding_model_id: str = Field(..., min_length=1, max_length=255, description="目标向量化模型ID")


class KnowledgeOrganizationRequest(SQLModel):
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=255)

    model_config = ConfigDict(extra="forbid")


class ManagedKnowledgeCreateRequest(SQLModel):
    knowledge_key: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    llm_maintainable: bool = False
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=255)


class ManagedKnowledgeUpdateRequest(ManagedKnowledgeCreateRequest):
    expected_version: int = Field(..., ge=1)


class ManagedKnowledgeDeleteRequest(SQLModel):
    expected_version: int = Field(..., ge=1)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=255)


class ManagedKnowledgeRetryRequest(SQLModel):
    expected_version: int = Field(..., ge=1)
    failed_job_id: int = Field(..., ge=1)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=255)


class ManagedKnowledgeItemSummaryResponse(SQLModel):
    id: int
    knowledge_base_id: int
    knowledge_key: str
    content_preview: str
    content_token_count: int
    version: int
    source_type: ManagedKnowledgeSourceType
    source_reference: dict[str, Any] | None = None
    created_by: ManagedKnowledgeActorType
    last_modified_by: ManagedKnowledgeActorType
    llm_maintainable: bool
    indexed_version: int
    is_recallable: bool
    pending_job_id: int | None = None
    publication_job_id: int | None = None
    publication_job_status: KnowledgeJobStatus | None = None
    publication_job_error: str | None = None
    created_at: datetime
    updated_at: datetime
    last_recalled_at: datetime | None = None


class ManagedKnowledgeItemResponse(ManagedKnowledgeItemSummaryResponse):
    content: str


class ManagedKnowledgeListResponse(SQLModel):
    items: list[ManagedKnowledgeItemSummaryResponse]
    total: int


class ManagedKnowledgeMutationResponse(SQLModel):
    status: str
    item: ManagedKnowledgeItemResponse | None = None
    job_id: int | None = None


class ManagedKnowledgeRevisionResponse(SQLModel):
    id: int
    knowledge_base_id: int
    knowledge_id: int
    version: int
    operation: ManagedKnowledgeRevisionOperation
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any]
    source_type: ManagedKnowledgeSourceType
    source_reference: dict[str, Any] | None = None
    source_job_id: int | None = None
    modified_by: ManagedKnowledgeActorType
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBaseProfileBindingUpdate(SQLModel):
    knowledge_base_ids: list[int] = Field(default_factory=list, description="绑定的知识库ID列表")


class KnowledgeBaseQueryTestRequest(SQLModel):
    query: str = Field(..., min_length=1, max_length=5000, description="检索词")
    top_k: int = Field(5, ge=1, le=50, description="返回最相似的结果数量")


class KnowledgeBaseQueryTestItem(SQLModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    content: str
    distance: float | None = None
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")


class KnowledgeBaseQueryTestResponse(SQLModel):
    items: list[KnowledgeBaseQueryTestItem]
    retrieval_mode: str | None = None  # hybrid / hybrid_rerank
    rerank_error: str | None = None  # 仅 query-test 路径在 rerank 降级时回填


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


class KnowledgeEntryCreateRequest(SQLModel):
    name: Annotated[StrictStr, StringConstraints(pattern=r"\S")] = Field(..., min_length=1, max_length=255)
    content: StrictStr = Field(..., min_length=1)
    source_filename: Annotated[StrictStr, StringConstraints(pattern=r"\S")] | None = Field(default=None, min_length=1, max_length=255)
    llm_maintainable: bool = False
    chunk_size: int = Field(default=1000, ge=100, le=20000)
    chunk_overlap: int = Field(default=100, ge=0, le=5000)
    batch_size: int = Field(default=16, ge=1, le=256)
    dedupe_key: StrictStr | None = Field(default=None, min_length=1, max_length=255)

    model_config = ConfigDict(extra="forbid")


class KnowledgeEntryFilePreviewResponse(SQLModel):
    filename: str
    content: str


class KnowledgeEntryMutationResponse(SQLModel):
    status: str
    item_id: int | None = None
    job_id: int | None = None
