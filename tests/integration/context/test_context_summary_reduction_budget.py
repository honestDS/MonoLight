from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.crud.context_summary.fragment import context_summary_fragment_crud
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.utils.context_summary import merge as context_summary_merge
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.model_call import estimate_context_summary_prompt_tokens
from app.core.utils.context_summary.pipeline import SummaryFragmentInput, SummaryFragmentResult
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.tokenizer import estimate_tokens
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole
from app.models.profile import ProfileConfig
from app.models.session import ChatSession
from app.providers.llm.client import LLMClient
from tests.integration.context.context_summary_workflow_support import _context_summary_database as _context_summary_database
from tests.integration.context.context_summary_workflow_support import (
    _seed_summary_history,
    _summary_rejection_models,
)
from tests.integration.context_summary_test_support import make_fragment, make_stage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_shape",
    ["single-fragment", "grouped-fragments"],
    ids=["single-fragment", "grouped-fragments"],
)
@pytest.mark.parametrize(
    ("model_id", "protocol"),
    [
        pytest.param("gemini-3.8-flash-low", "openai", id="gemini-openai"),
        pytest.param("gpt-5.6-luna", "openai_responses", id="gpt-responses"),
    ],
)
async def test_context_summary_reduction_uses_complete_request_budget_and_reuses_plan(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    source_shape: str,
    model_id: str,
    protocol: str,
) -> None:
    await _seed_summary_history(context_summary_database)

    work_dedupe_key = f"merge-budget-{source_shape}"
    lower_stage_key = f"merge-budget-lower-{source_shape}"
    lower_model_key = "large-window-lower-model"
    escaped_row = 'row\n"C:\\test\\file"\t'

    def _lower_content(marker: str, repeats: int) -> str:
        return json.dumps(
            {"nested": {"marker": marker, "payload": escaped_row * repeats}},
            ensure_ascii=False,
        )

    if source_shape == "single-fragment":
        lower_ranges = [(1, 3), (4, 4)]
        lower_contents = [
            _lower_content("single-long-marker", 900),
            _lower_content("single-tail-marker", 2),
        ]
    else:
        assert source_shape == "grouped-fragments"
        lower_ranges = [(message_id, message_id) for message_id in range(1, 5)]
        lower_contents = [_lower_content(f"grouped-marker-{message_id}", 100) for message_id in range(1, 5)]

    lower_stage = make_stage(
        work_dedupe_key=work_dedupe_key,
        stage_key=lower_stage_key,
        model_key=lower_model_key,
        expected_fragment_count=len(lower_contents),
        persistent_summary_target_id=4,
    )
    lower_stage.uid = "summary-user"
    lower_stage.session_id = "summary-session"
    lower_stage.snapshot_max_message_id = 4
    lower_stage.model_id = "large-window-lower-stage"
    lower_stage.context_window_k = 128
    lower_stage.max_output_tokens = 20_480

    async with context_summary_database() as db:
        persisted_lower_stage, stage_created = await context_summary_stage_crud.create_stage(
            db,
            stage=lower_stage,
        )
        assert stage_created is True
        for fragment_index, (message_range, content) in enumerate(zip(lower_ranges, lower_contents)):
            message_start_id, message_end_id = message_range
            fragment = make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key=work_dedupe_key,
                stage_key=persisted_lower_stage.stage_key,
                model_key=lower_model_key,
                message_start_id=message_start_id,
                message_end_id=message_end_id,
            )
            fragment.uid = persisted_lower_stage.uid
            fragment.session_id = persisted_lower_stage.session_id
            fragment.model_id = persisted_lower_stage.model_id
            fragment.content = content
            fragment.token_count = max(1, estimate_tokens(content))
            persisted_fragment, fragment_created = await context_summary_fragment_crud.write_ordered(
                db,
                fragment=fragment,
            )
            assert persisted_fragment is not None
            assert fragment_created is True
        await db.refresh(persisted_lower_stage)
        assert await context_summary_stage_crud.mark_completed(
            db,
            work_dedupe_key=work_dedupe_key,
            stage_key=persisted_lower_stage.stage_key,
            model_key=persisted_lower_stage.model_key,
        )

    async with context_summary_database() as db:
        lower_stage = (
            await db.execute(
                select(ContextSummaryStage).where(
                    ContextSummaryStage.work_dedupe_key == work_dedupe_key,
                    ContextSummaryStage.stage_key == lower_stage_key,
                )
            )
        ).scalar_one()
        lower_fragments = list(
            (
                await db.execute(
                    select(ContextSummaryFragment)
                    .where(
                        ContextSummaryFragment.work_dedupe_key == work_dedupe_key,
                        ContextSummaryFragment.stage_key == lower_stage_key,
                    )
                    .order_by(ContextSummaryFragment.fragment_index)
                )
            ).scalars()
        )

    lower_stage_before = lower_stage.model_dump()
    lower_fragments_before = [fragment.model_dump() for fragment in lower_fragments]
    summary_model = ContextSummaryModelSnapshot(
        channel_id=11,
        channel_name="merge-budget-summary",
        model_id=model_id,
        protocol=protocol,
        base_url="https://summary.invalid",
        api_key="secret",
        priority=1,
        context_window_tokens=4_096,
        max_output_tokens=512,
        temperature=0.0,
        top_p=None,
        safety_margin_tokens=0,
        input_budget_tokens=3_584,
    )

    empty_merge_prompt = context_summary_reduction.merge_fragment_prompt(
        SummaryFragmentInput(
            fragment_index=0,
            message_start_id=lower_stage.expected_summary_message_id or 1,
            message_end_id=lower_stage.persistent_summary_target_id,
            token_count=1,
            content=json.dumps({"summary_fragments": []}, ensure_ascii=False, separators=(",", ":")),
        )
    )
    max_group_tokens = (
        summary_model.input_budget_tokens
        - estimate_context_summary_prompt_tokens(
            summary_model,
            empty_merge_prompt,
        )
        - 32
    )
    assert max_group_tokens > 0

    candidate_groups = [
        group
        async for group in context_summary_merge.iter_lower_stage_merge_groups(
            work_dedupe_key=work_dedupe_key,
            lower_stage_key=lower_stage.stage_key,
            max_group_tokens=max_group_tokens,
            token_counter=lambda text: estimate_tokens(
                text,
                model_id=summary_model.model_id,
                protocol=summary_model.protocol,
            ),
        )
    ]
    assert candidate_groups
    assert all(0 < group.token_count <= max_group_tokens for group in candidate_groups)
    assert all(json.loads(group.content)["summary_fragments"] for group in candidate_groups)

    model_prompts: list[str] = []

    async def _call_model(*, model: ContextSummaryModelSnapshot, prompt: str) -> str:
        assert model == summary_model
        prompt_tokens = estimate_context_summary_prompt_tokens(model, prompt)
        assert prompt_tokens <= model.input_budget_tokens
        model_prompts.append(prompt)
        return "summary"

    monkeypatch.setattr(context_summary_stage, "call_context_summary_model", _call_model)

    first_stage = await context_summary_reduction.execute_reduction_stage(
        lower_stage=lower_stage,
        model=summary_model,
        refinement_index=0,
        concurrency=4,
    )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryStage.id))).scalars())
        fragments = list((await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryFragment.stage_key, ContextSummaryFragment.fragment_index))).scalars())

    reduction_stages = [stage for stage in stages if stage.lower_stage_key == lower_stage.stage_key]
    assert len(reduction_stages) == 1
    completed_stage = reduction_stages[0]
    assert completed_stage.status == ContextSummaryStageStatus.COMPLETED
    assert completed_stage.expected_fragment_count == completed_stage.succeeded_fragment_count
    reduction_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == completed_stage.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    assert len(reduction_fragments) == completed_stage.succeeded_fragment_count == len(model_prompts)
    assert [fragment.fragment_index for fragment in reduction_fragments] == list(range(len(reduction_fragments)))
    assert all(1 <= fragment.message_start_id <= fragment.message_end_id <= 4 for fragment in reduction_fragments)
    assert reduction_fragments[0].message_start_id == 1
    assert reduction_fragments[-1].message_end_id == 4
    assert {message_id for fragment in reduction_fragments for message_id in range(fragment.message_start_id, fragment.message_end_id + 1)} == {1, 2, 3, 4}
    assert first_stage.stage_key == completed_stage.stage_key
    first_lower_stage = next(stage for stage in stages if stage.stage_key == lower_stage.stage_key)
    first_lower_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == lower_stage.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    assert first_lower_stage.model_dump() == lower_stage_before
    assert [fragment.model_dump() for fragment in first_lower_fragments] == lower_fragments_before

    observed_parts: dict[int, list[tuple[int, str]]] = {}
    observed_order: list[tuple[int, int]] = []
    for prompt in model_prompts:
        for fragment in json.loads(prompt)["summary_fragments"]:
            fragment_index = fragment["index"]
            part_index = fragment.get("part", 0)
            observed_parts.setdefault(fragment_index, []).append((part_index, fragment["content"]))
            observed_order.append((fragment_index, part_index))

    assert set(observed_parts) == set(range(len(lower_contents)))
    for fragment_index, expected_content in enumerate(lower_contents):
        parts = sorted(observed_parts[fragment_index])
        assert [part_index for part_index, _content in parts] == list(range(len(parts)))
        assert "".join(content for _part_index, content in parts) == expected_content
    assert sorted(observed_order) == [(fragment_index, part_index) for fragment_index in range(len(lower_contents)) for part_index in range(len(observed_parts[fragment_index]))]
    if source_shape == "single-fragment":
        assert len(observed_parts[0]) > 1

    prompt_count = len(model_prompts)
    second_stage = await context_summary_reduction.execute_reduction_stage(
        lower_stage=lower_stage,
        model=summary_model,
        refinement_index=0,
        concurrency=4,
    )
    assert len(model_prompts) == prompt_count
    assert second_stage.stage_key == completed_stage.stage_key
    assert second_stage.succeeded_fragment_count == completed_stage.succeeded_fragment_count
    assert second_stage.expected_fragment_count == completed_stage.expected_fragment_count

    async with context_summary_database() as db:
        final_stages = list((await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryStage.id))).scalars())
        final_lower_stage = next(stage for stage in final_stages if stage.stage_key == lower_stage.stage_key)
        final_lower_fragments = list(
            (
                await db.execute(
                    select(ContextSummaryFragment)
                    .where(
                        ContextSummaryFragment.work_dedupe_key == work_dedupe_key,
                        ContextSummaryFragment.stage_key == lower_stage.stage_key,
                    )
                    .order_by(ContextSummaryFragment.fragment_index)
                )
            ).scalars()
        )

    assert len(final_stages) == 2
    assert final_lower_stage.model_dump() == lower_stage_before
    assert [fragment.model_dump() for fragment in final_lower_fragments] == lower_fragments_before


