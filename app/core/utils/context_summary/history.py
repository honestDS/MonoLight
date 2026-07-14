from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.message import message_crud
from app.core.utils.context_messages import message_token_text
from app.core.utils.context_summary.common import join_messages, serialize_message
from app.core.utils.context_summary.pipeline import SummaryFragmentInput
from app.core.utils.context_summary.snapshot import CONTEXT_SUMMARY_SCAN_PAGE_SIZE, ContextSummarySnapshot, iter_persistent_summary_rounds
from app.core.utils.context_summary.split import SummarySourceUnit, iter_round_source_units
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens


async def measure_snapshot_history(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    use_request_token_text: bool = False,
) -> tuple[int, int]:
    token_text = message_token_text if use_request_token_text else serialize_message
    history_tokens = sum(estimate_tokens(token_text(message)) for message in snapshot.recent_messages)
    history_message_count = len(snapshot.recent_messages)
    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        history_tokens += sum(estimate_tokens(token_text(message)) for message in round_messages)
        history_message_count += len(round_messages)
    return history_tokens, history_message_count


async def measure_complete_replacement_input(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    existing_summary: str | None,
    page_size: int = CONTEXT_SUMMARY_SCAN_PAGE_SIZE,
) -> int:
    total_tokens = estimate_tokens(existing_summary or "")
    target_id = snapshot.persistent_summary_target_id
    if target_id is None:
        return total_tokens

    page_after_id = snapshot.expected_summary_message_id
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

        total_tokens += sum(estimate_tokens(message_token_text(message)) for message in parse_db_messages_to_internal(page))
        if len(page) < page_size:
            break
        last_id = page[-1].id
        if last_id is None:
            break
        page_after_id = last_id

    return total_tokens


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
    expected_id = snapshot.expected_summary_message_id or 0
    target_id = snapshot.persistent_summary_target_id or 0
    excluded_ids = tuple(sorted(message_id for message_id in snapshot.model_excluded_message_ids if expected_id < message_id <= target_id))
    covered_through_id = expected_id
    extended_source_range: tuple[int, int, int] | None = None

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
            source_units = (
                SummarySourceUnit(
                    message_start_id=start_id,
                    message_end_id=end_id,
                    token_count=round_tokens,
                    content=round_content,
                ),
            )
        else:
            source_units = iter_round_source_units(
                round_messages,
                max_unit_tokens=max_unit_tokens,
            )

        for unit in source_units:
            original_range = (unit.message_start_id, unit.message_end_id)
            if extended_source_range is not None and original_range == extended_source_range[:2]:
                coverage_start_id = extended_source_range[2]
            else:
                excluded_before_unit = tuple(message_id for message_id in excluded_ids if covered_through_id < message_id < unit.message_start_id)
                coverage_start_id = min(excluded_before_unit, default=unit.message_start_id)
                extended_source_range = (
                    unit.message_start_id,
                    unit.message_end_id,
                    coverage_start_id,
                )
            yield SummarySourceUnit(
                message_start_id=coverage_start_id,
                message_end_id=unit.message_end_id,
                token_count=unit.token_count,
                content=unit.content,
            )
            covered_through_id = max(covered_through_id, unit.message_end_id)


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
