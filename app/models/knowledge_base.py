from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, model_validator
from sqlalchemy import DDL, CheckConstraint, ForeignKeyConstraint, Text, event
from sqlmodel import (
    JSON,
    Column,
    DateTime,
    Field,
    Index,
    SQLModel,
    UniqueConstraint,
)

from app.core.utils.time import get_local_time


class KnowledgeBaseType(StrEnum):
    USER = "user"
    LLM_MANAGED = "llm_managed"


class KnowledgeBaseMigrationStatus(StrEnum):
    PREPARING = "preparing"
    BUILDING = "building"
    CATCHING_UP = "catching_up"
    VALIDATING = "validating"
    SWITCHING = "switching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeBaseOldCollectionCleanupStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class KnowledgeBaseIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    REINDEXING = "reindexing"
    FAILED = "failed"


class KnowledgeBaseCore(SQLModel):
    uid: str = Field(index=True, nullable=False, max_length=50, description="知识库所属用户ID")
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, max_length=500, description="知识库描述")
    embedding_channel_id: int = Field(
        nullable=False,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
        description="向量化使用的渠道ID",
    )
    embedding_model_id: str = Field(nullable=False, max_length=255, description="向量化使用的模型ID")
    embedding_dimensions: int | None = Field(default=None, gt=0, description="向量输出维度")
    collection_name: str = Field(unique=True, index=True, nullable=False, max_length=100, description="ChromaDB 中的 collection 名称")
    knowledge_base_type: KnowledgeBaseType = Field(
        default=KnowledgeBaseType.USER,
        nullable=False,
        index=True,
        max_length=20,
        description="知识库类型",
    )
    managed_profile_id: int | None = Field(
        default=None,
        description="LLM 托管知识库所属 Profile",
    )

    active_embedding_channel_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
        description="当前生效的向量化渠道ID",
    )
    active_embedding_model_id: str | None = Field(default=None, index=True, max_length=255, description="当前生效的向量化模型ID")
    active_embedding_dimensions: int | None = Field(default=None, ge=1, description="当前生效的向量输出维度")
    active_embedding_signature: str | None = Field(default=None, index=True, max_length=128, description="当前生效的向量化配置签名")
    active_embedding_revision: int = Field(default=0, ge=0, index=True, description="当前生效的向量化配置版本")
    active_collection_name: str | None = Field(default=None, index=True, unique=True, max_length=255, description="当前生效的 collection 名称")

    target_embedding_channel_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
        description="目标向量化渠道ID",
    )
    target_embedding_model_id: str | None = Field(default=None, index=True, max_length=255, description="目标向量化模型ID")
    target_embedding_dimensions: int | None = Field(default=None, ge=1, description="目标向量输出维度")
    target_embedding_signature: str | None = Field(default=None, index=True, max_length=128, description="目标向量化配置签名")
    target_embedding_revision: int | None = Field(default=None, ge=1, index=True, description="目标向量化配置版本")
    target_collection_name: str | None = Field(default=None, index=True, unique=True, max_length=255, description="目标 collection 名称")

    migration_job_id: int | None = Field(default=None, index=True, description="迁移任务ID")
    migration_status: KnowledgeBaseMigrationStatus | None = Field(default=None, index=True, max_length=20, description="迁移状态")
    migration_snapshot_boundary: int | None = Field(default=None, ge=0, description="迁移快照边界")
    migration_cursor: int | None = Field(default=None, ge=0, description="迁移游标")
    migration_total_count: int = Field(default=0, ge=0, description="迁移总数量")
    migration_success_count: int = Field(default=0, ge=0, description="迁移成功数量")
    migration_failure_count: int = Field(default=0, ge=0, description="迁移失败数量")
    migration_delta_high_watermark: int = Field(default=0, ge=0, description="迁移增量高水位")
    migration_delta_applied_watermark: int = Field(default=0, ge=0, description="迁移增量已应用水位")
    migration_error: str | None = Field(default=None, sa_column=Column(Text), description="迁移错误信息")
    migration_started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="迁移开始时间")
    migration_finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="迁移完成时间")

    old_collection_name: str | None = Field(default=None, index=True, unique=True, max_length=255, description="旧 collection 名称")
    old_collection_cleanup_status: KnowledgeBaseOldCollectionCleanupStatus = Field(
        default=KnowledgeBaseOldCollectionCleanupStatus.NONE,
        index=True,
        max_length=20,
        description="旧 collection 清理状态",
    )
    old_collection_cleanup_job_id: int | None = Field(default=None, index=True, description="旧 collection 清理任务ID")
    old_collection_cleanup_error: str | None = Field(default=None, sa_column=Column(Text), description="旧 collection 清理错误信息")
    old_collection_cleanup_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="旧 collection 清理时间")

    index_revision: int = Field(default=0, ge=0, index=True, description="索引版本")
    index_status: KnowledgeBaseIndexStatus = Field(
        default=KnowledgeBaseIndexStatus.PENDING,
        index=True,
        max_length=20,
        description="索引状态",
    )


