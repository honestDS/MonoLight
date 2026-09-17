from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT,
    ERR_SESSION_REPLY_NO_FOREGROUND_INPUT,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.i18n import t
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_work_item import (
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)

from .manager_common import (
    logger,
    merge_work_request_ids,
)

__all__ = [
    "SessionReplyFreeze",
]


class SessionReplyFreeze:
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
        bounded_contiguous: list[SessionReplyWorkItem] = []
        for item in contiguous:
            is_audit_decision = bool((item.execution_state or {}).get("audit_decision_response"))
            if is_audit_decision and item.id != work.id:
                break
            bounded_contiguous.append(item)
            if is_audit_decision:
                break
        contiguous = bounded_contiguous
        source_message_ids = [int(item.source_id) for item in contiguous]
        message_types = [MessageType.AUDIT_DECISION] if bool((work.execution_state or {}).get("audit_decision_response")) else [MessageType.TEXT]
        message_result = await db.execute(
            select(Message).where(
                Message.id.in_(source_message_ids),
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.USER,
                Message.type.in_(message_types),
                Message.is_processed == False,  # noqa: E712
            )
        )
        messages = self._reorder_messages_by_ids(
            list(message_result.scalars().all()),
            source_message_ids,
        )
        if messages is None or not messages:
            if work.input_message_ids:
                return await self._load_frozen_input(db, work.input_message_ids)
            raise RuntimeError(t(ERR_SESSION_REPLY_NO_FOREGROUND_INPUT))
        message_ids = list(source_message_ids)

        merged_ids = [item.id for item in contiguous[1:] if item.id is not None]
        if len(merged_ids) != len(contiguous[1:]):
            await db.rollback()
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT))
        if merged_ids:
            updated_merged_ids = await session_reply_work_item_crud.merge_ready_foreground(
                db,
                work_ids=merged_ids,
                merged_into_id=work.id,
            )
            if updated_merged_ids is None or len(updated_merged_ids) != len(merged_ids) or set(updated_merged_ids) != set(merged_ids):
                await db.rollback()
                raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT))

        message_update_result = await db.execute(
            update(Message)
            .where(
                Message.id.in_(message_ids),
                Message.is_processed == False,  # noqa: E712
            )
            .values(is_processed=True)
        )
        if (message_update_result.rowcount or 0) != len(message_ids):
            await db.rollback()
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT))
        stream_requested = any(bool((item.execution_state or {}).get("stream_requested")) for item in contiguous)
        context_summary_events_requested = any(bool((item.execution_state or {}).get("context_summary_events_requested")) for item in contiguous)
        show_tool_calls = all(bool((item.execution_state or {}).get("show_tool_calls", True)) for item in contiguous)
        expose_tool_call_content = all(bool((item.execution_state or {}).get("expose_tool_call_content", True)) for item in contiguous)
        latest_guidance_prompt = next(
            (message.guidance_prompt for message in reversed(messages) if isinstance(message.guidance_prompt, str) and message.guidance_prompt.strip()),
            None,
        )
        execution_state = {
            **(work.execution_state or {}),
            "stream_requested": stream_requested,
            "context_summary_events_requested": context_summary_events_requested,
            "show_tool_calls": show_tool_calls,
            "expose_tool_call_content": expose_tool_call_content,
            "request_ids": merge_work_request_ids(*contiguous),
        }
        if latest_guidance_prompt is not None:
            execution_state["guidance_prompt"] = latest_guidance_prompt
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "input_message_ids": message_ids,
                "execution_state": execution_state,
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT))
        await db.commit()
        work.input_message_ids = message_ids
        work.execution_state = execution_state
        return self._merge_messages(messages)

    async def absorb_contiguous_foreground_messages(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
    ) -> UserInputBatch | None:
        work = await session_reply_work_item_crud.get(db, work_id)
        if (
            work is None
            or work.status != SessionReplyWorkStatus.RUNNING
            or work.locked_by != worker_id
            or work.work_type
            not in {
                SessionReplyWorkType.FOREGROUND_REPLY,
                SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            }
        ):
            return None
        if bool((work.execution_state or {}).get("audit_decision_response")):
            return None

        contiguous = await session_reply_work_item_crud.list_contiguous_foreground(db, work=work)
        additional_work: list[SessionReplyWorkItem] = []
        for item in contiguous:
            if item.id == work.id:
                continue
            if bool((item.execution_state or {}).get("audit_decision_response")):
                break
            if item.status != SessionReplyWorkStatus.READY_FOR_LLM or not item.source_id:
                continue
            additional_work.append(item)
        if not additional_work:
            return None

        source_work_message_ids = [int(item.source_id) for item in additional_work]
        if len(set(source_work_message_ids)) != len(source_work_message_ids):
            return None
        message_result = await db.execute(
            select(Message).where(
                Message.id.in_(source_work_message_ids),
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.TEXT,
                Message.is_processed == False,  # noqa: E712
            )
        )
        messages = self._reorder_messages_by_ids(
            list(message_result.scalars().all()),
            source_work_message_ids,
        )
        if messages is None:
            return None
        message_ids = list(source_work_message_ids)

        merged_work_ids = [item.id for item in additional_work if item.id is not None]
        if len(merged_work_ids) != len(additional_work):
            return None
        updated_merged_work_ids = await session_reply_work_item_crud.merge_ready_foreground(
            db,
            work_ids=merged_work_ids,
            merged_into_id=work.id,
        )
        if updated_merged_work_ids is None or len(updated_merged_work_ids) != len(merged_work_ids) or set(updated_merged_work_ids) != set(merged_work_ids):
            await db.rollback()
            return None

        message_update_result = await db.execute(
            update(Message)
            .where(
                Message.id.in_(message_ids),
                Message.is_processed == False,  # noqa: E712
            )
            .values(is_processed=True)
        )
        if (message_update_result.rowcount or 0) != len(message_ids):
            await db.rollback()
            return None
        frozen_message_ids = list(work.input_message_ids or [])
        merged_work = [work, *additional_work]
        execution_state = {
            **(work.execution_state or {}),
            "request_ids": merge_work_request_ids(work, *additional_work),
            "show_tool_calls": all(bool((item.execution_state or {}).get("show_tool_calls", True)) for item in merged_work),
            "expose_tool_call_content": all(bool((item.execution_state or {}).get("expose_tool_call_content", True)) for item in merged_work),
        }
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "input_message_ids": [*frozen_message_ids, *message_ids],
                "execution_state": execution_state,
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            return None

        await db.commit()
        work.input_message_ids = [*frozen_message_ids, *message_ids]
        work.execution_state = execution_state
        content, attachments, _ids = self._merge_messages(messages)
        latest_guidance_prompt = next(
            (message.guidance_prompt for message in reversed(messages) if isinstance(message.guidance_prompt, str) and message.guidance_prompt.strip()),
            None,
        )
        source_message_ids = tuple(dict.fromkeys(message_ids))
        combined_message = InternalMessage(
            id=source_message_ids[-1],
            role=MessageRole.USER,
            content=content or None,
            attachments=attachments or None,
            guidance_prompt=latest_guidance_prompt,
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
        return UserInputBatch(
            messages=(combined_message,),
            source_message_ids=source_message_ids,
        )

    @staticmethod
    def _reorder_messages_by_ids(messages: list[Message], message_ids: list[int]) -> list[Message] | None:
        if len(set(message_ids)) != len(message_ids):
            return None
        messages_by_id = {message.id: message for message in messages if message.id is not None}
        if len(messages) != len(message_ids) or len(messages_by_id) != len(message_ids) or set(messages_by_id) != set(message_ids):
            return None
        return [messages_by_id[message_id] for message_id in message_ids]

    async def _load_frozen_input(self, db: AsyncSession, message_ids: list[int]) -> tuple[str, list[str], list[int]]:
        result = await db.execute(select(Message).where(Message.id.in_(message_ids)))
        messages = self._reorder_messages_by_ids(list(result.scalars().all()), message_ids)
        if messages is None:
            raise RuntimeError(t(ERR_SESSION_REPLY_NO_FOREGROUND_INPUT))
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
