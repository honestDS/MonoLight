import asyncio
import hashlib
import json
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core import constants
from app.core.crud.session import session_crud
from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.dispatcher.markdown_instruction import append_user_runtime_instructions
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)

logger = get_logger(__name__)

WORK_RESULT_POLL_INTERVAL_SECONDS = 0.2


def _serialize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if hasattr(content, "model_dump"):
        content = content.model_dump(mode="json")
    return json.dumps(content, ensure_ascii=False)


def build_foreground_message_dedupe_key(session_id: str, message_id: int) -> str:
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"foreground-message:{session_digest}:{message_id}"


async def _raise_work_failure(db: AsyncSession, work: SessionReplyWorkItem) -> None:
    message = await db.get(Message, work.result_message_id) if work.result_message_id else None
    error_content = message.content if message and message.content else None
    raise BaseBusinessException(
        message=error_content or constants.ERR_LLM_UNEXPECTED_ERROR,
        default_message=error_content,
    )


class SessionReplyQueueManager:
    async def enqueue_foreground_message(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile: Profile,
        message: str | list[dict[str, Any]],
        attachments: list[str] | None,
        source: str,
        stream_requested: bool | None = None,
        context_summary_events_requested: bool | None = None,
    ) -> tuple[InternalMessage, SessionReplyWorkItem]:
        profile_id = profile.id if profile and profile.id else -1
        if profile_id > 0:
            await session_crud.upsert_profile(
                db,
                session_id=session_id,
                uid=uid,
                profile_id=profile_id,
                source=source,
            )

        message_row = Message(
            session_id=session_id,
            uid=uid,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=_serialize_message_content(message),
            attachments=attachments,
            profile_id=profile_id,
            is_processed=False,
        )
        db.add(message_row)
        await db.flush()
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=message_row.id,
            dedupe_key=build_foreground_message_dedupe_key(session_id, message_row.id),
            commit=False,
        )
        if created:
            work.execution_state = {
                **(work.execution_state or {}),
                "stream_requested": source == "ws" if stream_requested is None else stream_requested,
                "context_summary_events_requested": source == "ws" if context_summary_events_requested is None else context_summary_events_requested,
                # 微信 OpenClaw 对发送频率有限制，工具调用阶段的正文只保留在
                # 数据库和日志中，不作为额外的用户可见消息发送到微信。
                "expose_tool_call_content": source != "weixin-openclaw",
            }
            db.add(work)
        await db.commit()
        await db.refresh(message_row)
        await db.refresh(work)
        return (
            InternalMessage(
                id=message_row.id,
                role=MessageRole.USER,
                content=message_row.content,
                attachments=message_row.attachments,
                created_at=message_row.created_at.timestamp(),
            ),
            work,
        )

    async def enqueue_background_summary(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        background_task_id: int,
        commit: bool = True,
    ) -> tuple[SessionReplyWorkItem, bool]:
        return await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id=background_task_id,
            dedupe_key=f"background-task-summary:{background_task_id}",
            commit=commit,
        )

    async def enqueue_scheduled_summary(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        scheduled_task_id: int,
        trigger_message_id: int,
        commit: bool = True,
    ) -> tuple[SessionReplyWorkItem, bool]:
        return await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id=trigger_message_id,
            dedupe_key=f"scheduled-task-summary:{scheduled_task_id}:{trigger_message_id}",
            commit=commit,
        )

    async def freeze_foreground_input(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        worker_id: str,
    ) -> tuple[str, list[str], list[int]]:
        if work.input_message_ids:
            return await self._load_frozen_input(db, work.input_message_ids)

        contiguous = await session_reply_work_item_crud.list_contiguous_foreground(db, work=work)
        source_message_ids = [int(item.source_id) for item in contiguous]
        message_result = await db.execute(
            select(Message)
            .where(
                Message.id.in_(source_message_ids),
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.TEXT,
                Message.is_processed == False,  # noqa: E712
            )
            .order_by(Message.id)
        )
        messages = list(message_result.scalars().all())
        message_ids = [message.id for message in messages if message.id is not None]
        if not message_ids:
            if work.input_message_ids:
                return await self._load_frozen_input(db, work.input_message_ids)
            raise RuntimeError("No unprocessed foreground messages are available")

        await db.execute(update(Message).where(Message.id.in_(message_ids)).values(is_processed=True))
        merged_ids = [item.id for item in contiguous[1:] if item.id is not None]
        if merged_ids:
            await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.id.in_(merged_ids),
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                )
                .values(
                    status=SessionReplyWorkStatus.MERGED,
                    merged_into_id=work.id,
                    locked_by=None,
                    lock_until=None,
                )
            )
        stream_requested = any(bool((item.execution_state or {}).get("stream_requested")) for item in contiguous)
        context_summary_events_requested = any(bool((item.execution_state or {}).get("context_summary_events_requested")) for item in contiguous)
        expose_tool_call_content = all(bool((item.execution_state or {}).get("expose_tool_call_content", True)) for item in contiguous)
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "input_message_ids": message_ids,
                "execution_state": {
                    **(work.execution_state or {}),
                    "stream_requested": stream_requested,
                    "context_summary_events_requested": context_summary_events_requested,
                    "expose_tool_call_content": expose_tool_call_content,
                },
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            raise RuntimeError("Session reply work lease was lost while freezing input")
        await db.commit()
        return self._merge_messages(messages)

    async def absorb_contiguous_foreground_messages(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
    ) -> list[InternalMessage]:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id or work.work_type != SessionReplyWorkType.FOREGROUND_REPLY:
            return []

        contiguous = await session_reply_work_item_crud.list_contiguous_foreground(db, work=work)
        additional_work = [item for item in contiguous if item.id != work.id and item.status == SessionReplyWorkStatus.READY_FOR_LLM and item.source_id]
        if not additional_work:
            return []

        source_message_ids = [int(item.source_id) for item in additional_work]
        message_result = await db.execute(
            select(Message)
            .where(
                Message.id.in_(source_message_ids),
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.TEXT,
                Message.is_processed == False,  # noqa: E712
            )
            .order_by(Message.id)
        )
        messages = list(message_result.scalars().all())
        message_ids = [message.id for message in messages if message.id is not None]
        if not message_ids:
            return []

        await db.execute(update(Message).where(Message.id.in_(message_ids)).values(is_processed=True))
        merged_work_ids = [item.id for item in additional_work if item.id is not None]
        await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id.in_(merged_work_ids),
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            )
            .values(
                status=SessionReplyWorkStatus.MERGED,
                merged_into_id=work.id,
                locked_by=None,
                lock_until=None,
            )
        )
        frozen_message_ids = list(work.input_message_ids or [])
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"input_message_ids": [*frozen_message_ids, *message_ids]},
            commit=False,
        )
        if not updated:
            await db.rollback()
            return []

        await db.commit()
        content, attachments, _ids = self._merge_messages(messages)
        combined_message = InternalMessage(
            id=message_ids[-1],
            role=MessageRole.USER,
            content=content or None,
            attachments=attachments or None,
        )
        logger.bind(
            uid=work.uid,
            session_id=work.session_id,
            work_id=work.id,
            message_ids=message_ids,
        ).info(
            t(
                "LOG_DISPATCHER_NON_STREAM_ADDITIONAL_MESSAGES",
                message=content,
                attachments=str(attachments),
            )
        )
        await append_user_runtime_instructions(db, work.session_id, combined_message)
        return [combined_message]

    async def _load_frozen_input(self, db: AsyncSession, message_ids: list[int]) -> tuple[str, list[str], list[int]]:
        result = await db.execute(select(Message).where(Message.id.in_(message_ids)).order_by(Message.id))
        messages = list(result.scalars().all())
        content, attachments, _ids = self._merge_messages(messages)
        return content, attachments, message_ids

    @staticmethod
    def _merge_messages(messages: list[Message]) -> tuple[str, list[str], list[int]]:
        contents = [message.content or "" for message in messages]
        attachments: list[str] = []
        seen: set[str] = set()
        for message in messages:
            for attachment in message.attachments or []:
                if attachment not in seen:
                    seen.add(attachment)
                    attachments.append(attachment)
        return "\n".join(contents), attachments, [message.id for message in messages if message.id is not None]

    async def wait_for_result(self, work_id: int) -> dict[str, Any]:
        from app.providers.database import AsyncSessionLocal

        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.resolve_merged_target(db, work_id)
                if work is None:
                    raise RuntimeError("Session reply work no longer exists")
                if work.status == SessionReplyWorkStatus.SUCCEEDED:
                    response = (work.execution_state or {}).get("response")
                    if isinstance(response, dict):
                        return {**response, "work_id": work.id}
                    if work.result_message_id:
                        message = await db.get(Message, work.result_message_id)
                        return {"content": message.content if message else "", "work_id": work.id}
                    return {"content": "", "work_id": work.id}
                if work.status == SessionReplyWorkStatus.FAILED:
                    await _raise_work_failure(db, work)
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or f"Session reply work ended with status {work.status}")
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)

    async def wait_for_stream(self, work_id: int):
        from app.providers.database import AsyncSessionLocal

        target_work_id = work_id
        after_sequence_no = 0
        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.resolve_merged_target(db, target_work_id)
                if work is None:
                    raise RuntimeError("Session reply work no longer exists")
                if work.id != target_work_id:
                    target_work_id = work.id
                    after_sequence_no = 0

                events = await session_reply_stream_event_crud.list_after_sequence(
                    db,
                    work_id=target_work_id,
                    after_sequence_no=after_sequence_no,
                )
                for item in events:
                    after_sequence_no = item.sequence_no
                    yield item.event

                if work.status == SessionReplyWorkStatus.SUCCEEDED:
                    response = (work.execution_state or {}).get("response")
                    if not isinstance(response, dict):
                        response = await self.wait_for_result(target_work_id)
                    yield {
                        "type": "done",
                        "session_id": work.session_id,
                        "work_id": target_work_id,
                        "response_id": f"session-reply-work:{target_work_id}",
                        "history": response.get("history", []),
                        "files": response.get("files"),
                        "response": response,
                    }
                    return
                if work.status == SessionReplyWorkStatus.FAILED:
                    await _raise_work_failure(db, work)
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or f"Session reply work ended with status {work.status}")
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)


session_reply_queue_manager = SessionReplyQueueManager()
