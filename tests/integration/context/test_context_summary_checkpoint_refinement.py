from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.constants import (
    CONTEXT_SUMMARY_MIN_REFINEMENT_REDUCTION_TOKENS,
    ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED,
    FROZEN_CONTEXT_SUMMARY_WORK_KEY_PREFIX,
)
from app.core.crud.context_summary.fragment import context_summary_fragment_crud
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.exceptions import LLMException
from app.core.utils.context_budget import calculate_context_summary_trigger_tokens
from app.core.utils.context_summary import reduction as context_summary_reduction
from app.core.utils.context_summary import stage as context_summary_stage
from app.core.utils.context_summary.boundary import ContextSummaryTriggerMode
from app.core.utils.context_summary.common import ContextSummaryState
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.tokenizer import estimate_tokens
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage, ContextSummaryStageStatus
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole, StoredConversationSummary
from app.models.profile import OtherConfig, ProfileConfig
from app.models.session import ChatSession
from app.providers.llm.client import LLMClient, estimate_request_context_tokens
from tests.integration.context.context_summary_workflow_support import _context_summary_database as _context_summary_database
from tests.integration.context.context_summary_workflow_support import (
    _seed_summary_history,
    _summary_rejection_models,
)
from tests.integration.context_summary_test_support import make_fragment, make_stage


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["session-history", "frozen-task"], ids=["session-history", "frozen-task"])
async def test_context_summary_reduction_splits_repeated_range_and_completes(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    first_content = "first source " + "fact " * 700 + " tail evidence"
    last_content = "last message evidence"
    if mode == "session-history":
        await _seed_summary_history(context_summary_database)
        uid, session_id = "summary-user", "summary-session"
        work_dedupe_key = "repeated-range-summary-work"
    else:
        uid, session_id = "frozen-user", "frozen-session"
        work_dedupe_key = f"{FROZEN_CONTEXT_SUMMARY_WORK_KEY_PREFIX}repeated-range-summary"

    lower_stage = make_stage(
        work_dedupe_key=work_dedupe_key,
        stage_key="repeated-range-source-stage",
        model_key="repeated-range-source-model",
        expected_fragment_count=2,
        persistent_summary_target_id=4,
    )
    lower_stage.uid = uid
    lower_stage.session_id = session_id
    lower_stage.snapshot_max_message_id = 4

    async with context_summary_database() as db:
        persisted_stage, created = await context_summary_stage_crud.create_stage(db, stage=lower_stage)
        assert created is True
        for fragment_index, (message_start_id, message_end_id, content) in enumerate([(1, 3, first_content), (4, 4, last_content)]):
            fragment = make_fragment(
                fragment_index=fragment_index,
                work_dedupe_key=work_dedupe_key,
                stage_key=persisted_stage.stage_key,
                model_key=persisted_stage.model_key,
                message_start_id=message_start_id,
                message_end_id=message_end_id,
            )
            fragment.uid = persisted_stage.uid
            fragment.session_id = persisted_stage.session_id
            fragment.content = content
            persisted_fragment, fragment_created = await context_summary_fragment_crud.write_ordered(
                db,
                fragment=fragment,
            )
            assert persisted_fragment is not None
            assert fragment_created is True
        await db.refresh(persisted_stage)
        assert await context_summary_stage_crud.mark_completed(
            db,
            work_dedupe_key=work_dedupe_key,
            stage_key=persisted_stage.stage_key,
            model_key=persisted_stage.model_key,
        )
        await db.refresh(persisted_stage)

    model = replace(
        _summary_rejection_models()[1],
        context_window_tokens=2_000,
        max_output_tokens=768,
        safety_margin_tokens=0,
        input_budget_tokens=1_232,
    )
    model_prompts: list[str] = []

    async def select_model(*_args, **_kwargs):
        return model

    async def generate_model(*_args, **kwargs):
        model_prompts.append(kwargs["messages"][0].content)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="condensed evidence"),
            model=kwargs["model_id"],
            usage={"completion_tokens": 2},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    profile = SimpleNamespace(id=1)
    cfg = ProfileConfig.model_validate({})
    async with context_summary_database() as db:
        result = await context_summary_reduction.reduce_completed_summary_stage_result(
            db,
            profile=profile,
            cfg=cfg,
            initial_stage=persisted_stage,
            safety_margin_tokens=0,
        )

    async with context_summary_database() as db:
        stages = list((await db.execute(select(ContextSummaryStage))).scalars().all())
        fragments = list((await db.execute(select(ContextSummaryFragment))).scalars().all())

    source_stage = next(stage for stage in stages if stage.stage_key == "repeated-range-source-stage")
    first_reduction = next(stage for stage in stages if stage.lower_stage_key == source_stage.stage_key)
    first_fragments = sorted(
        (fragment for fragment in fragments if fragment.stage_key == first_reduction.stage_key),
        key=lambda fragment: fragment.fragment_index,
    )
    first_ranges = [(fragment.message_start_id, fragment.message_end_id) for fragment in first_fragments]
    assert source_stage.status == ContextSummaryStageStatus.COMPLETED
    assert source_stage.succeeded_fragment_count == 2
    assert first_reduction.status == ContextSummaryStageStatus.COMPLETED
    assert len(first_fragments) == first_reduction.expected_fragment_count
    assert first_ranges.count((1, 3)) >= 2
    assert first_ranges.count((4, 4)) == 1
    assert set(first_ranges) <= {(1, 3), (4, 4)}

    final_stage = next(stage for stage in stages if stage.stage_key == result.stage.stage_key)
    final_fragments = [fragment for fragment in fragments if fragment.stage_key == final_stage.stage_key]
    assert final_stage.status == ContextSummaryStageStatus.COMPLETED
    assert final_stage.expected_fragment_count == 1
    assert final_stage.succeeded_fragment_count == 1
    assert len(final_fragments) == 1
    assert (final_fragments[0].message_start_id, final_fragments[0].message_end_id) == (1, 4)
    assert result.content == final_fragments[0].content == "condensed evidence"
    assert any("first source " in prompt for prompt in model_prompts)
    assert any("tail evidence" in prompt for prompt in model_prompts)
    assert any(last_content in prompt for prompt in model_prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "no-progress", "insufficient"], ids=["success", "no-progress", "insufficient"])
