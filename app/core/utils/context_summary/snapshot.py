import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_VALUE_MUST_BE_POSITIVE
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.utils.dispatcher.session_todo_snapshot import strip_session_todo_snapshots
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
    model_excluded_message_ids: tuple[int, ...] = ()
    content_revision: int = 0
    frozen_rounds: tuple[tuple[InternalMessage, ...], ...] | None = None
    source_key: str | None = None

    @property
    def has_persistent_history(self) -> bool:
        expected_id = self.expected_summary_message_id or 0
        return self.persistent_summary_target_id is not None and self.persistent_summary_target_id > expected_id


def build_frozen_context_summary_snapshot(messages: list[InternalMessage], *, work_id: int | None) -> ContextSummarySnapshot:
    if not messages:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="messages"))

    serialized_messages = json.dumps(
        [message.model_dump(mode="json") for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_digest = hashlib.sha256(serialized_messages.encode("utf-8")).hexdigest()
    task_identity = str(work_id) if work_id is not None else uuid.uuid4().hex
    source_key = f"frozen:{task_identity}:{content_digest}"

    copied_messages: list[InternalMessage] = []
    for message_id, message in enumerate(messages, start=1):
        copied_message = message.model_copy(deep=True)
        copied_message.id = message_id
        copied_messages.append(copied_message)

    frozen_rounds: list[tuple[InternalMessage, ...]] = []
    current_round: list[InternalMessage] = []
    for message in copied_messages:
        if current_round and message.role == MessageRole.USER:
            frozen_rounds.append(tuple(current_round))
            current_round = []
        current_round.append(message)
    if current_round:
        frozen_rounds.append(tuple(current_round))

    message_count = len(copied_messages)
    return ContextSummarySnapshot(
        expected_summary_message_id=None,
        snapshot_before_id=None,
        snapshot_max_message_id=message_count,
        persistent_summary_target_id=message_count,
        recent_round_start_ids=(),
        frozen_user_message_ids=(),
        recent_messages=(),
        frozen_rounds=tuple(frozen_rounds),
        source_key=source_key,
    )


async def build_context_summary_snapshot(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    expected_summary_message_id: int | None,
    before_id: int | None,
    frozen_user_message_ids: list[int] | tuple[int, ...] | None = None,
    target_message_id: int | None = None,
    model_excluded_message_ids: list[int] | tuple[int, ...] | None = None,
    page_size: int = CONTEXT_SUMMARY_SCAN_PAGE_SIZE,
    content_revision: int = 0,
) -> ContextSummarySnapshot:
    normalized_frozen_ids = tuple(frozen_user_message_ids or ())
    normalized_excluded_ids = tuple(model_excluded_message_ids or ())
    boundary_candidates = [value for value in (before_id, min(normalized_frozen_ids) if normalized_frozen_ids else None) if value is not None]
    snapshot_before_id = min(boundary_candidates) if boundary_candidates else None
    if target_message_id is not None:
        explicit_before_id = target_message_id + 1
        snapshot_before_id = explicit_before_id if snapshot_before_id is None else min(snapshot_before_id, explicit_before_id)
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
            if target_message_id is not None:
                persistent_summary_target_id = target_message_id
                reached_persistent_history = True
                break
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

    recent_messages = tuple(parse_db_messages_to_internal(list(reversed(recent_raw_desc))))
    return ContextSummarySnapshot(
        expected_summary_message_id=expected_summary_message_id,
        snapshot_before_id=snapshot_before_id,
        snapshot_max_message_id=snapshot_max_message_id,
        persistent_summary_target_id=persistent_summary_target_id,
        recent_round_start_ids=tuple(reversed(recent_user_ids_desc)),
        frozen_user_message_ids=normalized_frozen_ids,
        recent_messages=recent_messages,
        model_excluded_message_ids=normalized_excluded_ids,
        content_revision=content_revision,
    )


async def iter_persistent_summary_rounds(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    page_size: int = CONTEXT_SUMMARY_SCAN_PAGE_SIZE,
) -> AsyncIterator[list[InternalMessage]]:
    if snapshot.frozen_rounds is not None:
        for round_messages in snapshot.frozen_rounds:
            yield list(round_messages)
        return

    if not snapshot.has_persistent_history:
        return

    page_after_id = snapshot.expected_summary_message_id
    current_round: list[Message] = []
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
            if current_round and message.role == MessageRole.USER:
                parsed_round = strip_session_todo_snapshots(parse_db_messages_to_internal([item for item in current_round if item.id not in snapshot.model_excluded_message_ids]))
                if parsed_round:
                    yield parsed_round
                current_round = []
            current_round.append(message)

        if len(page) < page_size:
            break

        last_id = page[-1].id
        if last_id is None:
            break
        page_after_id = last_id

    if current_round:
        parsed_round = strip_session_todo_snapshots(parse_db_messages_to_internal([item for item in current_round if item.id not in snapshot.model_excluded_message_ids]))
        if parsed_round:
            yield parsed_round
