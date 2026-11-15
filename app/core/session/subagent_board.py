import json
from time import time
from typing import Any

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_SUBAGENT_NOT_FOUND,
    ERR_SUBAGENT_WORK_NOT_FOUND,
    SUBAGENT_BOARD_ACTIVE_PAGE_SIZE,
    SUBAGENT_BOARD_POST_DISPLAY_CHARS,
    SUBAGENT_BOARD_POST_PAGE_SIZE,
    SUBAGENT_BOARD_REPORT_DISPLAY_CHARS,
    SUBAGENT_BOARD_ROUND_PAGE_SIZE,
)
from app.core.crud.session.subagent import subagent_crud
from app.core.exceptions import ParameterException
from app.core.session_reply_queue.child_result import get_child_snapshot
from app.models.session_reply_work_item import SessionReplyWorkStatus

__all__ = ["get_session_subagent_detail", "list_session_subagents"]


_FINAL_STAGES = frozenset({"succeeded", "failed", "cancelled", "execution_unknown", "ended_without_reply"})


def _is_query_result_prompt(content: str | None) -> bool:
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("full_result_operation") == "query_result"


def _is_hidden_post(post: RowMapping) -> bool:
    return post["stage"] in _FINAL_STAGES or _is_query_result_prompt(post["content"])


def _truncate_text(value: Any, *, limit: int) -> tuple[str | None, bool]:
    if not isinstance(value, str) or not value:
        return None, False
    return value[:limit], len(value) > limit


def _build_post(post: RowMapping) -> dict[str, Any]:
    content, truncated = _truncate_text(post["content"], limit=SUBAGENT_BOARD_POST_DISPLAY_CHARS)
    return {
        "id": post["id"],
        "work_id": post["child_work_id"],
        "stage": post["stage"],
        "content": content,
        "truncated": truncated,
        "created_at": post["created_at"],
    }


async def _list_round_page(
    db: AsyncSession,
    *,
    subagent_id: str,
    before_round_id: int | None = None,
) -> dict[str, Any]:
    raw_rounds = await subagent_crud.list_board_rounds(
        db,
        subagent_id=subagent_id,
        before_id=before_round_id,
        limit=SUBAGENT_BOARD_ROUND_PAGE_SIZE + 1,
    )
    page_rounds = raw_rounds[:SUBAGENT_BOARD_ROUND_PAGE_SIZE]
    next_before_round_id = page_rounds[-1][0] if len(raw_rounds) > SUBAGENT_BOARD_ROUND_PAGE_SIZE else None
    return {
        "rounds": [{"work_id": round_id, "created_at": created_at} for round_id, created_at in page_rounds],
        "next_before_round_id": next_before_round_id,
    }


async def _list_post_page(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    subagent_id: str,
    child_work_id: int,
    before_post_id: int | None = None,
) -> dict[str, Any]:
    raw_posts = await subagent_crud.list_board_posts_page(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        subagent_id=subagent_id,
        child_work_id=child_work_id,
        before_id=before_post_id,
        limit=SUBAGENT_BOARD_POST_PAGE_SIZE + 1,
    )
    page_posts = raw_posts[:SUBAGENT_BOARD_POST_PAGE_SIZE]
    next_before_post_id = page_posts[-1]["id"] if len(raw_posts) > SUBAGENT_BOARD_POST_PAGE_SIZE else None
    return {
        "posts": [_build_post(post) for post in reversed(page_posts) if not _is_hidden_post(post)],
        "next_before_post_id": next_before_post_id,
    }


def _build_entry(row: RowMapping, *, now: float) -> dict[str, Any]:
    completed = row["completed"]
    work_status = row["work_status"].value
    result_status = row["result_status"]
    if completed and result_status:
        status = result_status
    elif completed and work_status == SessionReplyWorkStatus.SUCCEEDED.value and row["result_message_id"] is None and row["skip_reply"] is not True:
        status = "execution_unknown"
    elif not completed and row["waiting_confirmation"]:
        status = None
    elif not completed and work_status == SessionReplyWorkStatus.RUNNING.value and (row["lock_until"] is None or row["lock_until"] <= now):
        status = "execution_unknown"
    else:
        status = work_status
    if status == "ended_without_reply":
        status = "succeeded"

    post_stage = row["post_stage"]
    post_preview = row["post_preview"]
    is_result_reference = _is_query_result_prompt(post_preview)
    return {
        "name": row["name"],
        "subagent_id": row["subagent_id"],
        "work_id": row["work_id"],
        "status": status,
        "completed": completed,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stage": None if is_result_reference or post_stage in _FINAL_STAGES else post_stage,
        "preview": "" if is_result_reference or post_preview is None else post_preview,
        "latest_post_id": row["latest_post_id"],
    }