@pytest.mark.parametrize(
    ("covered_user_content", "near_limit_fact_count"),
    [
        ("recent question", 725),
        (
            "Produce the complete itemized procurement report. Preserve source records, verify each quantity and amount, and flag unresolved approvals before delivery.\n" * 10,
            550,
        ),
    ],
    ids=["short-request", "long-request"],
)
async def test_context_summary_checkpoint_refines_against_complete_request_budget(
    context_summary_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    covered_user_content: str,
    near_limit_fact_count: int,
) -> None:
    await _seed_summary_history(context_summary_database)
    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()
        session.context_summary = StoredConversationSummary(conversation_summary="stable saved summary").model_dump_json(exclude_none=True)
        session.context_summary_message_id = 2
        session.context_summary_revision = 3
        session.context_content_revision = 7
        covered_user_message = await db.get(Message, 3)
        assert covered_user_message is not None
        covered_user_message.content = covered_user_content
        history = list((await db.execute(select(Message).where(Message.session_id == "summary-session", Message.id.in_([4])))).scalars())
        for message in history:
            message.content = "history evidence " * 2000
        db.add(
            Message(
                id=5,
                session_id="summary-session",
                uid="summary-user",
                profile_id=1,
                role=MessageRole.USER,
                content="current question",
            )
        )
        await db.commit()

    summary_model = replace(
        _summary_rejection_models()[1],
        max_output_tokens=2_048,
        input_budget_tokens=65_536 - 2_048,
    )
    first_summary = ("fact " * 1_400).strip()
    summary_1_200 = ("fact " * 1_200).strip()
    near_limit_summary = ("fact " * near_limit_fact_count).strip()
    slightly_smaller_summary = ("fact " * (near_limit_fact_count - 4)).strip()
    short_summary = "short summary"
    model_prompts: list[str] = []
    response_sequences = {
        "success": (first_summary, summary_1_200, near_limit_summary, short_summary),
        "insufficient": (first_summary, near_limit_summary, slightly_smaller_summary),
    }

    async def select_model(*_args, excluded_priorities=None, **_kwargs):
        excluded = set(excluded_priorities or ())
        return None if summary_model.priority in excluded else summary_model

    async def generate_model(**kwargs):
        prompt = kwargs["messages"][0].content
        model_prompts.append(prompt)
        response_index = len(model_prompts) - 1
        if mode == "no-progress":
            content = first_summary
        else:
            responses = response_sequences[mode]
            if response_index >= len(responses):
                raise AssertionError("summary refinement attempted another round")
            content = responses[response_index]
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
            model=kwargs["model_id"],
            usage={"completion_tokens": 1},
            finish_reason="stop",
        )

    monkeypatch.setattr(context_summary_stage, "select_context_summary_model", select_model)
    monkeypatch.setattr(context_summary_reduction, "select_context_summary_model", select_model)
    monkeypatch.setattr(LLMClient, "generate", generate_model)

    system_message = InternalMessage(
        role=MessageRole.SYSTEM,
        content=("context fact policy requires preserving verified history and current task constraints " * 7).strip(),
    )
    current_message = InternalMessage(id=5, role=MessageRole.USER, content="current question")
    request_model_id = summary_model.model_id
    request_protocol = summary_model.protocol
    request_channel_id = summary_model.channel_id

    def request_tokens(messages: list[InternalMessage]) -> int:
        return estimate_request_context_tokens(
            materialize_user_environment_prompts(messages),
            [],
            model_id=request_model_id,
            protocol=request_protocol,
            channel_id=request_channel_id,
        )

    def candidate_messages(summary: str) -> list[InternalMessage]:
        summary_message = ContextSummaryState(
            content=StoredConversationSummary(
                conversation_summary=summary,
                user_message=covered_user_content,
                covered_user_message_id=3,
            ).model_dump_json(exclude_none=True),
            message_id=4,
        ).as_message()
        assert summary_message is not None
        return [
            system_message,
            summary_message,
            current_message,
        ]

    target_tokens = calculate_context_summary_trigger_tokens(
        context_window_k=2,
        max_tokens=256,
        threshold_percent=50,
    )
    near_limit_tokens = estimate_tokens(near_limit_summary, model_id=request_model_id, protocol=request_protocol)
    slightly_smaller_tokens = estimate_tokens(slightly_smaller_summary, model_id=request_model_id, protocol=request_protocol)
    assert target_tokens == 744
    assert near_limit_tokens <= target_tokens
    assert slightly_smaller_tokens <= target_tokens
    assert 0 < near_limit_tokens - slightly_smaller_tokens < CONTEXT_SUMMARY_MIN_REFINEMENT_REDUCTION_TOKENS
    assert request_tokens(candidate_messages(near_limit_summary)) > target_tokens
    assert request_tokens(candidate_messages(slightly_smaller_summary)) > target_tokens
    if covered_user_content != "recent question":
        candidate_without_user_message = candidate_messages(near_limit_summary)
        candidate_without_user_message[1].content = None
        assert request_tokens(candidate_without_user_message) <= target_tokens

    checkpoint_kwargs = {
        "session_id": "summary-session",
        "uid": "summary-user",
        "profile": SimpleNamespace(id=1),
        "cfg": SimpleNamespace(
            channel=SimpleNamespace(context_summary_channel=object()),
            other=OtherConfig(context_summary_threshold_percent=50),
        ),
        "messages": [system_message, current_message],
        "trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE,
        "fixed_upper_message_id": 5,
        "context_window_k": 2,
        "max_tokens": 256,
        "tools": [],
        "channel_id": request_channel_id,
        "model_id": request_model_id,
        "protocol": request_protocol,
        "force": True,
    }

    async def run_checkpoint():
        async with context_summary_database() as db:
            return await apply_context_summary_checkpoint(db, **checkpoint_kwargs)

    if mode == "success":
        checkpointed = await run_checkpoint()
        assert request_tokens(checkpointed) <= target_tokens
        checkpointed_summary_meta = checkpointed[1].conversation_summary
        assert checkpointed_summary_meta is not None
        assert checkpointed_summary_meta.content == short_summary
        assert checkpointed_summary_meta.through_message_id == 4
        assert checkpointed_summary_meta.covered_user_message_id == 3
        assert checkpointed[1].content == covered_user_content
        assert checkpointed[2].id == 5
        assert len(model_prompts) == 4
    else:
        with pytest.raises(LLMException) as exc_info:
            await run_checkpoint()
        assert exc_info.value.message == ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED
        if mode == "no-progress":
            assert len(model_prompts) > 1
            assert all(json.loads(prompt)["instructions"].startswith("Further compress the summary below") for prompt in model_prompts[1:])
        else:
            assert len(model_prompts) == 3
            assert json.loads(model_prompts[2])["conversation_summary"] == near_limit_summary

    async with context_summary_database() as db:
        session = (await db.execute(select(ChatSession).where(ChatSession.session_id == "summary-session"))).scalar_one()

    if mode == "success":
        assert session.context_summary is not None
        saved_summary = StoredConversationSummary.model_validate_json(session.context_summary)
        assert saved_summary.conversation_summary == short_summary
        assert saved_summary.covered_user_message_id == 3
        assert saved_summary.user_message == covered_user_content
        assert session.context_summary_message_id == 4
        assert session.context_summary_revision == 4
    else:
        assert session.context_summary is not None
        assert StoredConversationSummary.model_validate_json(session.context_summary).conversation_summary == "stable saved summary"
        assert session.context_summary_message_id == 2
        assert session.context_summary_revision == 3
    assert session.context_content_revision == 7