class KnowledgeBase(KnowledgeBaseCore, table=True):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        UniqueConstraint("managed_profile_id", name="uq_knowledge_base_managed_profile"),
        UniqueConstraint("id", "uid", name="uq_knowledge_base_id_uid"),
        ForeignKeyConstraint(
            ["managed_profile_id", "uid"],
            ["profile.id", "profile.uid"],
            name="fk_kb_managed_profile",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(knowledge_base_type = 'USER' AND managed_profile_id IS NULL) OR (knowledge_base_type = 'LLM_MANAGED' AND managed_profile_id IS NOT NULL)",
            name="ck_knowledge_base_type_profile_owner",
        ),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
    updated_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), onupdate=get_local_time),
    )


class KnowledgeBaseCollectionOwner(SQLModel, table=True):
    __tablename__ = "knowledge_base_collection_owner"

    collection_name: str = Field(primary_key=True, max_length=255)
    knowledge_base_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="knowledge_base.id",
        ondelete="SET NULL",
    )
    cleanup_attempt_count: int = Field(default=0, ge=0)
    cleanup_error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime | None = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), onupdate=get_local_time),
    )


_COLLECTION_OWNER_FIELDS = (
    "collection_name",
    "active_collection_name",
    "target_collection_name",
    "old_collection_name",
)
_COLLECTION_OWNER_TRIGGER_NAMES = (
    "trg_knowledge_base_collection_owner_before_insert",
    "trg_knowledge_base_collection_owner_after_insert",
    "trg_knowledge_base_collection_owner_before_update",
    "trg_knowledge_base_collection_owner_after_update",
)
_COLLECTION_OWNER_TRIGGER_ERROR = "knowledge_base.collection_owner"


def _collection_owner_quote(connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _collection_owner_qualified(connection, alias: str, column: str) -> str:
    return f"{_collection_owner_quote(connection, alias)}.{_collection_owner_quote(connection, column)}"


def _collection_owner_new_expression(connection, prefix: str, column: str) -> str:
    return f"{prefix}.{_collection_owner_quote(connection, column)}"


def _collection_owner_nonempty(expression: str) -> str:
    return f"{expression} IS NOT NULL AND TRIM({expression}) <> ''"


def _collection_owner_columns_sql(connection) -> str:
    return ", ".join(
        _collection_owner_quote(connection, column)
        for column in (
            "collection_name",
            "knowledge_base_id",
            "cleanup_attempt_count",
            "cleanup_error",
            "created_at",
            "updated_at",
        )
    )


def _collection_owner_conflict_condition(connection, *, updating: bool) -> str:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    owner_alias = _collection_owner_quote(connection, "owner_row")
    checks = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, "NEW", field)
        owner_condition = f"{_collection_owner_qualified(connection, 'owner_row', 'collection_name')} = {expression}"
        if updating:
            owner_condition += f" AND ({_collection_owner_qualified(connection, 'owner_row', 'knowledge_base_id')} IS NULL OR {_collection_owner_qualified(connection, 'owner_row', 'knowledge_base_id')} <> OLD.{_collection_owner_quote(connection, 'id')})"
        checks.append(f"({_collection_owner_nonempty(expression)} AND EXISTS (SELECT 1 FROM {owner} AS {owner_alias} WHERE {owner_condition}))")
    return " OR ".join(checks)


