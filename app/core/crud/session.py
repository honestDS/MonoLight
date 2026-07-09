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

    async def upsert_profile(self, db: AsyncSession, *, session_id: str, uid: str, profile_id: int, source: str = "http", reply_target_source: str | None = None) -> ChatSession:
        session = await self.get_by_session_id(db, session_id)
        if session:
            if session.uid != uid:
                return session
            session.profile_id = profile_id
            if not session.source:
                session.source = source
            if reply_target_source is not None:
                session.reply_target_source = reply_target_source
            db.add(session)
        else:
            session = ChatSession(session_id=session_id, uid=uid, profile_id=profile_id, source=source, reply_target_source=reply_target_source or source)
            db.add(session)
        await db.flush()
        return session


session_crud = CRUDSession(ChatSession)
