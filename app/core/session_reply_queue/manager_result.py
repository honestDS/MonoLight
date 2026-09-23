import asyncio
from typing import Any

from app.core.constants import (
    ERR_SESSION_REPLY_WORK_ENDED,
    ERR_SESSION_REPLY_WORK_NOT_FOUND,
)
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.i18n import t
from app.models.message import Message
from app.models.session_reply_work_item import (
    SessionReplyWorkStatus,
)

from .manager_common import (
    WORK_RESULT_POLL_INTERVAL_SECONDS,
    _get_work_failure_content,
    _raise_work_failure,
    build_identified_work_response,
    build_session_reply_work_event_id,
    get_work_request_ids,
)

__all__ = [
    "SessionReplyResult",
]


class SessionReplyResult:
    async def wait_for_session_stream(self, *, uid: str, session_id: str, history_message_id: int = 0):
        from app.providers.database import AsyncSessionLocal

        after_work_sequence_no = 0
        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.get_next_for_session_resume(
                    db,
                    uid=uid,
                    session_id=session_id,
                    history_message_id=history_message_id,
                    after_sequence_no=after_work_sequence_no,
                )
                resume_after_sequence_no = (
                    await session_reply_stream_event_crud.get_latest_resume_boundary_sequence(
                        db,
                        work_id=work.id,
                        history_message_id=history_message_id,
                    )
                    if work is not None and work.id is not None
                    else 0
                )
            if work is None or work.id is None:
                return

            after_work_sequence_no = work.sequence_no
            async for event in self.wait_for_stream(
                work.id,
                after_sequence_no=resume_after_sequence_no,
                resume_mode=True,
                history_message_id=history_message_id,
            ):
                yield event

    async def wait_for_result(self, work_id: int) -> dict[str, Any]:
        from app.providers.database import AsyncSessionLocal

        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.resolve_merged_target(db, work_id)
                if work is None:
                    raise RuntimeError(t(ERR_SESSION_REPLY_WORK_NOT_FOUND))
                if work.status == SessionReplyWorkStatus.SUCCEEDED:
                    response = (work.execution_state or {}).get("response")
                    if isinstance(response, dict):
                        return build_identified_work_response(work, response)
                    if work.result_message_id:
                        message = await db.get(Message, work.result_message_id)
                        return build_identified_work_response(work, {"content": message.content if message else ""})
                    return build_identified_work_response(work, {"content": ""})
                if work.status == SessionReplyWorkStatus.FAILED:
                    await _raise_work_failure(db, work)
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or t(ERR_SESSION_REPLY_WORK_ENDED, status=work.status))
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)

    async def wait_for_stream(
        self,
        work_id: int,
        *,
        after_sequence_no: int = 0,
        resume_mode: bool = False,
        history_message_id: int = 0,
    ):
        from app.providers.database import AsyncSessionLocal

        target_work_id = work_id
        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.resolve_merged_target(db, target_work_id)
                if work is None:
                    raise RuntimeError(t(ERR_SESSION_REPLY_WORK_NOT_FOUND))
                if work.id != target_work_id:
                    target_work_id = work.id
                    after_sequence_no = (
                        await session_reply_stream_event_crud.get_latest_resume_boundary_sequence(
                            db,
                            work_id=target_work_id,
                            history_message_id=history_message_id,
                        )
                        if resume_mode
                        else 0
                    )

                events = await session_reply_stream_event_crud.list_after_sequence(
                    db,
                    work_id=target_work_id,
                    after_sequence_no=after_sequence_no,
                )
                for item in events:
                    after_sequence_no = item.sequence_no
                    yield item.event

                if events:
                    continue

                if work.status == SessionReplyWorkStatus.SUCCEEDED:
                    response = (work.execution_state or {}).get("response")
                    if not isinstance(response, dict):
                        response = await self.wait_for_result(target_work_id)
                    identified_response = build_identified_work_response(work, response)
                    response_id = identified_response.get("response_id")
                    if not isinstance(response_id, str) or not response_id:
                        response_id = f"session-reply-work:{target_work_id}"
                    done_event = {
                        "type": "done",
                        "session_id": work.session_id,
                        "work_id": target_work_id,
                        "response_id": response_id,
                        "history": identified_response.get("history", []),
                        "files": identified_response.get("files"),
                        "response": identified_response,
                        "request_ids": get_work_request_ids(work),
                    }
                    if identified_response.get("message_id") is not None:
                        done_event["message_id"] = identified_response["message_id"]
                    yield done_event
                    return
                if work.status == SessionReplyWorkStatus.FAILED:
                    error_content = await _get_work_failure_content(db, work)
                    yield {
                        "event_id": build_session_reply_work_event_id(work, error=True),
                        "type": "error",
                        "message": error_content,
                        "session_id": work.session_id,
                        "work_id": target_work_id,
                        "request_ids": get_work_request_ids(work),
                    }
                    return
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or t(ERR_SESSION_REPLY_WORK_ENDED, status=work.status))
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)
