from types import SimpleNamespace

import pytest

from app.core.utils.context_summary import merge as merge_module
from app.core.utils.context_summary.merge import (
    CompletedSummaryFragment,
    count_lower_stage_merge_groups,
    group_completed_summary_fragments,
    iter_completed_lower_stage_fragments,
)


def _stage(
    *,
    stage_key: str = "lower-stage",
    model_key: str = "model-key",
    expected_fragment_count: int = 4,
):
    return SimpleNamespace(
        uid="user-1",
        session_id="session-1",
        work_id=1,
        work_dedupe_key="work-key",
        snapshot_key="snapshot-key",
        stage_key=stage_key,
        model_key=model_key,
        channel_id=10,
        model_id="summary-model",
        expected_summary_message_id=None,
        expected_summary_revision=0,
        snapshot_max_message_id=expected_fragment_count * 10 + 20,
        expected_fragment_count=expected_fragment_count,
        persistent_summary_target_id=expected_fragment_count * 10,
    )


def _fragment(fragment_index: int) -> SimpleNamespace:
    return SimpleNamespace(
        fragment_index=fragment_index,
        message_start_id=fragment_index * 10 + 1,
        message_end_id=fragment_index * 10 + 10,
        content=f"summary-{fragment_index}",
    )


def _completed_fragment(
    fragment_index: int,
    *,
    message_start_id: int | None = None,
    message_end_id: int | None = None,
) -> CompletedSummaryFragment:
    return CompletedSummaryFragment(
        fragment_index=fragment_index,
        message_start_id=message_start_id or fragment_index * 10 + 1,
        message_end_id=message_end_id or fragment_index * 10 + 10,
        content=f"summary-{fragment_index}",
    )


async def _iterate(items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_completed_lower_stage_iterator_pages_by_message_id_and_releases_each_session(
    monkeypatch,
):
    page_calls = []
    exited_sessions = []

    class SessionContext:
        def __init__(self):
            self.session = object()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, traceback):
            exited_sessions.append(self.session)
            return False

    async def get_page(
        db,
        *,
        work_dedupe_key,
        lower_stage_key,
        page_after_message_id,
        limit,
    ):
        page_calls.append(
            (
                db,
                work_dedupe_key,
                lower_stage_key,
                page_after_message_id,
                limit,
            )
        )
        pages = {
            None: (_fragment(0), _fragment(1)),
            20: (_fragment(2), _fragment(3)),
            40: (),
        }
        return SimpleNamespace(
            stage=_stage(),
            fragments=pages[page_after_message_id],
        )

    monkeypatch.setattr(merge_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        merge_module.context_summary_stage_crud,
        "get_completed_fragment_page",
        get_page,
    )

    iterator = iter_completed_lower_stage_fragments(
        work_dedupe_key="work-key",
        lower_stage_key="lower-stage",
        page_size=2,
    )
    first = await anext(iterator)
    assert first.fragment_index == 0
    assert len(exited_sessions) == 1

    remaining = [fragment async for fragment in iterator]

    assert [fragment.fragment_index for fragment in remaining] == [1, 2, 3]
    assert [call[3] for call in page_calls] == [None, 20, 40]
    assert all(call[1:3] == ("work-key", "lower-stage") for call in page_calls)
    assert all(call[4] == 2 for call in page_calls)
    assert len(exited_sessions) == 3


@pytest.mark.asyncio
async def test_completed_lower_stage_iterator_rejects_uncompleted_stage(
    monkeypatch,
):
    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_page(*_args, **_kwargs):
        return None

    monkeypatch.setattr(merge_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        merge_module.context_summary_stage_crud,
        "get_completed_fragment_page",
        get_page,
    )

    with pytest.raises(RuntimeError, match="not completed"):
        await anext(
            iter_completed_lower_stage_fragments(
                work_dedupe_key="work-key",
                lower_stage_key="lower-stage",
            )
        )


@pytest.mark.asyncio
async def test_completed_lower_stage_iterator_rejects_identity_change_between_pages(
    monkeypatch,
):
    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_page(
        _db,
        *,
        page_after_message_id,
        **_kwargs,
    ):
        if page_after_message_id is None:
            return SimpleNamespace(
                stage=_stage(),
                fragments=(_fragment(0), _fragment(1)),
            )
        return SimpleNamespace(
            stage=_stage(model_key="changed-model"),
            fragments=(_fragment(2), _fragment(3)),
        )

    monkeypatch.setattr(merge_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        merge_module.context_summary_stage_crud,
        "get_completed_fragment_page",
        get_page,
    )

    with pytest.raises(RuntimeError, match="changed during pagination"):
        _ = [
            fragment
            async for fragment in iter_completed_lower_stage_fragments(
                work_dedupe_key="work-key",
                lower_stage_key="lower-stage",
                page_size=2,
            )
        ]


