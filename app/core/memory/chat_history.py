import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_FIELD_REQUIRED,
    ERR_VALUE_MUST_BE_BETWEEN,
    ERR_VALUE_MUST_BE_POSITIVE,
    MEMORY_CHAT_HISTORY_RECALL_CANDIDATE_LIMIT,
)
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.retrieval.schemas import RetrievalChunk, RetrievalHit
from app.core.retrieval.sparse import bm25_search
from app.core.utils.context_messages import message_token_text
from app.core.utils.message_parser import parse_db_messages_to_internal


@dataclass(frozen=True, slots=True)
class ChatHistoryRecallItem:
    role: str
    content: str
    truncated: bool = False
    message_id: int | None = field(default=None, repr=False, compare=False)
    created_at: datetime | None = field(default=None, repr=False, compare=False)
    session_id: str | None = field(default=None, repr=False, compare=False)
    sparse_score: float | None = field(default=None, repr=False, compare=False)
    sparse_rank: int | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ChatHistoryRecallResult:
    items: tuple[ChatHistoryRecallItem, ...] = ()


class ChatHistoryRecallService:
    async def recall(
        self,
        db: AsyncSession,
        uid: str,
        query: str,
        top_k: int = 5,
        result_max_chars: int = 4000,
        before_message_id: int | None = None,
    ) -> ChatHistoryRecallResult:
        normalized_uid = uid.strip() if isinstance(uid, str) else ""
        if not normalized_uid:
            raise ValueError(t(ERR_MEMORY_FIELD_REQUIRED, field="uid"))
        normalized_query = query.strip() if isinstance(query, str) else ""
        if not normalized_query:
            raise ValueError(t(ERR_MEMORY_FIELD_REQUIRED, field="query"))
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 50:
            raise ValueError(t(ERR_VALUE_MUST_BE_BETWEEN, field="top_k", minimum=1, maximum=50))
        if isinstance(result_max_chars, bool) or not isinstance(result_max_chars, int) or not 1 <= result_max_chars <= 50000:
            raise ValueError(t(ERR_VALUE_MUST_BE_BETWEEN, field="result_max_chars", minimum=1, maximum=50000))
        if before_message_id is not None and (isinstance(before_message_id, bool) or not isinstance(before_message_id, int) or before_message_id <= 0):
            raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="before_message_id"))

        raw_messages = await message_crud.list_recallable_chat_page(
            db,
            uid=normalized_uid,
            before_message_id=before_message_id,
            limit=MEMORY_CHAT_HISTORY_RECALL_CANDIDATE_LIMIT,
        )

        raw_messages_by_id = {message.id: message for message in raw_messages if message.id is not None}
        candidates: list[tuple[int, str, str, datetime | None, str | None]] = []
        for internal_message in parse_db_messages_to_internal(raw_messages):
            if internal_message.id is None:
                continue
            try:
                content = message_token_text(internal_message).strip()
            except Exception:
                continue
            if not content:
                continue
            role = internal_message.role.value if hasattr(internal_message.role, "value") else str(internal_message.role)
            raw_message = raw_messages_by_id.get(internal_message.id)
            candidates.append(
                (
                    internal_message.id,
                    role,
                    content,
                    raw_message.created_at if raw_message is not None else None,
                    raw_message.session_id if raw_message is not None else None,
                )
            )

        candidates.sort(key=lambda item: item[0], reverse=True)
        chunks = [
            RetrievalChunk(
                id=str(message_id),
                content=content,
                metadata={
                    "message_id": message_id,
                    "role": role,
                    "created_at": created_at,
                    "session_id": session_id,
                },
            )
            for message_id, role, content, created_at, session_id in candidates
        ]
        hits = await asyncio.to_thread(bm25_search, normalized_query, chunks, top_k)
        return ChatHistoryRecallResult(items=self._build_items(hits, result_max_chars))

    @staticmethod
    def _build_items(hits: list[RetrievalHit], result_max_chars: int) -> tuple[ChatHistoryRecallItem, ...]:
        items: list[ChatHistoryRecallItem] = []
        remaining_chars = result_max_chars
        for hit in hits:
            if remaining_chars <= 0:
                break
            metadata = hit.metadata or {}
            message_id = metadata.get("message_id")
            role = metadata.get("role", "")
            created_at = metadata.get("created_at")
            session_id = metadata.get("session_id")
            if len(hit.content) > remaining_chars:
                items.append(
                    ChatHistoryRecallItem(
                        role=role,
                        content=hit.content[:remaining_chars],
                        truncated=True,
                        message_id=message_id,
                        created_at=created_at,
                        session_id=session_id,
                        sparse_score=hit.sparse_score,
                        sparse_rank=hit.sparse_rank,
                    )
                )
                break
            items.append(
                ChatHistoryRecallItem(
                    role=role,
                    content=hit.content,
                    message_id=message_id,
                    created_at=created_at,
                    session_id=session_id,
                    sparse_score=hit.sparse_score,
                    sparse_rank=hit.sparse_rank,
                )
            )
            remaining_chars -= len(hit.content)
        return tuple(items)


chat_history_recall_service = ChatHistoryRecallService()


__all__ = [
    "ChatHistoryRecallItem",
    "ChatHistoryRecallResult",
    "ChatHistoryRecallService",
    "chat_history_recall_service",
]