async def list_session_subagents(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    recent_limit: int = 10,
    active_page: int = 1,
    selected_subagent_id: str | None = None,
) -> dict[str, Any]:
    now = time()
    active_total = await subagent_crud.count_active_for_board(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
    )
    last_active_page = max(1, (active_total + SUBAGENT_BOARD_ACTIVE_PAGE_SIZE - 1) // SUBAGENT_BOARD_ACTIVE_PAGE_SIZE)
    active_page = min(max(active_page, 1), last_active_page)
    active_rows = await subagent_crud.list_for_board(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        completed=False,
        offset=(active_page - 1) * SUBAGENT_BOARD_ACTIVE_PAGE_SIZE,
        limit=SUBAGENT_BOARD_ACTIVE_PAGE_SIZE,
    )
    completed_rows = await subagent_crud.list_for_board(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        completed=True,
        limit=recent_limit + 1,
    )

    active = [_build_entry(row, now=now) for row in active_rows]
    recent_candidates = [_build_entry(row, now=now) for row in completed_rows]
    has_more_recent = len(recent_candidates) > recent_limit
    recent = recent_candidates[:recent_limit]
    selected = None
    if selected_subagent_id is not None:
        selected = next((entry for entry in active + recent if entry["subagent_id"] == selected_subagent_id), None)
    if selected_subagent_id is not None and selected is None:
        selected_rows = await subagent_crud.list_for_board(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            completed=None,
            limit=1,
            subagent_id=selected_subagent_id,
        )
        selected = next((_build_entry(row, now=now) for row in selected_rows), None)
    return {
        "active": active,
        "recent": recent,
        "active_page": active_page,
        "active_page_size": SUBAGENT_BOARD_ACTIVE_PAGE_SIZE,
        "active_total": active_total,
        "has_more_recent": has_more_recent,
        "profile_id": profile_id,
        "selected": selected,
    }


async def get_session_subagent_detail(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    subagent_id: str,
    work_id: int | None = None,
    before_post_id: int | None = None,
    before_round_id: int | None = None,
) -> dict[str, Any]:
    subagent = await subagent_crud.get_scoped(
        db,
        subagent_id=subagent_id,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        touch=False,
        lock=False,
    )
    if subagent is None:
        raise ParameterException(ERR_SUBAGENT_NOT_FOUND)

    if before_round_id is not None:
        return await _list_round_page(db, subagent_id=subagent.id, before_round_id=before_round_id)

    selected_work_id = work_id if work_id is not None else subagent.latest_child_work_id
    round_row = await subagent_crud.get_round(db, child_work_id=selected_work_id, subagent_id=subagent.id)
    if round_row is None:
        raise ParameterException(ERR_SUBAGENT_WORK_NOT_FOUND)

    posts_page = await _list_post_page(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        subagent_id=subagent.id,
        child_work_id=round_row.child_work_id,
        before_post_id=before_post_id,
    )
    if before_post_id is not None:
        return {
            "work_id": round_row.child_work_id,
            **posts_page,
        }

    snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
    rounds_page = await _list_round_page(db, subagent_id=subagent.id)
    result = snapshot.get("result")
    result_text, result_text_truncated = _truncate_text(
        result.get("content") if isinstance(result, dict) else None,
        limit=SUBAGENT_BOARD_REPORT_DISPLAY_CHARS,
    )
    reason, reason_truncated = _truncate_text(snapshot.get("error"), limit=SUBAGENT_BOARD_REPORT_DISPLAY_CHARS)
    ended_without_reply = isinstance(result, dict) and result.get("skip_reply") is True
    return {
        "subagent_id": subagent.id,
        "name": subagent.name,
        "work_id": round_row.child_work_id,
        "status": None if snapshot["status"] == "waiting_confirmation" else snapshot["status"],
        "completed": snapshot["completed"],
        "rounds": rounds_page["rounds"],
        "next_before_round_id": rounds_page["next_before_round_id"],
        **posts_page,
        "result_text": result_text,
        "result_text_truncated": result_text_truncated,
        "reason": reason,
        "reason_truncated": reason_truncated,
        "ended_without_reply": ended_without_reply,
    }
