from sqlalchemy import update
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
        summary: str,
        message_id: int,
    ) -> bool:
        stmt = update(ChatSession).where(ChatSession.session_id == session_id).where(ChatSession.uid == uid)
        if expected_message_id is None:
            stmt = stmt.where(ChatSession.context_summary_message_id.is_(None))
        else:
            stmt = stmt.where(ChatSession.context_summary_message_id == expected_message_id)

        result = await db.execute(
            stmt.values(
                context_summary=summary,
                context_summary_message_id=message_id,
            )
        )
        await db.flush()
        return (result.rowcount or 0) == 1

    async def upsert_profile(self, db: AsyncSession, *, session_id: str, uid: str, profile_id: int, source: str = "http") -> ChatSession:
        session = await self.get_by_session_id(db, session_id)
        if session:
            if session.uid != uid:
                return session
            session.profile_id = profile_id
            if not session.source:
                session.source = source
            db.add(session)
        else:
            session = ChatSession(session_id=session_id, uid=uid, profile_id=profile_id, source=source)
            db.add(session)
        await db.flush()
        return session


session_crud = CRUDSession(ChatSession)