@pytest.mark.asyncio
async def test_completed_lower_stage_iterator_rejects_incomplete_fragment_sequence(
    monkeypatch,
):
    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_page(
        _db,
        *,
        page_after_message_id,
        **_kwargs,
    ):
        fragments = (_fragment(0), _fragment(2)) if page_after_message_id is None else ()
        return SimpleNamespace(
            stage=_stage(expected_fragment_count=3),
            fragments=fragments,
        )

    monkeypatch.setattr(merge_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        merge_module.context_summary_stage_crud,
        "get_completed_fragment_page",
        get_page,
    )

    with pytest.raises(RuntimeError, match="not continuous"):
        _ = [
            fragment
            async for fragment in iter_completed_lower_stage_fragments(
                work_dedupe_key="work-key",
                lower_stage_key="lower-stage",
            )
        ]


@pytest.mark.asyncio
async def test_merge_groups_follow_budget_and_keep_message_ranges():
    fragments = [_completed_fragment(index) for index in range(5)]

    def count_tokens(content: str) -> int:
        return content.count("<summary_fragment") * 4

    groups = [
        group
        async for group in group_completed_summary_fragments(
            _iterate(fragments),
            max_group_tokens=8,
            token_counter=count_tokens,
        )
    ]

    assert [group.fragment_index for group in groups] == [0, 1, 2]
    assert [(group.message_start_id, group.message_end_id) for group in groups] == [(1, 20), (21, 40), (41, 50)]
    assert [group.token_count for group in groups] == [8, 8, 4]
    assert [group.content.count("<summary_fragment") for group in groups] == [
        2,
        2,
        1,
    ]


@pytest.mark.asyncio
async def test_merge_groups_resume_without_yielding_completed_prefix():
    fragments = [_completed_fragment(index) for index in range(5)]

    def count_tokens(content: str) -> int:
        return content.count("<summary_fragment") * 4

    groups = [
        group
        async for group in group_completed_summary_fragments(
            _iterate(fragments),
            max_group_tokens=8,
            first_group_index=1,
            token_counter=count_tokens,
        )
    ]

    assert [group.fragment_index for group in groups] == [1, 2]
    assert [(group.message_start_id, group.message_end_id) for group in groups] == [(21, 40), (41, 50)]


@pytest.mark.asyncio
async def test_merge_group_count_reuses_paginated_group_stream(
    monkeypatch,
):
    calls = []

    async def iter_groups(**kwargs):
        calls.append(kwargs)
        for index in range(3):
            yield SimpleNamespace(fragment_index=index)

    monkeypatch.setattr(
        merge_module,
        "iter_lower_stage_merge_groups",
        iter_groups,
    )

    count = await count_lower_stage_merge_groups(
        work_dedupe_key="work-key",
        lower_stage_key="lower-stage",
        max_group_tokens=40,
        page_size=2,
    )

    assert count == 3
    assert calls == [
        {
            "work_dedupe_key": "work-key",
            "lower_stage_key": "lower-stage",
            "max_group_tokens": 40,
            "page_size": 2,
        }
    ]


@pytest.mark.asyncio
async def test_merge_groups_reject_fragment_larger_than_budget():
    with pytest.raises(RuntimeError, match="exceeds the merge budget"):
        _ = [
            group
            async for group in group_completed_summary_fragments(
                _iterate([_completed_fragment(0)]),
                max_group_tokens=3,
                token_counter=lambda _content: 4,
            )
        ]


@pytest.mark.asyncio
async def test_merge_groups_reject_overlapping_ranges():
    fragments = [
        _completed_fragment(
            0,
            message_start_id=1,
            message_end_id=10,
        ),
        _completed_fragment(
            1,
            message_start_id=10,
            message_end_id=20,
        ),
    ]

    with pytest.raises(RuntimeError, match="ranges overlap"):
        _ = [
            group
            async for group in group_completed_summary_fragments(
                _iterate(fragments),
                max_group_tokens=100,
                token_counter=lambda _content: 1,
            )
        ]
