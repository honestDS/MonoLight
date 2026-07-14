from types import SimpleNamespace

from app.core.prompts import CONTEXT_SUMMARY_COMPRESS_PROMPT, CONTEXT_SUMMARY_PROMPT
from app.core.utils.context_summary import common as common_module
from app.core.utils.context_summary import history as history_module
from app.core.utils.context_summary import reduction as reduction_module
from app.core.utils.context_summary import service as service_module
from app.core.utils.context_summary import stage as stage_module
from app.core.utils.context_summary.common import (
    ContextSummaryState,
    join_messages,
    serialize_message,
)
from app.core.utils.context_summary.model_call import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.models.message import InternalMessage, MessageRole


def _patch_token_counter(monkeypatch, token_counter):
    monkeypatch.setattr(common_module, "estimate_tokens", token_counter)
    monkeypatch.setattr(history_module, "estimate_tokens", token_counter)
    monkeypatch.setattr(service_module, "estimate_tokens", token_counter)
    monkeypatch.setattr(stage_module, "estimate_tokens", token_counter)


def _summary_cfg(threshold_percent: int = 90) -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(
            chat_channel=object(),
            context_summary_channel=object(),
        ),
        other=SimpleNamespace(context_summary_threshold_percent=threshold_percent),
    )


def _patch_summary_dependencies(monkeypatch, *, update_result=True, generation_error=None):
    selected_calls = []
    update_calls = []
    generated_calls = []

    async def get_state(_db, *, session_id, uid):
        return ContextSummaryState(content=None, message_id=None)

    async def build_snapshot(
        _db,
        *,
        expected_summary_message_id,
        before_id,
        frozen_user_message_ids=None,
        **_kwargs,
    ):
        target_id = (expected_summary_message_id or 0) + 4
        return ContextSummarySnapshot(
            expected_summary_message_id=expected_summary_message_id,
            snapshot_before_id=before_id,
            snapshot_max_message_id=target_id + 1,
            persistent_summary_target_id=target_id,
            recent_round_start_ids=(target_id + 1,),
            frozen_user_message_ids=tuple(frozen_user_message_ids or ()),
            recent_messages=(InternalMessage(id=target_id + 1, role=MessageRole.USER, content="recent"),),
        )

    async def iter_rounds(_db, *, snapshot, **_kwargs):
        start_id = snapshot.expected_summary_message_id or 0
        yield [
            InternalMessage(id=start_id + 1, role=MessageRole.USER, content="u1" * 100),
            InternalMessage(id=start_id + 2, role=MessageRole.ASSISTANT, content="a1" * 100),
        ]
        yield [
            InternalMessage(id=start_id + 3, role=MessageRole.USER, content="u2" * 100),
            InternalMessage(id=start_id + 4, role=MessageRole.ASSISTANT, content="a2" * 100),
        ]

    async def select_model(*_args, **kwargs):
        selected_calls.append(kwargs)
        if kwargs.get("excluded_priorities"):
            return None
        return ContextSummaryModelSnapshot(
            channel_id=1,
            channel_name="summary-channel",
            model_id="summary-model",
            protocol="openai",
            base_url="https://example.invalid",
            api_key="secret",
            priority=1,
            context_window_tokens=4096,
            max_output_tokens=256,
            safety_margin_tokens=0,
            input_budget_tokens=3840,
        )

    async def call_model(*, model, prompt):
        generated_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "messages": [InternalMessage(role=MessageRole.USER, content=prompt)],
                "timeout": CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
            }
        )
        if generation_error is not None:
            raise generation_error
        return "compressed history"

    async def generate_with_input_tokens(
        db,
        *,
        profile,
        cfg,
        prompt,
        input_tokens,
        safety_margin_tokens,
    ):
        excluded_priorities = set()
        while True:
            model = await select_model(
                db,
                profile_id=profile.id,
                channel_config=cfg.channel.context_summary_channel,
                safety_margin_tokens=safety_margin_tokens,
                excluded_priorities=set(excluded_priorities),
                call_context=("context_summary" if not excluded_priorities else "context_summary_retry"),
            )
            if model is None:
                return None
            try:
                return await stage_module.call_fixed_summary_model(
                    model=model,
                    prompt=prompt,
                    input_tokens=input_tokens,
                )
            except Exception:
                excluded_priorities.add(model.priority)

    async def generate_snapshot_summary_result(
        db,
        *,
        profile,
        cfg,
        snapshot,
        existing_summary,
        safety_margin_tokens,
        **_kwargs,
    ):
        conversation_messages = []
        async for round_messages in iter_rounds(
            db,
            snapshot=snapshot,
        ):
            conversation_messages.extend(round_messages)
        prompt = CONTEXT_SUMMARY_PROMPT.format(
            existing_summary=existing_summary or "(none)",
            recent_dialogue="(none)",
            conversation=join_messages(conversation_messages),
        )
        generated = await generate_with_input_tokens(
            db,
            profile=profile,
            cfg=cfg,
            prompt=prompt,
            input_tokens=sum(max(1, stage_module.estimate_tokens(serialize_message(message))) for message in conversation_messages),
            safety_margin_tokens=safety_margin_tokens,
        )
        return stage_module.GeneratedSummaryResult(
            content=generated,
            message_count=len(conversation_messages),
            completed_stage=(SimpleNamespace(content=generated, refinement_index=0) if generated else None),
        )

    async def refine_completed_summary_stage(
        db,
        *,
        profile,
        cfg,
        lower_stage,
        safety_margin_tokens,
        refinement_index,
    ):
        generated = await generate_with_input_tokens(
            db,
            profile=profile,
            cfg=cfg,
            prompt=CONTEXT_SUMMARY_COMPRESS_PROMPT.format(
                summary=lower_stage.content,
            ),
            input_tokens=max(
                1,
                stage_module.estimate_tokens(lower_stage.content),
            ),
            safety_margin_tokens=safety_margin_tokens,
        )
        if not generated:
            raise RuntimeError("Context summary refinement failed")
        stage = SimpleNamespace(
            content=generated,
            refinement_index=refinement_index,
        )
        return reduction_module.CompletedSummaryResult(
            content=generated,
            stage=stage,
        )

    async def update_summary(*_args, **kwargs):
        update_calls.append(kwargs)
        return update_result

    monkeypatch.setattr(service_module, "get_context_summary_state", get_state)
    monkeypatch.setattr(service_module, "build_context_summary_snapshot", build_snapshot)
    monkeypatch.setattr(history_module, "iter_persistent_summary_rounds", iter_rounds)
    monkeypatch.setattr(service_module, "select_context_summary_model", select_model)
    monkeypatch.setattr(stage_module, "select_context_summary_model", select_model)
    monkeypatch.setattr(stage_module, "call_context_summary_model", call_model)
    monkeypatch.setattr(
        service_module,
        "generate_snapshot_summary_result",
        generate_snapshot_summary_result,
    )
    monkeypatch.setattr(
        reduction_module,
        "refine_completed_summary_stage",
        refine_completed_summary_stage,
    )
    monkeypatch.setattr(service_module, "persist_context_summary", update_summary)
    return selected_calls, update_calls, generated_calls
