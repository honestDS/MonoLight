from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.message import message_crud
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, Message, MessageRole

CONTEXT_SUMMARY_SCAN_PAGE_SIZE = 200
RECENT_PROTECTED_ROUND_COUNT = 2


@dataclass(frozen=True)
class ContextSummarySnapshot:
    expected_summary_message_id: int | None
    snapshot_before_id: int | None
    snapshot_max_message_id: int | None
    persistent_summary_target_id: int | None
    recent_round_start_ids: tuple[int, ...]
    frozen_user_message_ids: tuple[int, ...]
    recent_messages: tuple[InternalMessage, ...]

    @property
    def has_persistent_history(self) -> bool:
        expected_id = self.expected_summary_message_id or 0
        return self.persistent_summary_target_id is not None and self.persistent_summary_target_id > expected_id


async def build_context_summary_snapshot(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    expected_summary_message_id: int | None,
    before_id: int | None,
    frozen_user_message_ids: list[int] | tuple[int, ...] | None = None,
    page_size: int = CONTEXT_SUMMARY_SCAN_PAGE_SIZE,
) -> ContextSummarySnapshot:
    normalized_frozen_ids = tuple(frozen_user_message_ids or ())
    boundary_candidates = [value for value in (before_id, min(normalized_frozen_ids) if normalized_frozen_ids else None) if value is not None]
    snapshot_before_id = min(boundary_candidates) if boundary_candidates else None
    page_before_id = snapshot_before_id
    snapshot_max_message_id: int | None = None
    recent_raw_desc: list[Message] = []
    recent_user_ids_desc: list[int] = []
    persistent_summary_target_id: int | None = None

    while True:
        page = await message_crud.get_history_backward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            after_id=expected_summary_message_id,
            before_id=snapshot_before_id,
            page_before_id=page_before_id,
            limit=page_size,
        )
        if not page:
            break

        if snapshot_max_message_id is None:
            snapshot_max_message_id = page[0].id

        reached_persistent_history = False
        for message in page:
            if len(recent_user_ids_desc) >= RECENT_PROTECTED_ROUND_COUNT:
                persistent_summary_target_id = message.id
                reached_persistent_history = True
                break

            recent_raw_desc.append(message)
            if message.role == MessageRole.USER and message.id is not None:
                recent_user_ids_desc.append(message.id)

        if reached_persistent_history or len(page) < page_size:
            break

        last_id = page[-1].id
        if last_id is None:
            break
        page_before_id = last_id

    recent_messages = tuple(
        parse_db_messages_to_internal(
            list(reversed(recent_raw_desc)),
        )
    )
    return ContextSummarySnapshot(
        expected_summary_message_id=expected_summary_message_id,
        snapshot_before_id=snapshot_before_id,
        snapshot_max_message_id=snapshot_max_message_id,
        persistent_summary_target_id=persistent_summary_target_id,
        recent_round_start_ids=tuple(reversed(recent_user_ids_desc)),
        frozen_user_message_ids=normalized_frozen_ids,
        recent_messages=recent_messages,
    )


async def iter_persistent_summary_rounds(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    page_size: int = CONTEXT_SUMMARY_SCAN_PAGE_SIZE,
) -> AsyncIterator[list[InternalMessage]]:
    if not snapshot.has_persistent_history:
        return

    page_after_id = snapshot.expected_summary_message_id
    current_round: list[Message] = []
    started_with_user = False
    target_id = snapshot.persistent_summary_target_id
    if target_id is None:
        return
    fixed_before_id = target_id + 1

    while True:
        page = await message_crud.get_history_forward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            after_id=snapshot.expected_summary_message_id,
            before_id=fixed_before_id,
            page_after_id=page_after_id,
            limit=page_size,
        )
        if not page:
            break

        for message in page:
            if not current_round:
                if message.role != MessageRole.USER:
                    return
                started_with_user = True
            elif message.role == MessageRole.USER:
                parsed_round = parse_db_messages_to_internal(current_round)
                if not parsed_round or parsed_round[0].role != MessageRole.USER:
                    return
                yield parsed_round
                current_round = []
            current_round.append(message)

        if len(page) < page_size:
            break

        last_id = page[-1].id
        if last_id is None:
            break
        page_after_id = last_id

    if current_round and started_with_user:
        parsed_round = parse_db_messages_to_internal(current_round)
        if parsed_round and parsed_round[0].role == MessageRole.USER:
            yield parsed_round
