from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.session_reply_provider_usage import SessionReplyProviderUsage


class CRUDSessionReplyProviderUsage:
    async def get_by_provider_request_id(self, db: AsyncSession, provider_request_id: str) -> SessionReplyProviderUsage | None:
        result = await db.execute(select(SessionReplyProviderUsage).where(SessionReplyProviderUsage.provider_request_id == provider_request_id))
        return result.scalars().first()

    async def create_once(
        self,
        db: AsyncSession,
        *,
        provider_request_id: str,
        work_id: int,
        session_id: str,
        uid: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
    ) -> tuple[SessionReplyProviderUsage, bool]:
        item = SessionReplyProviderUsage(
            provider_request_id=provider_request_id,
            work_id=work_id,
            session_id=session_id,
            uid=uid,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
        )

        try:
            async with db.begin_nested():
                db.add(item)
                await db.flush()
        except IntegrityError:
            existing = await self.get_by_provider_request_id(db, provider_request_id)
            if existing is None:
                raise

            values_match = existing.work_id == work_id and existing.session_id == session_id and existing.uid == uid and existing.input_tokens == input_tokens and existing.cached_tokens == cached_tokens and existing.output_tokens == output_tokens
            if not values_match:
                raise

            return existing, False

        return item, True


session_reply_provider_usage_crud = CRUDSessionReplyProviderUsage()
