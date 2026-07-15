from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from app.core.constants import (
    ERR_CONTEXT_SUMMARY_LOWER_FRAGMENT_RANGE_INVALID,
    ERR_CONTEXT_SUMMARY_LOWER_FRAGMENT_RANGES_OVERLAP,
    ERR_CONTEXT_SUMMARY_LOWER_FRAGMENT_SET_INCOMPLETE,
    ERR_CONTEXT_SUMMARY_LOWER_FRAGMENTS_DISCONTINUOUS,
    ERR_CONTEXT_SUMMARY_LOWER_STAGE_CHANGED,
    ERR_CONTEXT_SUMMARY_LOWER_STAGE_INCOMPLETE,
    ERR_CONTEXT_SUMMARY_MERGE_GROUP_EMPTY,
    ERR_CONTEXT_SUMMARY_MERGE_METADATA_OVER_BUDGET,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.crud.context_summary_stage import (
    CONTEXT_SUMMARY_FRAGMENT_PAGE_SIZE,
    context_summary_stage_crud,
)
from app.core.i18n import t
from app.core.utils.context_summary.pipeline import SummaryFragmentInput
from app.core.utils.tokenizer import estimate_tokens
from app.providers.database import AsyncSessionLocal


@dataclass(frozen=True)
class CompletedSummaryFragment:
    fragment_index: int
    message_start_id: int
    message_end_id: int
    content: str


def _format_completed_fragment(
    fragment: CompletedSummaryFragment,
    *,
    part_index: int | None = None,
    content: str | None = None,
) -> str:
    part_attribute = f' part="{part_index}"' if part_index is not None else ""
    return f'<summary_fragment index="{fragment.fragment_index}" from_message_id="{fragment.message_start_id}" through_message_id="{fragment.message_end_id}"{part_attribute}>\n{fragment.content if content is None else content}\n</summary_fragment>'


def _split_completed_fragment(
    fragment: CompletedSummaryFragment,
    *,
    max_group_tokens: int,
    token_counter: Callable[[str], int],
) -> list[str]:
    whole = _format_completed_fragment(fragment)
    if token_counter(whole) <= max_group_tokens:
        return [whole]

    parts: list[str] = []
    remaining = fragment.content
    part_index = 0
    while remaining:
        low = 1
        high = len(remaining)
        best = 0
        while low <= high:
            middle = (low + high) // 2
            candidate = _format_completed_fragment(
                fragment,
                part_index=part_index,
                content=remaining[:middle],
            )
            if token_counter(candidate) <= max_group_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best <= 0:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MERGE_METADATA_OVER_BUDGET))
        parts.append(
            _format_completed_fragment(
                fragment,
                part_index=part_index,
                content=remaining[:best],
            )
        )
        remaining = remaining[best:]
        part_index += 1
    return parts


async def iter_completed_lower_stage_fragments(
    *,
    work_dedupe_key: str,
    lower_stage_key: str,
    page_size: int = CONTEXT_SUMMARY_FRAGMENT_PAGE_SIZE,
) -> AsyncIterator[CompletedSummaryFragment]:
    page_after_fragment_index: int | None = None
    expected_fragment_index = 0
    expected_fragment_count: int | None = None
    expected_summary_message_id: int | None = None
    persistent_summary_target_id: int | None = None
    previous_message_start_id: int | None = None
    previous_message_end_id: int | None = None
    stage_identity: tuple[object, ...] | None = None

    while True:
        async with AsyncSessionLocal() as page_db:
            page = await context_summary_stage_crud.get_completed_fragment_page(
                page_db,
                work_dedupe_key=work_dedupe_key,
                lower_stage_key=lower_stage_key,
                page_after_fragment_index=page_after_fragment_index,
                limit=page_size,
            )
            if page is None:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_STAGE_INCOMPLETE))

            current_stage_identity = (
                page.stage.uid,
                page.stage.session_id,
                page.stage.work_id,
                page.stage.work_dedupe_key,
                page.stage.snapshot_key,
                page.stage.stage_key,
                page.stage.model_key,
                page.stage.channel_id,
                page.stage.model_id,
                page.stage.expected_summary_message_id,
                page.stage.expected_summary_revision,
                page.stage.snapshot_max_message_id,
                page.stage.persistent_summary_target_id,
                page.stage.expected_fragment_count,
            )
            if stage_identity is None:
                stage_identity = current_stage_identity
                expected_fragment_count = page.stage.expected_fragment_count
                expected_summary_message_id = page.stage.expected_summary_message_id
                persistent_summary_target_id = page.stage.persistent_summary_target_id
            elif current_stage_identity != stage_identity:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_STAGE_CHANGED))

            fragments = tuple(
                CompletedSummaryFragment(
                    fragment_index=fragment.fragment_index,
                    message_start_id=fragment.message_start_id,
                    message_end_id=fragment.message_end_id,
                    content=fragment.content,
                )
                for fragment in page.fragments
            )

        page = None
        if not fragments:
            break

        for fragment in fragments:
            if fragment.fragment_index != expected_fragment_index:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_FRAGMENTS_DISCONTINUOUS))
            repeated_range = fragment.message_start_id == previous_message_start_id and fragment.message_end_id == previous_message_end_id
            if fragment.message_start_id > fragment.message_end_id or (expected_fragment_index == 0 and fragment.message_start_id <= (expected_summary_message_id or 0)) or (previous_message_end_id is not None and fragment.message_start_id <= previous_message_end_id and not repeated_range):
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_FRAGMENT_RANGE_INVALID))
            yield fragment
            expected_fragment_index += 1
            previous_message_start_id = fragment.message_start_id
            previous_message_end_id = fragment.message_end_id

        page_after_fragment_index = fragments[-1].fragment_index
        fragments = ()

    if expected_fragment_count is None or expected_fragment_index != expected_fragment_count or previous_message_end_id != persistent_summary_target_id:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_FRAGMENT_SET_INCOMPLETE))


