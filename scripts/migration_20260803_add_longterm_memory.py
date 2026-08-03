from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260803_add_longterm_memory"


metadata = MetaData()


long_term_memory_store = Table(
    "long_term_memory_store",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("active_embedding_channel_id", Integer),
    Column("active_embedding_model_id", String(255)),
    Column("active_embedding_dimensions", Integer),
    Column("active_embedding_signature", String(128)),
    Column("active_embedding_revision", Integer, nullable=False, server_default=text("0")),
    Column("active_collection_name", String(255)),
    Column("target_embedding_channel_id", Integer),
    Column("target_embedding_model_id", String(255)),
    Column("target_embedding_dimensions", Integer),
    Column("target_embedding_signature", String(128)),
    Column("target_collection_name", String(255)),
    Column("migration_job_id", Integer),
    Column("migration_status", String(20)),
    Column("migration_snapshot_boundary", Integer),
    Column("migration_cursor", Integer),
    Column("migration_total_count", Integer, nullable=False, server_default=text("0")),
    Column("migration_success_count", Integer, nullable=False, server_default=text("0")),
    Column("migration_failure_count", Integer, nullable=False, server_default=text("0")),
    Column("migration_delta_high_watermark", Integer, nullable=False, server_default=text("0")),
    Column("migration_delta_applied_watermark", Integer, nullable=False, server_default=text("0")),
    Column("migration_error", Text),
    Column("migration_started_at", DateTime(timezone=True)),
    Column("migration_finished_at", DateTime(timezone=True)),
    Column("old_collection_name", String(255)),
    Column("old_collection_cleanup_status", String(20), nullable=False, server_default=text("'none'")),
    Column("old_collection_cleanup_job_id", Integer),
    Column("old_collection_cleanup_error", Text),
    Column("old_collection_cleanup_at", DateTime(timezone=True)),
    Column("max_active_records", Integer, nullable=False, server_default=text("500")),
    Column("index_revision", Integer, nullable=False, server_default=text("0")),
    Column("index_status", String(20), nullable=False, server_default=text("'pending'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("uid", name="uq_long_term_memory_store_uid"),
)
Index("ix_long_term_memory_store_uid", long_term_memory_store.c.uid)
Index("ix_long_term_memory_store_active_embedding_channel_id", long_term_memory_store.c.active_embedding_channel_id)
Index("ix_long_term_memory_store_active_embedding_model_id", long_term_memory_store.c.active_embedding_model_id)
Index("ix_long_term_memory_store_active_embedding_signature", long_term_memory_store.c.active_embedding_signature)
Index("ix_long_term_memory_store_active_embedding_revision", long_term_memory_store.c.active_embedding_revision)
Index("ix_long_term_memory_store_active_collection_name", long_term_memory_store.c.active_collection_name)
Index("ix_long_term_memory_store_target_embedding_channel_id", long_term_memory_store.c.target_embedding_channel_id)
Index("ix_long_term_memory_store_target_embedding_model_id", long_term_memory_store.c.target_embedding_model_id)
Index("ix_long_term_memory_store_target_embedding_signature", long_term_memory_store.c.target_embedding_signature)
Index("ix_long_term_memory_store_target_collection_name", long_term_memory_store.c.target_collection_name)
Index("ix_long_term_memory_store_migration_job_id", long_term_memory_store.c.migration_job_id)
Index("ix_long_term_memory_store_migration_status", long_term_memory_store.c.migration_status)
Index("ix_long_term_memory_store_old_collection_name", long_term_memory_store.c.old_collection_name)
Index("ix_long_term_memory_store_old_collection_cleanup_status", long_term_memory_store.c.old_collection_cleanup_status)
Index("ix_long_term_memory_store_old_collection_cleanup_job_id", long_term_memory_store.c.old_collection_cleanup_job_id)
Index("ix_long_term_memory_store_index_revision", long_term_memory_store.c.index_revision)
Index("ix_long_term_memory_store_index_status", long_term_memory_store.c.index_status)
Index("ix_long_term_memory_store_created_at", long_term_memory_store.c.created_at)
Index("ix_long_term_memory_store_updated_at", long_term_memory_store.c.updated_at)


long_term_memory_embedding_revision = Table(
    "long_term_memory_embedding_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("revision", Integer, nullable=False, server_default=text("1")),
    Column("from_channel_id", Integer),
    Column("from_model_id", String(255)),
    Column("from_dimensions", Integer),
    Column("from_signature", String(128)),
    Column("from_collection", String(255)),
    Column("to_channel_id", Integer),
    Column("to_model_id", String(255)),
    Column("to_dimensions", Integer),
    Column("to_signature", String(128)),
    Column("to_collection", String(255)),
    Column("confirmation_source_profile_id", Integer),
    Column("confirmation_source", String(50), nullable=False, server_default=text("'profile'")),
    Column("embedding_selection_signature", String(128)),
    Column("confirmed_at", DateTime(timezone=True)),
    Column("job_id", Integer),
    Column("status", String(20), nullable=False, server_default=text("'confirmed'")),
    Column("result", JSON),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("uid", "revision", name="uq_long_term_memory_embedding_revision_uid_revision"),
)
Index("ix_long_term_memory_embedding_revision_uid", long_term_memory_embedding_revision.c.uid)
Index("ix_long_term_memory_embedding_revision_revision", long_term_memory_embedding_revision.c.revision)
Index("ix_long_term_memory_embedding_revision_from_channel_id", long_term_memory_embedding_revision.c.from_channel_id)
Index("ix_long_term_memory_embedding_revision_from_model_id", long_term_memory_embedding_revision.c.from_model_id)
Index("ix_long_term_memory_embedding_revision_from_collection", long_term_memory_embedding_revision.c.from_collection)
Index("ix_long_term_memory_embedding_revision_to_channel_id", long_term_memory_embedding_revision.c.to_channel_id)
Index("ix_long_term_memory_embedding_revision_to_model_id", long_term_memory_embedding_revision.c.to_model_id)
Index("ix_long_term_memory_embedding_revision_to_collection", long_term_memory_embedding_revision.c.to_collection)
Index("ix_ltm_embedding_revision_confirm_profile", long_term_memory_embedding_revision.c.confirmation_source_profile_id)
Index("ix_long_term_memory_embedding_revision_confirmation_source", long_term_memory_embedding_revision.c.confirmation_source)
Index("ix_ltm_embedding_revision_selection_signature", long_term_memory_embedding_revision.c.embedding_selection_signature)
Index("ix_long_term_memory_embedding_revision_confirmed_at", long_term_memory_embedding_revision.c.confirmed_at)
Index("ix_long_term_memory_embedding_revision_job_id", long_term_memory_embedding_revision.c.job_id)
Index("ix_long_term_memory_embedding_revision_status", long_term_memory_embedding_revision.c.status)
Index("ix_long_term_memory_embedding_revision_created_at", long_term_memory_embedding_revision.c.created_at)
Index("ix_long_term_memory_embedding_revision_updated_at", long_term_memory_embedding_revision.c.updated_at)
Index("ix_long_term_memory_embedding_revision_finished_at", long_term_memory_embedding_revision.c.finished_at)


long_term_memory_embedding_delta = Table(
    "long_term_memory_embedding_delta",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("migration_job_id", Integer, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("memory_id", Integer),
    Column("memory_version", Integer),
    Column("action", String(20), nullable=False, server_default=text("'upsert'")),
    Column("source_mutation_job_id", Integer),
    Column("snapshot", JSON, nullable=False),
    Column("status", String(20), nullable=False, server_default=text("'pending'")),
    Column("error", Text),
    Column("applied_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("migration_job_id", "sequence", name="uq_long_term_memory_embedding_delta_job_sequence"),
)
Index("ix_long_term_memory_embedding_delta_uid", long_term_memory_embedding_delta.c.uid)
Index("ix_long_term_memory_embedding_delta_migration_job_id", long_term_memory_embedding_delta.c.migration_job_id)
Index("ix_long_term_memory_embedding_delta_sequence", long_term_memory_embedding_delta.c.sequence)
Index("ix_long_term_memory_embedding_delta_memory_id", long_term_memory_embedding_delta.c.memory_id)
Index("ix_long_term_memory_embedding_delta_action", long_term_memory_embedding_delta.c.action)
Index("ix_long_term_memory_embedding_delta_source_mutation_job_id", long_term_memory_embedding_delta.c.source_mutation_job_id)
Index("ix_long_term_memory_embedding_delta_status", long_term_memory_embedding_delta.c.status)
Index("ix_long_term_memory_embedding_delta_applied_at", long_term_memory_embedding_delta.c.applied_at)
Index("ix_long_term_memory_embedding_delta_created_at", long_term_memory_embedding_delta.c.created_at)
Index("ix_long_term_memory_embedding_delta_uid_job_sequence", long_term_memory_embedding_delta.c.uid, long_term_memory_embedding_delta.c.migration_job_id, long_term_memory_embedding_delta.c.sequence)


long_term_memory_record = Table(
    "long_term_memory_record",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("memory_key", String(255)),
    Column("memory_type", String(50), nullable=False, server_default=text("'fact'")),
    Column("importance", Integer, nullable=False, server_default=text("0")),
    Column("scope", String(100)),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64)),
    Column("version", Integer, nullable=False, server_default=text("0")),
    Column("indexed_version", Integer, nullable=False, server_default=text("0")),
    Column("vector_item_id", String(255)),
    Column("source", String(50), nullable=False, server_default=text("'user_api'")),
    Column("source_id", String(255)),
    Column("source_session_id", String(100)),
    Column("source_profile_id", Integer),
    Column("source_message_id", Integer),
    Column("source_job_id", Integer),
    Column("change_evidence", Text),
    Column("is_active", Boolean, nullable=False, server_default=false()),
    Column("pending_mutation_job_id", Integer),
    Column("suppress_recall", Boolean, nullable=False, server_default=false()),
    Column("suppressed_by_job_id", Integer),
    Column("index_status", String(20), nullable=False, server_default=text("'pending'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("indexed_at", DateTime(timezone=True)),
    Column("deleted_at", DateTime(timezone=True)),
    UniqueConstraint("uid", "memory_key", name="uq_long_term_memory_record_uid_key"),
    UniqueConstraint("uid", "content_hash", name="uq_long_term_memory_record_uid_content_hash"),
    UniqueConstraint("vector_item_id", name="uq_long_term_memory_record_vector_item_id"),
)
Index("ix_long_term_memory_record_uid", long_term_memory_record.c.uid)
Index("ix_long_term_memory_record_memory_key", long_term_memory_record.c.memory_key)
Index("ix_long_term_memory_record_memory_type", long_term_memory_record.c.memory_type)
Index("ix_long_term_memory_record_importance", long_term_memory_record.c.importance)
Index("ix_long_term_memory_record_scope", long_term_memory_record.c.scope)
Index("ix_long_term_memory_record_content_hash", long_term_memory_record.c.content_hash)
Index("ix_long_term_memory_record_version", long_term_memory_record.c.version)
Index("ix_long_term_memory_record_indexed_version", long_term_memory_record.c.indexed_version)
Index("ix_long_term_memory_record_vector_item_id", long_term_memory_record.c.vector_item_id)
Index("ix_long_term_memory_record_source", long_term_memory_record.c.source)
Index("ix_long_term_memory_record_source_id", long_term_memory_record.c.source_id)
Index("ix_long_term_memory_record_source_session_id", long_term_memory_record.c.source_session_id)
Index("ix_long_term_memory_record_source_profile_id", long_term_memory_record.c.source_profile_id)
Index("ix_long_term_memory_record_source_message_id", long_term_memory_record.c.source_message_id)
Index("ix_long_term_memory_record_source_job_id", long_term_memory_record.c.source_job_id)
Index("ix_long_term_memory_record_is_active", long_term_memory_record.c.is_active)
Index("ix_long_term_memory_record_pending_mutation_job_id", long_term_memory_record.c.pending_mutation_job_id)
Index("ix_long_term_memory_record_suppress_recall", long_term_memory_record.c.suppress_recall)
Index("ix_long_term_memory_record_suppressed_by_job_id", long_term_memory_record.c.suppressed_by_job_id)
Index("ix_long_term_memory_record_index_status", long_term_memory_record.c.index_status)
Index("ix_long_term_memory_record_created_at", long_term_memory_record.c.created_at)
Index("ix_long_term_memory_record_updated_at", long_term_memory_record.c.updated_at)
Index("ix_long_term_memory_record_indexed_at", long_term_memory_record.c.indexed_at)
Index("ix_long_term_memory_record_deleted_at", long_term_memory_record.c.deleted_at)
Index("ix_long_term_memory_record_uid_is_active_deleted_at", long_term_memory_record.c.uid, long_term_memory_record.c.is_active, long_term_memory_record.c.deleted_at)
Index("ix_long_term_memory_record_uid_updated_at", long_term_memory_record.c.uid, long_term_memory_record.c.updated_at)


long_term_memory_revision = Table(
    "long_term_memory_revision",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("memory_id", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("memory_key", String(255), nullable=False, server_default=text("''")),
    Column("memory_type", String(50), nullable=False, server_default=text("'fact'")),
    Column("importance", Integer, nullable=False, server_default=text("0")),
    Column("scope", String(100)),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64)),
    Column("source", String(50), nullable=False, server_default=text("'user_api'")),
    Column("source_id", String(255)),
    Column("source_session_id", String(100)),
    Column("source_profile_id", Integer),
    Column("source_message_id", Integer),
    Column("source_job_id", Integer),
    Column("change_evidence", Text),
    Column("published_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("uid", "memory_id", "version", name="uq_long_term_memory_revision_uid_memory_version"),
)
Index("ix_long_term_memory_revision_uid", long_term_memory_revision.c.uid)
Index("ix_long_term_memory_revision_memory_id", long_term_memory_revision.c.memory_id)
Index("ix_long_term_memory_revision_version", long_term_memory_revision.c.version)
Index("ix_long_term_memory_revision_memory_key", long_term_memory_revision.c.memory_key)
Index("ix_long_term_memory_revision_memory_type", long_term_memory_revision.c.memory_type)
Index("ix_long_term_memory_revision_scope", long_term_memory_revision.c.scope)
Index("ix_long_term_memory_revision_content_hash", long_term_memory_revision.c.content_hash)
Index("ix_long_term_memory_revision_source", long_term_memory_revision.c.source)
Index("ix_long_term_memory_revision_source_id", long_term_memory_revision.c.source_id)
Index("ix_long_term_memory_revision_source_session_id", long_term_memory_revision.c.source_session_id)
Index("ix_long_term_memory_revision_source_profile_id", long_term_memory_revision.c.source_profile_id)
Index("ix_long_term_memory_revision_source_message_id", long_term_memory_revision.c.source_message_id)
Index("ix_long_term_memory_revision_source_job_id", long_term_memory_revision.c.source_job_id)
Index("ix_long_term_memory_revision_published_at", long_term_memory_revision.c.published_at)
Index("ix_long_term_memory_revision_created_at", long_term_memory_revision.c.created_at)
Index("ix_long_term_memory_revision_uid_memory_version", long_term_memory_revision.c.uid, long_term_memory_revision.c.memory_id, long_term_memory_revision.c.version)


long_term_memory_mutation_job = Table(
    "long_term_memory_mutation_job",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("uid", String(100), nullable=False),
    Column("operation", String(30), nullable=False),
    Column("dedupe_key", String(255), nullable=False),
    Column("active_mutation_key", String(255)),
    Column("status", String(20), nullable=False, server_default=text("'pending'")),
    Column("memory_id", Integer),
    Column("expected_version", Integer),
    Column("payload", JSON, nullable=False),
    Column("result", JSON),
    Column("error", Text),
    Column("source_session_id", String(100)),
    Column("source_profile_id", Integer),
    Column("source_message_id", Integer),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("3")),
    Column("locked_by", String(100)),
    Column("lock_until", DateTime(timezone=True)),
    Column("cancel_requested_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("uid", "dedupe_key", name="uq_long_term_memory_mutation_job_uid_dedupe"),
    UniqueConstraint("active_mutation_key", name="uq_long_term_memory_mutation_job_active_key"),
)
Index("ix_long_term_memory_mutation_job_uid", long_term_memory_mutation_job.c.uid)
Index("ix_long_term_memory_mutation_job_operation", long_term_memory_mutation_job.c.operation)
Index("ix_long_term_memory_mutation_job_dedupe_key", long_term_memory_mutation_job.c.dedupe_key)
Index("ix_long_term_memory_mutation_job_active_mutation_key", long_term_memory_mutation_job.c.active_mutation_key)
Index("ix_long_term_memory_mutation_job_status", long_term_memory_mutation_job.c.status)
Index("ix_long_term_memory_mutation_job_memory_id", long_term_memory_mutation_job.c.memory_id)
Index("ix_long_term_memory_mutation_job_source_session_id", long_term_memory_mutation_job.c.source_session_id)
Index("ix_long_term_memory_mutation_job_source_profile_id", long_term_memory_mutation_job.c.source_profile_id)
Index("ix_long_term_memory_mutation_job_source_message_id", long_term_memory_mutation_job.c.source_message_id)
Index("ix_long_term_memory_mutation_job_available_at", long_term_memory_mutation_job.c.available_at)
Index("ix_long_term_memory_mutation_job_attempt_count", long_term_memory_mutation_job.c.attempt_count)
Index("ix_long_term_memory_mutation_job_locked_by", long_term_memory_mutation_job.c.locked_by)
Index("ix_long_term_memory_mutation_job_lock_until", long_term_memory_mutation_job.c.lock_until)
Index("ix_long_term_memory_mutation_job_cancel_requested_at", long_term_memory_mutation_job.c.cancel_requested_at)
Index("ix_long_term_memory_mutation_job_created_at", long_term_memory_mutation_job.c.created_at)
Index("ix_long_term_memory_mutation_job_updated_at", long_term_memory_mutation_job.c.updated_at)
Index("ix_long_term_memory_mutation_job_started_at", long_term_memory_mutation_job.c.started_at)
Index("ix_long_term_memory_mutation_job_finished_at", long_term_memory_mutation_job.c.finished_at)
Index("ix_long_term_memory_mutation_job_uid_status_available", long_term_memory_mutation_job.c.uid, long_term_memory_mutation_job.c.status, long_term_memory_mutation_job.c.available_at)


for table in (
    long_term_memory_store,
    long_term_memory_embedding_revision,
    long_term_memory_embedding_delta,
    long_term_memory_record,
    long_term_memory_revision,
    long_term_memory_mutation_job,
):
    Index(f"ix_{table.name}_id", table.c.id)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: metadata.create_all(sync_connection, checkfirst=True))