@pytest.mark.asyncio
async def test_context_summary_reduction_different_budgets_do_not_reuse_saved_prefix(
    context_summary_database: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_summary_history(context_summary_database)

    work_dedupe_key = "reduction-budget-isolation"
    lower_stage = make_stage(
        work_dedupe_key=work_dedupe_key,
        stage_key="reduction-budget-lower",
        model_key="reduction-budget-lower-model",
        expected_fragment_count=2,
        persistent_summary_target_id=4,
    )
    lower_stage.uid = "summary-user"
    lower_stage.session_id = "summary-session"
    lower_stage.snapshot_max_message_id = 4

    lower_ranges = [(1, 2), (3, 4)]
    lower_contents = ["lower prefix", "lower suffix"]
    async with context_summary_database() as db:
        persisted_lower_stage, stage_created = await context_summary_stage_crud.create_stage(
            db,
            stage=lower_stage,
        )
        assert stage_created is True
        for fragment_index, (message_range, content) in enumerate(zip(lower_ranges, lower_contents)):
            message_start_id, message_end_id = message_range
            fragment = make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key=work_dedupe_key,
                stage_key=persisted_lower_stage.stage_key,
                model_key=persisted_lower_stage.model_key,
                message_start_id=message_start_id,
                message_end_id=message_end_id,
            )
            fragment.uid = persisted_lower_stage.uid
            fragment.session_id = persisted_lower_stage.session_id
            fragment.model_id = persisted_lower_stage.model_id
            fragment.content = content
            fragment.token_count = max(1, estimate_tokens(content))
            persisted_fragment, fragment_created = await context_summary_fragment_crud.write_ordered(
                db,
                fragment=fragment,
            )
            assert persisted_fragment is not None
            assert fragment_created is True
        await db.refresh(persisted_lower_stage)
        assert await context_summary_stage_crud.mark_completed(
            db,
            work_dedupe_key=work_dedupe_key,
            stage_key=persisted_lower_stage.stage_key,
            model_key=persisted_lower_stage.model_key,
        )

    lower_stage = persisted_lower_stage
    summary_model = replace(
        _summary_rejection_models()[1],
        context_window_tokens=4_096,
        max_output_tokens=512,
        input_budget_tokens=3_584,
    )
    first_stage = await context_summary_reduction.create_reduction_stage(
        lower_stage=lower_stage,
        model=summary_model,
        expected_fragment_count=2,
        refinement_index=0,
        max_group_tokens=2_000,
    )
    await context_summary_stage.write_summary_fragment(
        stage=first_stage,
        result=SummaryFragmentResult(
            fragment_index=0,
            message_start_id=1,
            message_end_id=2,
            content="saved prefix",
            token_count=max(1, estimate_tokens("saved prefix")),
        ),
    )

    second_stage = await context_summary_reduction.create_reduction_stage(
        lower_stage=lower_stage,
        model=summary_model,
        expected_fragment_count=2,
        refinement_index=0,
        max_group_tokens=1_800,
    )
    assert second_stage.stage_key != first_stage.stage_key
    assert second_stage.model_key == first_stage.model_key
    assert second_stage.succeeded_fragment_count == 0
    assert second_stage.status == ContextSummaryStageStatus.RUNNING

    restored_first_stage = await context_summary_reduction.create_reduction_stage(
        lower_stage=lower_stage,
        model=summary_model,
        expected_fragment_count=2,
        refinement_index=0,
        max_group_tokens=2_000,
    )
    restored_second_stage = await context_summary_reduction.create_reduction_stage(
        lower_stage=lower_stage,
        model=summary_model,
        expected_fragment_count=2,
        refinement_index=0,
        max_group_tokens=1_800,
    )
    assert restored_first_stage.stage_key == first_stage.stage_key
    assert restored_first_stage.succeeded_fragment_count == 1
    assert restored_second_stage.stage_key == second_stage.stage_key
    assert restored_second_stage.succeeded_fragment_count == 0

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryStage.id))).scalars())
        fragments = list((await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryFragment.stage_key, ContextSummaryFragment.fragment_index))).scalars())

    lower_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == lower_stage.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    first_fragments = [fragment for fragment in fragments if fragment.stage_key == first_stage.stage_key]
    second_fragments = [fragment for fragment in fragments if fragment.stage_key == second_stage.stage_key]
    assert len(stages) == 3
    assert len([stage for stage in stages if stage.lower_stage_key == lower_stage.stage_key]) == 2
    assert [(fragment.fragment_index, fragment.message_start_id, fragment.message_end_id, fragment.content) for fragment in lower_fragments] == [
        (0, 1, 2, "lower prefix"),
        (1, 3, 4, "lower suffix"),
    ]
    assert len(first_fragments) == 1
    assert first_fragments[0].fragment_index == 0
    assert first_fragments[0].message_start_id == 1
    assert first_fragments[0].message_end_id == 2
    assert first_fragments[0].content == "saved prefix"
    assert first_fragments[0].stage_key == first_stage.stage_key
    assert first_fragments[0].model_key == first_stage.model_key
    assert second_fragments == []
    reduction_stages = {stage.stage_key: stage for stage in stages if stage.lower_stage_key == lower_stage.stage_key}
    assert set(reduction_stages) == {first_stage.stage_key, second_stage.stage_key}
    assert reduction_stages[first_stage.stage_key].succeeded_fragment_count == 1
    assert reduction_stages[second_stage.stage_key].succeeded_fragment_count == 0
    assert reduction_stages[first_stage.stage_key].model_key == reduction_stages[second_stage.stage_key].model_key


