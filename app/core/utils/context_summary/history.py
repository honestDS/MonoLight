from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.context_summary.common import join_messages, serialize_message
from app.core.utils.context_summary.pipeline import SummaryFragmentInput
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot, iter_persistent_summary_rounds
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage


async def measure_snapshot_history(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
) -> tuple[int, int]:
    history_tokens = sum(estimate_tokens(serialize_message(message)) for message in snapshot.recent_messages)
    history_message_count = len(snapshot.recent_messages)
    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        history_tokens += sum(estimate_tokens(serialize_message(message)) for message in round_messages)
        history_message_count += len(round_messages)
    return history_tokens, history_message_count


async def measure_persistent_history(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
) -> tuple[int, int]:
    total_tokens = 0
    message_count = 0
    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        total_tokens += sum(estimate_tokens(serialize_message(message)) for message in round_messages)
        message_count += len(round_messages)
    return total_tokens, message_count


async def count_summary_fragments(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    fragment_target_tokens: int,
    max_fragment_tokens: int,
) -> int:
    fragment_count = 0
    pending_tokens = 0
    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        round_tokens = sum(estimate_tokens(serialize_message(message)) for message in round_messages)
        if round_tokens > max_fragment_tokens:
            return 0
        if pending_tokens and pending_tokens + round_tokens > fragment_target_tokens:
            fragment_count += 1
            pending_tokens = 0
        pending_tokens += round_tokens
    if pending_tokens:
        fragment_count += 1
    return fragment_count


async def iter_summary_fragments(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    existing_summary: str | None,
    fragment_target_tokens: int,
    first_fragment_index: int = 0,
) -> AsyncIterator[SummaryFragmentInput]:
    fragment_index = 0
    pending_messages: list[InternalMessage] = []
    pending_tokens = 0

    def build_fragment() -> SummaryFragmentInput:
        start_id = pending_messages[0].id
        end_id = pending_messages[-1].id
        if start_id is None or end_id is None:
            raise RuntimeError("Context summary fragment messages must have database IDs")
        return SummaryFragmentInput(
            fragment_index=fragment_index,
            message_start_id=start_id,
            message_end_id=end_id,
            token_count=pending_tokens,
            content=join_messages(pending_messages),
            existing_summary=existing_summary if fragment_index == 0 else None,
        )

    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        round_tokens = sum(estimate_tokens(serialize_message(message)) for message in round_messages)
        if pending_messages and pending_tokens + round_tokens > fragment_target_tokens:
            if fragment_index >= first_fragment_index:
                yield build_fragment()
            fragment_index += 1
            pending_messages = []
            pending_tokens = 0
        pending_messages.extend(round_messages)
        pending_tokens += round_tokens

    if pending_messages and fragment_index >= first_fragment_index:
        yield build_fragment()
