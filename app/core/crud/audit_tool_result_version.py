from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.crud.session import session_crud
from app.models.audit import AuditToolResultVersion
from app.models.message import Message


class CRUDAuditToolResultVersion(CRUDBase[AuditToolResultVersion, AuditToolResultVersion, AuditToolResultVersion]):
    async def append_version(
        self,
        db: AsyncSession,
        uid: str,
        session_id: str,
        audit_record_id: int,
        source_assistant_message_id: int,
        original_tool_call_id: str,
        message_id: int,
        content: str,
        commit: bool = True,
    ) -> AuditToolResultVersion:
        latest_version_result = await db.execute(
            select(func.max(AuditToolResultVersion.version_no)).where(
                AuditToolResultVersion.audit_record_id == audit_record_id,
                AuditToolResultVersion.original_tool_call_id == original_tool_call_id,
            )
        )
        latest_version = latest_version_result.scalar_one()
        version_no = 0 if latest_version is None else int(latest_version) + 1
        version = AuditToolResultVersion(
            uid=uid,
            session_id=session_id,
            audit_record_id=audit_record_id,
            source_assistant_message_id=source_assistant_message_id,
            original_tool_call_id=original_tool_call_id,
            message_id=message_id,
            version_no=version_no,
            content=content,
        )
        db.add(version)

        projection_result = await db.execute(
            update(Message)
            .where(Message.id == message_id)
            .values(
                content=content,
                audit_record_id=audit_record_id,
                audit_tool_call_id=original_tool_call_id,
                content_revision=version_no,
            )
            .execution_options(synchronize_session=False)
        )
        if (projection_result.rowcount or 0) != 1:
            raise LookupError(message_id)

        await session_crud.bump_context_content_revision(
            db,
            session_id=session_id,
            uid=uid,
            commit=False,
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        await db.refresh(version)
        return version


audit_tool_result_version_crud = CRUDAuditToolResultVersion(AuditToolResultVersion)
