import uuid

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.constants import (
    ERR_KB_CHUNK_OVERLAP_ERROR,
    ERR_KB_DOC_SAVE_FAILED,
    ERR_KB_EMBEDDING_CONFIG_CHANGED,
    ERR_KB_FILE_EMPTY,
    ERR_KB_FILE_ENCODING_ERROR,
    ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN,
    ERR_KB_NOT_FOUND,
    ERR_KB_VECTOR_WRITE_FAILED,
)
from app.core.crud.knowledge.base import knowledge_base_crud, knowledge_base_document_crud
from app.core.embedding.knowledge_base import embed_chunks_with_knowledge_base_config
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import ParameterException, ResourceNotFoundException, ServerException
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseType,
)
from app.providers.vector import (
    async_delete_collection_items,
    async_get_or_create_collection,
    async_upsert_collection_items,
)


def decode_knowledge_document(raw_content: bytes) -> str:
    try:
        content = raw_content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = raw_content.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ParameterException(ERR_KB_FILE_ENCODING_ERROR) from exc

    if not content.strip():
        raise ParameterException(ERR_KB_FILE_EMPTY)
    return content


async def embed_document_with_stable_knowledge_base_config(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    chunks: list[str],
    batch_size: int,
) -> tuple[KnowledgeBase, list[list[float]]]:
    knowledge_base_uid = knowledge_base.uid
    knowledge_base_id = knowledge_base.id
    if knowledge_base_id is None:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)

    candidate = knowledge_base
    for _attempt in range(2):
        if candidate.knowledge_base_type != KnowledgeBaseType.USER:
            raise ParameterException(ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN, code=409)
        expected_embedding = resolve_active_knowledge_base_embedding(candidate)
        embeddings = await embed_chunks_with_knowledge_base_config(
            db,
            candidate,
            chunks,
            batch_size,
            release_connection=True,
        )
        locked = await knowledge_base_crud.lock_owned_by_id(
            db,
            uid=knowledge_base_uid,
            knowledge_base_id=knowledge_base_id,
        )
        if locked is None:
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
        if locked.knowledge_base_type != KnowledgeBaseType.USER:
            raise ParameterException(ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN, code=409)
        if resolve_active_knowledge_base_embedding(locked) == expected_embedding:
            return locked, embeddings

        await db.rollback()
        candidate = await knowledge_base_crud.get(db, knowledge_base_id)
        if candidate is None or candidate.uid != knowledge_base_uid:
            raise ResourceNotFoundException(ERR_KB_NOT_FOUND)

    raise ParameterException(ERR_KB_EMBEDDING_CONFIG_CHANGED, code=409)


async def create_user_knowledge_document(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    filename: str,
    content: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 100,
    batch_size: int = 16,
    metadata: dict | None = None,
) -> KnowledgeBaseDocument:
    knowledge_base_id = knowledge_base.id
    if knowledge_base_id is None:
        raise ResourceNotFoundException(ERR_KB_NOT_FOUND)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.USER:
        raise ParameterException(ERR_KB_MANAGED_DOCUMENT_IMPORT_FORBIDDEN, code=409)
    if chunk_overlap >= chunk_size:
        raise ParameterException(ERR_KB_CHUNK_OVERLAP_ERROR)

    chunks = TextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap).split(content)
    if not chunks:
        raise ParameterException(ERR_KB_FILE_EMPTY)

    locked_kb, embeddings = await embed_document_with_stable_knowledge_base_config(
        db,
        knowledge_base=knowledge_base,
        chunks=chunks,
        batch_size=batch_size,
    )
    active_embedding = resolve_active_knowledge_base_embedding(locked_kb)
    document_uuid = uuid.uuid4().hex
    chunk_ids = [f"kb_{knowledge_base_id}_doc_{document_uuid}_chunk_{index}" for index in range(len(chunks))]
    metadatas = [
        {
            "knowledge_base_id": knowledge_base_id,
            "document_uuid": document_uuid,
            "filename": filename,
            "chunk_index": chunk_index,
        }
        for chunk_index in range(len(chunks))
    ]

    try:
        await async_get_or_create_collection(active_embedding.collection_name)
        await async_upsert_collection_items(
            active_embedding.collection_name,
            chunk_ids,
            chunks,
            embeddings,
            metadatas,
            batch_size=batch_size,
        )
    except Exception as exc:
        await db.rollback()
        raise ServerException(ERR_KB_VECTOR_WRITE_FAILED, params={"message": str(exc)}) from exc

    document_metadata = dict(metadata or {})
    document_metadata["document_uuid"] = document_uuid
    try:
        db_document = await knowledge_base_document_crud.create(
            db,
            values={
                "knowledge_base_id": knowledge_base_id,
                "filename": filename,
                "content": content,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "batch_size": batch_size,
                "chunk_count": len(chunks),
                "chunk_ids": chunk_ids,
                "metadata_": document_metadata,
            },
            commit=False,
        )
        await record_knowledge_base_migration_change(
            db,
            knowledge_base=locked_kb,
            source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
            source_id=db_document.id,
            action=KnowledgeBaseMigrationDeltaAction.UPSERT,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        try:
            await async_delete_collection_items(active_embedding.collection_name, chunk_ids)
        except Exception:
            pass
        raise ServerException(ERR_KB_DOC_SAVE_FAILED, params={"message": str(exc)}) from exc

    return db_document