async def group_completed_summary_fragments(
    fragments: AsyncIterator[CompletedSummaryFragment],
    *,
    max_group_tokens: int,
    first_group_index: int = 0,
    token_counter: Callable[[str], int] = estimate_tokens,
) -> AsyncIterator[SummaryFragmentInput]:
    if max_group_tokens <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_group_tokens"))
    if first_group_index < 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="first_group_index"))

    group_index = 0
    group_parts: list[str] = []
    group_token_floor = 0
    group_start_id: int | None = None
    group_end_id: int | None = None
    previous_fragment_index: int | None = None
    previous_message_start_id: int | None = None
    previous_message_end_id: int | None = None

    def build_group() -> SummaryFragmentInput:
        if group_start_id is None or group_end_id is None:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MERGE_GROUP_EMPTY))
        content = "\n".join(group_parts)
        return SummaryFragmentInput(
            fragment_index=group_index,
            message_start_id=group_start_id,
            message_end_id=group_end_id,
            token_count=max(group_token_floor, max(1, token_counter(content))),
            content=content,
        )

    async for fragment in fragments:
        if previous_fragment_index is not None and fragment.fragment_index != previous_fragment_index + 1:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_FRAGMENTS_DISCONTINUOUS))
        repeated_range = fragment.message_start_id == previous_message_start_id and fragment.message_end_id == previous_message_end_id
        if previous_message_end_id is not None and fragment.message_start_id <= previous_message_end_id and not repeated_range:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_LOWER_FRAGMENT_RANGES_OVERLAP))

        parts = _split_completed_fragment(
            fragment,
            max_group_tokens=max_group_tokens,
            token_counter=token_counter,
        )
        for part in parts:
            part_token_floor = max(1, token_counter(part))
            candidate_content = "\n".join([*group_parts, part])
            candidate_token_floor = group_token_floor + part_token_floor
            candidate_tokens = max(
                candidate_token_floor,
                max(1, token_counter(candidate_content)),
            )
            if group_parts and candidate_tokens > max_group_tokens:
                if group_index >= first_group_index:
                    yield build_group()
                group_index += 1
                group_parts = []
                group_token_floor = 0
                group_start_id = None

            if not group_parts:
                group_start_id = fragment.message_start_id
            group_parts.append(part)
            group_token_floor += part_token_floor
            group_end_id = fragment.message_end_id
        previous_fragment_index = fragment.fragment_index
        previous_message_start_id = fragment.message_start_id
        previous_message_end_id = fragment.message_end_id

    if group_parts and group_index >= first_group_index:
        yield build_group()


async def count_lower_stage_merge_groups(
    *,
    work_dedupe_key: str,
    lower_stage_key: str,
    max_group_tokens: int,
    page_size: int = CONTEXT_SUMMARY_FRAGMENT_PAGE_SIZE,
) -> int:
    group_count = 0
    async for _ in iter_lower_stage_merge_groups(
        work_dedupe_key=work_dedupe_key,
        lower_stage_key=lower_stage_key,
        max_group_tokens=max_group_tokens,
        page_size=page_size,
    ):
        group_count += 1
    return group_count


async def iter_lower_stage_merge_groups(
    *,
    work_dedupe_key: str,
    lower_stage_key: str,
    max_group_tokens: int,
    page_size: int = CONTEXT_SUMMARY_FRAGMENT_PAGE_SIZE,
    first_group_index: int = 0,
) -> AsyncIterator[SummaryFragmentInput]:
    fragments = iter_completed_lower_stage_fragments(
        work_dedupe_key=work_dedupe_key,
        lower_stage_key=lower_stage_key,
        page_size=page_size,
    )
    async for group in group_completed_summary_fragments(
        fragments,
        max_group_tokens=max_group_tokens,
        first_group_index=first_group_index,
    ):
        yield group
