from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.audit import AuditRecord, AuditToolResultVersion
from app.models.background_task import BackgroundTask
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseEmbeddingDelta,
    KnowledgeBaseProfileBinding,
    KnowledgeJob,
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingSelectionToken,
    LongTermMemoryMutationJob,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemoryStore,
)
from app.models.message import Message
from app.models.message_platform import MessagePlatform
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.models.session_todo import SessionTodoPlan
from app.models.system_log import SystemLog
from app.models.terminal_session import TerminalSession
from app.models.user import (
    User,
    UserCreate,
    UserUpdate,
)


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    async def get_by_username(self, db: AsyncSession, username: str) -> User | None:
        result = await db.execute(select(User).where(User.username == username))
        return result.scalars().first()

    async def get_by_uid(
        self,
        db: AsyncSession,
        uid: str,
        *,
        lock_for_reference_write: bool = False,
    ) -> User | None:
        query = select(User).where(User.uid == uid)
        if lock_for_reference_write:
            await db.execute(update(User).where(User.uid == uid).values(uid=User.uid, updated_at=User.updated_at).execution_options(synchronize_session=False))
            query = query.with_for_update().execution_options(populate_existing=True)
        result = await db.execute(query)
        return result.scalars().first()

    async def get_deletion_state(
        self,
        db: AsyncSession,
        *,
        uid: str,
    ) -> tuple[list[str], list[KnowledgeBase], list[LongTermMemoryStore], list[LongTermMemoryEmbeddingRevision]]:
        result = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).with_for_update().execution_options(populate_existing=True))
        memory_stores = list(result.scalars().all())

        result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.uid == uid).order_by(KnowledgeBase.id.asc()).with_for_update().execution_options(populate_existing=True))
        knowledge_bases = list(result.scalars().all())

        result = await db.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == uid).order_by(LongTermMemoryEmbeddingRevision.id.asc()).with_for_update().execution_options(populate_existing=True))
        memory_revisions = list(result.scalars().all())

        result = await db.execute(select(ChatSession.session_id).where(ChatSession.uid == uid).order_by(ChatSession.session_id.asc()).with_for_update())
        session_ids = list(result.scalars().all())

        return session_ids, knowledge_bases, memory_stores, memory_revisions

    async def lock_owned_configuration(self, db: AsyncSession, *, uid: str) -> None:
        await db.execute(select(PromptLibrary).where(PromptLibrary.uid == uid).order_by(PromptLibrary.id.asc()).with_for_update().execution_options(populate_existing=True))
        await db.execute(select(Profile).where(Profile.uid == uid).order_by(Profile.id.asc()).with_for_update().execution_options(populate_existing=True))

    async def unlink_configuration_references(self, db: AsyncSession, *, uid: str) -> None:
        profile_ids = select(Profile.id).where(Profile.uid == uid)
        prompt_ids = select(PromptLibrary.id).where(PromptLibrary.uid == uid)

        await db.execute(update(ChatSession).where(ChatSession.uid != uid, ChatSession.profile_id.in_(profile_ids)).values(profile_id=None))
        await db.execute(update(ChatSession).where(ChatSession.uid != uid, ChatSession.profile_override_id.in_(profile_ids)).values(profile_override_id=None))
        await db.execute(update(ScheduledTask).where(ScheduledTask.uid != uid, ScheduledTask.profile_id.in_(profile_ids)).values(profile_id=None))
        await db.execute(update(MessagePlatform).where(or_(MessagePlatform.uid != uid, MessagePlatform.uid.is_(None)), MessagePlatform.profile_id.in_(profile_ids)).values(profile_id=None))
        await db.execute(update(Profile).where(or_(Profile.uid != uid, Profile.uid.is_(None)), Profile.prompt_id.in_(prompt_ids)).values(prompt_id=None))

    async def delete_owned_memory_data(self, db: AsyncSession, *, uid: str) -> None:
        await db.execute(delete(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid))
        await db.execute(delete(LongTermMemoryEmbeddingDelta).where(LongTermMemoryEmbeddingDelta.uid == uid))
        await db.execute(delete(LongTermMemoryRevision).where(LongTermMemoryRevision.uid == uid))
        await db.execute(delete(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid))
        await db.execute(delete(LongTermMemoryEmbeddingSelectionToken).where(LongTermMemoryEmbeddingSelectionToken.uid == uid))
        await db.execute(delete(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == uid))
        await db.execute(delete(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid))

    async def delete_owned_knowledge_data(self, db: AsyncSession, *, uid: str) -> None:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.uid == uid))
        await db.execute(delete(KnowledgeOrganizationFragment).where(KnowledgeOrganizationFragment.uid == uid))
        await db.execute(delete(KnowledgeOrganizationStage).where(KnowledgeOrganizationStage.uid == uid))
        await db.execute(delete(KnowledgeJob).where(KnowledgeJob.uid == uid))
        await db.execute(delete(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.uid == uid))
        await db.execute(delete(ManagedKnowledgeItem).where(ManagedKnowledgeItem.uid == uid))
        await db.execute(delete(KnowledgeBaseEmbeddingDelta).where(KnowledgeBaseEmbeddingDelta.uid == uid))
        await db.execute(delete(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.uid == uid))
        await db.execute(delete(KnowledgeOrganizationSnapshot).where(KnowledgeOrganizationSnapshot.uid == uid))

    async def delete_owned_session_data(self, db: AsyncSession, *, uid: str) -> None:
        await db.execute(delete(ContextSummaryFragment).where(ContextSummaryFragment.uid == uid))
        await db.execute(delete(ContextSummaryStage).where(ContextSummaryStage.uid == uid))
        await db.execute(delete(BackgroundTask).where(BackgroundTask.uid == uid))
        await db.execute(delete(ScheduledTask).where(ScheduledTask.uid == uid))
        await db.execute(delete(MessagePlatform).where(MessagePlatform.uid == uid))
        await db.execute(delete(AuditToolResultVersion).where(AuditToolResultVersion.uid == uid))
        await db.execute(delete(AuditRecord).where(AuditRecord.uid == uid))
        await db.execute(delete(SystemLog).where(SystemLog.uid == uid))
        await db.execute(delete(Message).where(Message.uid == uid))
        await db.execute(delete(MessagePlatformOutbox).where(MessagePlatformOutbox.uid == uid))
        await db.execute(delete(SessionEvent).where(SessionEvent.uid == uid))
        await db.execute(delete(SessionTodoPlan).where(SessionTodoPlan.uid == uid))
        await db.execute(delete(SessionReplyWorkItem).where(SessionReplyWorkItem.uid == uid))
        await db.execute(delete(TerminalSession).where(TerminalSession.uid == uid))
        await db.execute(delete(ChatSession).where(ChatSession.uid == uid))

    async def delete_owned_configuration(self, db: AsyncSession, *, uid: str) -> None:
        await db.execute(delete(Profile).where(Profile.uid == uid))
        await db.execute(delete(PromptLibrary).where(PromptLibrary.uid == uid))

    async def get_superuser(self, db: AsyncSession) -> User | None:
        result = await db.execute(select(User).where(User.is_superuser.is_(True)).order_by(User.id.asc()))
        return result.scalars().first()

    async def get_multi_by_uids(self, db: AsyncSession, uids: list[str]) -> list[User]:
        if not uids:
            return []
        result = await db.execute(select(User).where(User.uid.in_(uids)))
        return list(result.scalars().all())


user_crud = CRUDUser(User)
