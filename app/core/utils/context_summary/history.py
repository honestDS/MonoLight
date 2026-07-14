from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.context_summary.common import join_messages, serialize_message
from app.core.utils.context_summary.pipeline import SummaryFragmentInput
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot, iter_persistent_summary_rounds
from app.core.utils.context_summary.split import SummarySourceUnit, iter_round_source_units
from app.core.utils.tokenizer import estimate_tokens


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


async def iter_persistent_summary_source_units(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    max_unit_tokens: int,
) -> AsyncIterator[SummarySourceUnit]:
    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        round_content = join_messages(round_messages)
        serialized_token_floor = sum(max(1, estimate_tokens(serialize_message(message))) for message in round_messages)
        round_tokens = max(
            serialized_token_floor,
            max(1, estimate_tokens(round_content)),
        )
        if round_tokens <= max_unit_tokens:
            start_id = round_messages[0].id
            end_id = round_messages[-1].id
            if start_id is None or end_id is None:
                raise RuntimeError("Context summary round messages must have database IDs")
            yield SummarySourceUnit(
                message_start_id=start_id,
                message_end_id=end_id,
                token_count=round_tokens,
                content=round_content,
            )
            continue

        for unit in iter_round_source_units(
            round_messages,
            max_unit_tokens=max_unit_tokens,
        ):
            yield unit


async def iter_grouped_summary_source_units(
    units: AsyncIterator[SummarySourceUnit],
    *,
    fragment_target_tokens: int,
    first_fragment_index: int = 0,
    existing_summary: str | None = None,
) -> AsyncIterator[SummaryFragmentInput]:
    if fragment_target_tokens <= 0:
        raise ValueError("fragment_target_tokens must be positive")
    if first_fragment_index < 0:
        raise ValueError("first_fragment_index must be non-negative")

    fragment_index = 0
    pending_units: list[SummarySourceUnit] = []
    pending_tokens = 0

    def build_fragment() -> SummaryFragmentInput:
        return SummaryFragmentInput(
            fragment_index=fragment_index,
            message_start_id=pending_units[0].message_start_id,
            message_end_id=pending_units[-1].message_end_id,
            token_count=pending_tokens,
            content="\n".join(unit.content for unit in pending_units),
            existing_summary=existing_summary if fragment_index == 0 else None,
        )

    async for unit in units:
        if pending_units and pending_tokens + unit.token_count > fragment_target_tokens:
            if fragment_index >= first_fragment_index:
                yield build_fragment()
            fragment_index += 1
            pending_units = []
            pending_tokens = 0

        pending_units.append(unit)
        pending_tokens += unit.token_count

    if pending_units and fragment_index >= first_fragment_index:
        yield build_fragment()


async def count_summary_fragments(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    fragment_target_tokens: int,
    max_fragment_tokens: int,
) -> int:
    units = iter_persistent_summary_source_units(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        max_unit_tokens=max_fragment_tokens,
    )
    fragment_count = 0
    async for _fragment in iter_grouped_summary_source_units(
        units,
        fragment_target_tokens=fragment_target_tokens,
    ):
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
    max_fragment_tokens: int,
    first_fragment_index: int = 0,
) -> AsyncIterator[SummaryFragmentInput]:
    units = iter_persistent_summary_source_units(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        max_unit_tokens=max_fragment_tokens,
    )
    async for fragment in iter_grouped_summary_source_units(
        units,
        fragment_target_tokens=fragment_target_tokens,
        first_fragment_index=first_fragment_index,
        existing_summary=existing_summary,
    ):
        yield fragment