def _collection_owner_cleanup_statement(connection) -> str:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    keep_conditions = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, "NEW", field)
        keep_conditions.append(f"({_collection_owner_nonempty(expression)} AND {_collection_owner_quote(connection, 'collection_name')} = {expression})")
    return (
        f"UPDATE {owner} SET "
        f"{_collection_owner_quote(connection, 'knowledge_base_id')} = NULL, "
        f"{_collection_owner_quote(connection, 'cleanup_attempt_count')} = 0, "
        f"{_collection_owner_quote(connection, 'cleanup_error')} = NULL, "
        f"{_collection_owner_quote(connection, 'updated_at')} = CURRENT_TIMESTAMP "
        f"WHERE {_collection_owner_quote(connection, 'knowledge_base_id')} = NEW.{_collection_owner_quote(connection, 'id')} "
        f"AND NOT ({' OR '.join(keep_conditions)})"
    )


def _sqlite_collection_owner_registration_statements(connection, prefix: str) -> list[str]:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    columns = _collection_owner_columns_sql(connection)
    statements = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, prefix, field)
        statements.append(f"INSERT OR IGNORE INTO {owner} ({columns}) SELECT {expression}, {_collection_owner_new_expression(connection, prefix, 'id')}, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP WHERE {_collection_owner_nonempty(expression)}")
    return statements


def _mysql_collection_owner_registration_statements(connection, prefix: str) -> list[str]:
    owner = _collection_owner_quote(connection, KnowledgeBaseCollectionOwner.__table__.name)
    owner_alias = _collection_owner_quote(connection, "owner_row")
    owner_collection = _collection_owner_qualified(connection, "owner_row", "collection_name")
    owner_knowledge_base = _collection_owner_qualified(connection, "owner_row", "knowledge_base_id")
    columns = _collection_owner_columns_sql(connection)
    statements = []
    for field in _COLLECTION_OWNER_FIELDS:
        expression = _collection_owner_new_expression(connection, prefix, field)
        knowledge_base_id = _collection_owner_new_expression(connection, prefix, "id")
        statements.append(
            f"IF {_collection_owner_nonempty(expression)} THEN "
            f"INSERT IGNORE INTO {owner} ({columns}) VALUES ({expression}, {knowledge_base_id}, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP); "
            f"IF NOT EXISTS (SELECT 1 FROM {owner} AS {owner_alias} WHERE {owner_collection} = {expression} AND {owner_knowledge_base} = {knowledge_base_id}) THEN "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_COLLECTION_OWNER_TRIGGER_ERROR}'; "
            f"END IF; "
            f"END IF;"
        )
    return statements


def _collection_owner_trigger_statements(connection) -> list[str]:
    knowledge_base = _collection_owner_quote(connection, KnowledgeBase.__table__.name)
    before_insert_condition = _collection_owner_conflict_condition(connection, updating=False)
    before_update_condition = _collection_owner_conflict_condition(connection, updating=True)
    cleanup = _collection_owner_cleanup_statement(connection)

    if connection.dialect.name == "sqlite":
        registration = ";\n".join(_sqlite_collection_owner_registration_statements(connection, "NEW"))
        return [
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[0])} BEFORE INSERT ON {knowledge_base} FOR EACH ROW WHEN ({before_insert_condition}) BEGIN SELECT RAISE(ABORT, '{_COLLECTION_OWNER_TRIGGER_ERROR}'); END",
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[1])} AFTER INSERT ON {knowledge_base} FOR EACH ROW BEGIN {registration}; END",
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[2])} BEFORE UPDATE ON {knowledge_base} FOR EACH ROW WHEN ({before_update_condition}) BEGIN SELECT RAISE(ABORT, '{_COLLECTION_OWNER_TRIGGER_ERROR}'); END",
            f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[3])} AFTER UPDATE ON {knowledge_base} FOR EACH ROW BEGIN {registration}; {cleanup}; END",
        ]

    registration = " ".join(_mysql_collection_owner_registration_statements(connection, "NEW"))
    return [
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[0])} BEFORE INSERT ON {knowledge_base} FOR EACH ROW BEGIN IF ({before_insert_condition}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_COLLECTION_OWNER_TRIGGER_ERROR}'; END IF; END",
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[1])} AFTER INSERT ON {knowledge_base} FOR EACH ROW BEGIN {registration} END",
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[2])} BEFORE UPDATE ON {knowledge_base} FOR EACH ROW BEGIN IF ({before_update_condition}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_COLLECTION_OWNER_TRIGGER_ERROR}'; END IF; END",
        f"CREATE TRIGGER {_collection_owner_quote(connection, _COLLECTION_OWNER_TRIGGER_NAMES[3])} AFTER UPDATE ON {knowledge_base} FOR EACH ROW BEGIN {registration} {cleanup}; END",
    ]