async def _seed_summary_concurrency_history(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    source_marker: str,
) -> None:
    def long_content(message_id: int) -> str:
        return "".join(f"{source_marker}-{message_id} " + "fact " * 10 for _ in range(70))

    messages = [
        Message(
            id=message_id,
            session_id="summary-session",
            uid="summary-user",
            profile_id=1,
            role=MessageRole.USER if message_id % 2 else MessageRole.ASSISTANT,
            content=long_content(message_id) if message_id <= 48 else ("tail question" if message_id == 49 else "tail answer"),
        )
        for message_id in range(1, 51)
    ]
    async with session_factory() as db:
        db.add(ChatSession(session_id="summary-session", uid="summary-user"))
        db.add_all(messages)
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_values", "expected_concurrency"),
    [
        pytest.param({}, 4, id="default-4"),
        pytest.param({"context_summary_max_concurrency": 1}, 1, id="configured-1"),
        pytest.param({"context_summary_max_concurrency": 20}, 20, id="configured-20"),
    ],
)
async def test_context_summary_raw_fragments_respect_configured_model_concurrency(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    profile_values: dict[str, int],
    expected_concurrency: int,
) -> None:
    source_marker = f"raw-summary-source-{expected_concurrency}"
    await _seed_summary_concurrency_history(
        context_summary_database,
        source_marker=source_marker,
    )

    summary_model = replace(
        _summary_rejection_models()[1],
        context_window_tokens=2_000,
        max_output_tokens=768,
        input_budget_tokens=1_232,
    )
    cfg = ProfileConfig.model_validate(profile_values)
    profile = SimpleNamespace(id=1)
    raw_release = asyncio.Event()
    raw_active = 0
    raw_max_active = 0
    raw_call_count = 0

    async def select_model(*_args, **_kwargs) -> ContextSummaryModelSnapshot:
        return summary_model

    async def generate_model(**kwargs) -> InternalResponse:
        nonlocal raw_active, raw_call_count, raw_max_active
        prompt = kwargs["messages"][0].content
        assert isinstance(prompt, str)
        if source_marker in prompt:
            raw_call_count += 1
            raw_active += 1
            raw_max_active = max(raw_max_active, raw_active)
            if raw_active >= expected_concurrency:
                raw_release.set()
            try:
                await raw_release.wait()
                await asyncio.sleep(0.01)
            finally:
                raw_active -= 1
        else:
            await asyncio.sleep(0.001)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="short summary"),
            model=kwargs["model_id"],
            usage={"completion_tokens": 1},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    async with context_summary_database() as db:
        snapshot = await build_context_summary_snapshot(
            db,
            session_id="summary-session",
            uid="summary-user",
            expected_summary_message_id=None,
            before_id=51,
            target_message_id=48,
            content_revision=0,
        )
        generated = await asyncio.wait_for(
            context_summary_stage.generate_snapshot_summary_with_model(
                db,
                session_id="summary-session",
                uid="summary-user",
                profile=profile,
                cfg=cfg,
                snapshot=snapshot,
                existing_summary=None,
                existing_summary_revision=0,
                safety_margin_tokens=0,
                model=summary_model,
            ),
            timeout=10,
        )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage).order_by(ContextSummaryStage.id))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment).order_by(ContextSummaryFragment.stage_key, ContextSummaryFragment.fragment_index))).scalars().all())

    raw_stage = next(stage for stage in stages if stage.lower_stage_key is None)
    raw_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == raw_stage.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    assert cfg.other.context_summary_max_concurrency == expected_concurrency
    assert raw_max_active == expected_concurrency
    assert raw_stage.status == ContextSummaryStageStatus.COMPLETED
    assert raw_stage.expected_fragment_count >= 20
    assert raw_stage.expected_fragment_count == raw_stage.succeeded_fragment_count == len(raw_fragments) == raw_call_count
    assert [fragment.fragment_index for fragment in raw_fragments] == list(range(len(raw_fragments)))
    assert raw_fragments[0].message_start_id == 1
    assert raw_fragments[-1].message_end_id == 48
    assert all(1 <= fragment.message_start_id <= fragment.message_end_id <= 48 for fragment in raw_fragments)
    assert all(left.message_start_id <= right.message_start_id and left.message_end_id <= right.message_end_id for left, right in zip(raw_fragments, raw_fragments[1:]))
    assert {message_id for fragment in raw_fragments for message_id in range(fragment.message_start_id, fragment.message_end_id + 1)} == set(range(1, 49))
    assert all(fragment.content == "short summary" and source_marker not in fragment.content for fragment in raw_fragments)
    assert generated.completed_stage is not None
    assert generated.content and source_marker not in generated.content
    assert all(stage.status == ContextSummaryStageStatus.COMPLETED for stage in stages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_values", "expected_concurrency"),
    [
        pytest.param({}, 4, id="default-4"),
        pytest.param({"context_summary_max_concurrency": 1}, 1, id="configured-1"),
        pytest.param({"context_summary_max_concurrency": 20}, 20, id="configured-20"),
    ],
)
async def test_context_summary_reduction_respects_configured_model_concurrency(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    profile_values: dict[str, int],
    expected_concurrency: int,
) -> None:
    source_marker = f"lower-summary-source-{expected_concurrency}"
    await _seed_summary_concurrency_history(
        context_summary_database,
        source_marker=source_marker,
    )

    work_dedupe_key = f"merge-summary-concurrency-{expected_concurrency}"
    lower_stage_key = f"merge-summary-concurrency-lower-{expected_concurrency}"
    lower_model_key = f"merge-summary-concurrency-lower-model-{expected_concurrency}"
    lower_ranges = [(2 * fragment_index + 1, 2 * fragment_index + 2) for fragment_index in range(24)]
    lower_contents = ["".join(f"{source_marker}-{fragment_index} " + "fact " * 10 for _ in range(70)) for fragment_index in range(24)]
    lower_stage = make_stage(
        work_dedupe_key=work_dedupe_key,
        stage_key=lower_stage_key,
        model_key=lower_model_key,
        expected_fragment_count=len(lower_contents),
        persistent_summary_target_id=48,
    )
    lower_stage.uid = "summary-user"
    lower_stage.session_id = "summary-session"
    lower_stage.snapshot_max_message_id = 50

    async with context_summary_database() as db:
        persisted_lower_stage, stage_created = await context_summary_stage_crud.create_stage(
            db,
            stage=lower_stage,
        )
        assert stage_created is True
        for fragment_index, (message_range, content) in enumerate(zip(lower_ranges, lower_contents)):
            message_start_id, message_end_id = message_range
            fragment = make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key=work_dedupe_key,
                stage_key=persisted_lower_stage.stage_key,
                model_key=persisted_lower_stage.model_key,
                message_start_id=message_start_id,
                message_end_id=message_end_id,
            )
            fragment.uid = persisted_lower_stage.uid
            fragment.session_id = persisted_lower_stage.session_id
            fragment.model_id = persisted_lower_stage.model_id
            fragment.content = content
            fragment.token_count = max(1, estimate_tokens(content))
            persisted_fragment, fragment_created = await context_summary_fragment_crud.write_ordered(
                db,
                fragment=fragment,
            )
            assert persisted_fragment is not None
            assert fragment_created is True
        await db.refresh(persisted_lower_stage)
        assert await context_summary_stage_crud.mark_completed(
            db,
            work_dedupe_key=work_dedupe_key,
            stage_key=persisted_lower_stage.stage_key,
            model_key=persisted_lower_stage.model_key,
        )

    async with context_summary_database() as db:
        lower_stage = (await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.work_dedupe_key == work_dedupe_key, ContextSummaryStage.stage_key == lower_stage_key))).scalar_one()
        lower_fragments = list((await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.work_dedupe_key == work_dedupe_key, ContextSummaryFragment.stage_key == lower_stage_key).order_by(ContextSummaryFragment.fragment_index))).scalars())
    lower_stage_before = lower_stage.model_dump()
    lower_fragments_before = [fragment.model_dump() for fragment in lower_fragments]

    summary_model = replace(
        _summary_rejection_models()[1],
        context_window_tokens=2_000,
        max_output_tokens=768,
        input_budget_tokens=1_232,
    )
    cfg = ProfileConfig.model_validate(profile_values)
    profile = SimpleNamespace(id=1)
    merge_release = asyncio.Event()
    merge_active = 0
    merge_max_active = 0
    merge_call_count = 0

    async def select_model(*_args, **_kwargs) -> ContextSummaryModelSnapshot:
        return summary_model

    async def generate_model(**kwargs) -> InternalResponse:
        nonlocal merge_active, merge_call_count, merge_max_active
        prompt = kwargs["messages"][0].content
        assert isinstance(prompt, str)
        if source_marker in prompt:
            merge_call_count += 1
            merge_active += 1
            merge_max_active = max(merge_max_active, merge_active)
            if merge_active >= expected_concurrency:
                merge_release.set()
            try:
                await merge_release.wait()
                await asyncio.sleep(0.01)
            finally:
                merge_active -= 1
        else:
            await asyncio.sleep(0.001)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="short summary"),
            model=kwargs["model_id"],
            usage={"completion_tokens": 1},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    async with context_summary_database() as db:
        result = await asyncio.wait_for(
            context_summary_reduction.reduce_completed_summary_stage_result(
                db,
                profile=profile,
                cfg=cfg,
                initial_stage=lower_stage,
                safety_margin_tokens=0,
            ),
            timeout=10,
        )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage).where(ContextSummaryStage.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryStage.id))).scalars())
        fragments = list((await db.execute(select(ContextSummaryFragment).where(ContextSummaryFragment.work_dedupe_key == work_dedupe_key).order_by(ContextSummaryFragment.stage_key, ContextSummaryFragment.fragment_index))).scalars())

    reduction_stages = [stage for stage in stages if stage.lower_stage_key == lower_stage.stage_key]
    assert len(reduction_stages) == 1
    first_merge_stage = reduction_stages[0]
    first_merge_fragments = [fragment for fragment in fragments if fragment.stage_key == first_merge_stage.stage_key]
    assert merge_max_active == expected_concurrency
    assert merge_call_count == first_merge_stage.expected_fragment_count
    assert first_merge_stage.expected_fragment_count >= 20
    assert first_merge_stage.succeeded_fragment_count == first_merge_stage.expected_fragment_count
    assert first_merge_stage.status == ContextSummaryStageStatus.COMPLETED
    assert len(first_merge_fragments) == first_merge_stage.expected_fragment_count
    assert [fragment.fragment_index for fragment in first_merge_fragments] == list(range(len(first_merge_fragments)))
    assert first_merge_fragments[0].message_start_id == 1
    assert first_merge_fragments[-1].message_end_id == 48
    assert all(1 <= fragment.message_start_id <= fragment.message_end_id <= 48 for fragment in first_merge_fragments)
    assert all(left.message_start_id <= right.message_start_id and left.message_end_id <= right.message_end_id for left, right in zip(first_merge_fragments, first_merge_fragments[1:]))
    assert {message_id for fragment in first_merge_fragments for message_id in range(fragment.message_start_id, fragment.message_end_id + 1)} == set(range(1, 49))
    assert all(fragment.content == "short summary" for fragment in first_merge_fragments)

    assert result.content and source_marker not in result.content
    for stage in stages:
        assert stage.status == ContextSummaryStageStatus.COMPLETED
        stage_fragments = [fragment for fragment in fragments if fragment.stage_key == stage.stage_key]
        assert stage_fragments
        assert all(fragment.content for fragment in stage_fragments)

    final_lower_stage = next(stage for stage in stages if stage.stage_key == lower_stage.stage_key)
    final_lower_fragments = [fragment for fragment in fragments if fragment.stage_key == lower_stage.stage_key]
    assert final_lower_stage.model_dump() == lower_stage_before
    assert [fragment.model_dump() for fragment in final_lower_fragments] == lower_fragments_before
