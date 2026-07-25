from typing import Any

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.session import ChatSession


class CRUDSession(CRUDBase[ChatSession, ChatSession, ChatSession]):
    async def get_by_session_id(self, db: AsyncSession, session_id: str) -> ChatSession | None:
        result = await db.execute(select(ChatSession).where(ChatSession.session_id == session_id))
        return result.scalars().first()

    async def create_or_update_title(self, db: AsyncSession, session_id: str, uid: str, title: str) -> ChatSession:
        session = await self.get_by_session_id(db, session_id)
        if session:
            session.title = title
            db.add(session)
        else:
            session = ChatSession(session_id=session_id, uid=uid, title=title)
            db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def update_context_summary(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        expected_message_id: int | None,
        expected_revision: int,
        expected_content_revision: int = 0,
        summary: str,
        message_id: int,
    ) -> bool:
        stmt = update(ChatSession).where(ChatSession.session_id == session_id).where(ChatSession.uid == uid).where(ChatSession.context_summary_revision == expected_revision).where(ChatSession.context_content_revision == expected_content_revision)
        if expected_message_id is None:
            stmt = stmt.where(ChatSession.context_summary_message_id.is_(None))
        else:
            stmt = stmt.where(ChatSession.context_summary_message_id == expected_message_id)

        result = await db.execute(
            stmt.values(
                context_summary=summary,
                context_summary_message_id=message_id,
                context_summary_revision=ChatSession.context_summary_revision + 1,
            )
        )
        await db.flush()
        return (result.rowcount or 0) == 1

    async def bump_context_content_revision(
        self,
        db: AsyncSession,
        session_id: str,
        uid: str,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(ChatSession)
            .where(
                ChatSession.session_id == session_id,
                ChatSession.uid == uid,
            )
            .values(
                context_summary=None,
                context_summary_message_id=None,
                context_summary_revision=ChatSession.context_summary_revision + 1,
                context_content_revision=ChatSession.context_content_revision + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def update_llm_request_metadata(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        metadata: dict[str, Any],
        commit: bool = True,
    ) -> bool:
        if not isinstance(metadata, dict):
            return False

        required_int_fields = ("input_tokens", "context_window_tokens", "max_output_tokens")
        persisted_metadata = {field: metadata.get(field) for field in required_int_fields}
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in persisted_metadata.values()):
            return False

        optional_int_fields = (
            "request_message_min_id",
            "request_message_max_id",
            "context_summary_revision",
            "context_content_revision",
            "system_tokens",
            "tools_tokens",
        )
        for field in optional_int_fields:
            if field not in metadata:
                continue
            value = metadata[field]
            minimum = 1 if field in {"request_message_min_id", "request_message_max_id"} else 0
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                return False
            persisted_metadata[field] = value

        for field in ("model_id", "protocol"):
            if field not in metadata:
                continue
            value = metadata[field]
            if not isinstance(value, str) or not value.strip():
                return False
            persisted_metadata[field] = value

        if "input_tokens_source" in metadata:
            input_tokens_source = metadata["input_tokens_source"]
            if not isinstance(input_tokens_source, str) or input_tokens_source not in {"estimated", "provider"}:
                return False
            persisted_metadata["input_tokens_source"] = input_tokens_source

        result = await db.execute(
            update(ChatSession)
            .where(
                ChatSession.session_id == session_id,
                ChatSession.uid == uid,
            )
            .values(llm_request_metadata=persisted_metadata)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def delete_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        conditions = [ChatSession.session_id == session_id]
        if not is_admin:
            conditions.append(ChatSession.uid == uid)
        result = await db.execute(delete(ChatSession).where(*conditions).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def upsert_profile(self, db: AsyncSession, *, session_id: str, uid: str, profile_id: int, source: str = "http") -> ChatSession:
        session = await self.get_by_session_id(db, session_id)
        if session:
            if session.uid != uid:
                return session
            session.profile_id = profile_id
            if not session.source:
                session.source = source
            session.reply_target_source = session.source
            db.add(session)
        else:
            session = ChatSession(
                session_id=session_id,
                uid=uid,
                profile_id=profile_id,
                source=source,
                reply_target_source=source,
            )
            db.add(session)
        await db.flush()
        return session


session_crud = CRUDSession(ChatSession)