@event.listens_for(KnowledgeBaseCollectionOwner.__table__, "after_create")
def _install_collection_owner_triggers(target, connection, **kwargs) -> None:
    install_knowledge_base_collection_owner_triggers(connection)


def install_knowledge_base_collection_owner_triggers(connection) -> None:
    if connection.dialect.name not in {"sqlite", "mysql"}:
        return

    for trigger_name in _COLLECTION_OWNER_TRIGGER_NAMES:
        connection.execute(DDL(f"DROP TRIGGER IF EXISTS {_collection_owner_quote(connection, trigger_name)}"))
    for statement in _collection_owner_trigger_statements(connection):
        connection.execute(DDL(statement))


class KnowledgeBaseProfileBinding(SQLModel, table=True):
    """知识库与 Profile 的绑定关系。"""

    __tablename__ = "knowledge_base_profile_binding"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "profile_id", name="uq_knowledge_base_profile_binding_pair"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_kb_profile_binding_kb_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["profile_id", "uid"],
            ["profile.id", "profile.uid"],
            name="fk_kb_profile_binding_profile_owner",
            ondelete="CASCADE",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    knowledge_base_id: int = Field(nullable=False, index=True, description="知识库ID")
    profile_id: int = Field(nullable=False, index=True, description="配置文件ID")
    uid: str = Field(nullable=False, index=True, max_length=50, description="所属用户ID")


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


class ManagedKnowledgeSourceType(StrEnum):
    LLM_TOOL = "llm_tool"
    USER_API = "user_api"
    AUTO_ORGANIZE = "auto_organize"
    SYSTEM = "system"


class ManagedKnowledgeActorType(StrEnum):
    LLM = "llm"
    USER = "user"
    SYSTEM = "system"


class ManagedKnowledgeRevisionOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class KnowledgeJobOperation(StrEnum):
    MANAGED_CREATE = "managed_create"
    MANAGED_UPDATE = "managed_update"
    MANAGED_DELETE_CLEANUP = "managed_delete_cleanup"
    MANAGED_VECTOR_CLEANUP = "managed_vector_cleanup"
    USER_DOCUMENT_INDEX = "user_document_index"
    USER_DOCUMENT_DELETE_CLEANUP = "user_document_delete_cleanup"
    EMBEDDING_MIGRATION = "embedding_migration"
    REINDEX = "reindex"
    OLD_COLLECTION_CLEANUP = "old_collection_cleanup"
    AUTO_ORGANIZE = "auto_organize"
    MANUAL_ORGANIZE = "manual_organize"
    ORGANIZE_MUTATION = "organize_mutation"


class KnowledgeJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ManagedKnowledgeItem(SQLModel, table=True):
    __tablename__ = "managed_knowledge_item"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "knowledge_key", name="uq_managed_knowledge_item_kb_key"),
        UniqueConstraint("knowledge_base_id", "content_hash", name="uq_managed_knowledge_item_kb_content_hash"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_managed_knowledge_item_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_managed_knowledge_item_kb_recallable", "knowledge_base_id", "is_recallable", "deleted_at"),
        Index("ix_managed_knowledge_item_kb_updated", "knowledge_base_id", "updated_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True, index=True, description="稳定的托管知识标识")
    knowledge_base_id: int = Field(nullable=False, index=True, description="所属托管知识库")
    uid: str = Field(nullable=False, index=True, max_length=50, description="所属用户")
    knowledge_key: str = Field(nullable=False, index=True, max_length=255, description="稳定知识键")
    content: str = Field(sa_column=Column(Text, nullable=False), description="完整知识正文，不保存截断内容")
    content_token_count: int = Field(default=0, ge=0, nullable=False, description="完整正文 Token 数")
    content_hash: str = Field(nullable=False, index=True, max_length=64, description="完整正文的稳定 SHA-256 摘要")
    version: int = Field(default=1, ge=1, index=True, nullable=False, description="当前知识版本")
    source_type: ManagedKnowledgeSourceType = Field(default=ManagedKnowledgeSourceType.USER_API, index=True, max_length=30)
    source_reference: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON), description="当前版本来源引用")
    source_job_id: int | None = Field(default=None, index=True, description="产生当前版本的知识作业；步骤 4 接入")
    created_by: ManagedKnowledgeActorType = Field(default=ManagedKnowledgeActorType.USER, index=True, max_length=20)
    last_modified_by: ManagedKnowledgeActorType = Field(default=ManagedKnowledgeActorType.USER, index=True, max_length=20)
    llm_maintainable: bool = Field(default=False, index=True, nullable=False, description="是否允许 LLM 后续维护")
    indexed_version: int = Field(default=0, ge=0, index=True, nullable=False, description="已写入当前向量索引的知识版本")
    vector_item_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False), description="当前关联的向量分块标识")
    is_recallable: bool = Field(default=False, index=True, nullable=False, description="当前版本是否允许召回")
    pending_job_id: int | None = Field(default=None, index=True, description="待处理知识作业；步骤 4 接入")
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    last_recalled_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class ManagedKnowledgeRevision(SQLModel, table=True):
    __tablename__ = "managed_knowledge_revision"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "knowledge_id", "version", name="uq_managed_knowledge_revision_kb_knowledge_version"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_managed_knowledge_revision_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_managed_knowledge_revision_history", "knowledge_base_id", "knowledge_id", "version"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    knowledge_base_id: int = Field(nullable=False, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_id: int = Field(nullable=False, index=True, description="稳定知识标识；故意不外键到条目，以便未来清理主记录后仍保留审计快照")
    version: int = Field(ge=1, index=True, nullable=False)
    operation: ManagedKnowledgeRevisionOperation = Field(index=True, max_length=20)
    before_snapshot: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    after_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    source_type: ManagedKnowledgeSourceType = Field(index=True, max_length=30)
    source_reference: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    source_job_id: int | None = Field(default=None, index=True)
    modified_by: ManagedKnowledgeActorType = Field(index=True, max_length=20)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


class KnowledgeJob(SQLModel, table=True):
    __tablename__ = "knowledge_job"
    __table_args__ = (
        UniqueConstraint("uid", "dedupe_key", name="uq_knowledge_job_uid_dedupe"),
        UniqueConstraint("uid", "active_change_key", name="uq_knowledge_job_uid_active_change"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_knowledge_job_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_job_uid_status_available", "uid", "status", "available_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    parent_job_id: int | None = Field(default=None, index=True)
    operation: KnowledgeJobOperation = Field(index=True, max_length=40)
    dedupe_key: str = Field(index=True, max_length=255)
    request_hash: str = Field(index=True, max_length=64)
    active_change_key: str | None = Field(default=None, index=True, max_length=255)
    status: KnowledgeJobStatus = Field(default=KnowledgeJobStatus.PENDING, index=True, max_length=20)
    knowledge_base_id: int = Field(nullable=False, index=True)
    knowledge_id: int | None = Field(default=None, index=True)
    expected_version: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    source_session_id: str | None = Field(default=None, index=True, max_length=100)
    source_profile_id: int | None = Field(default=None, index=True)
    source_message_id: int | None = Field(default=None, index=True)
    available_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    attempt_count: int = Field(default=0, ge=0, index=True)
    max_attempts: int = Field(default=3, ge=1)
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    cancel_requested_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class KnowledgeBaseCreate(SQLModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="知识库描述")
    embedding_channel_id: int = Field(..., gt=0, description="向量化使用的渠道ID")
    embedding_model_id: str = Field(..., min_length=1, max_length=255, description="向量化使用的模型ID")


class KnowledgeBaseResponse(KnowledgeBaseCore):
    id: int
    profile_ids: list[int] = Field(default_factory=list)
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


class KnowledgeBaseListResponse(SQLModel):
    items: list[KnowledgeBaseResponse]
    total: int
    embedding_models: list[dict[str, Any]]


class KnowledgeBaseUpdate(SQLModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="知识库描述")


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
