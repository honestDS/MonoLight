from copy import deepcopy

import pytest
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.subagent import SubagentWork
from app.transformers.openai import OpenAIResponsesTransformer


async def _configure_runtime(
    runtime,
    subagent_rules: list[dict],
    *,
    subagent_fallback_model: str | None = None,
    main_fallback_model: str | None = None,
) -> None:
    async with runtime.factory() as db:
        chat_channel = await db.get(ModelChannel, 1)
        assert chat_channel is not None
        channel = await db.get(ModelChannel, 2)
        if channel is None:
            channel = ModelChannel(
                id=2,
                name="subagent-dedicated-channel",
                api_key=chat_channel.api_key,
                base_url=chat_channel.base_url,
            )
            db.add(channel)
        channel.model_ids = [
            *channel.model_ids,
            {
                "model_id": "subagent-worker-model",
                "usage": "CHAT",
                "protocol": "OPENAI_RESPONSES",
                "context_window_k": 32,
                "max_tokens": 512,
            },
        ]

        if subagent_fallback_model is not None:
            fallback_channel = await db.get(ModelChannel, 3)
            if fallback_channel is None:
                fallback_channel = ModelChannel(
                    id=3,
                    name="subagent-fallback-channel",
                    api_key=chat_channel.api_key,
                    base_url=chat_channel.base_url,
                )
                db.add(fallback_channel)
            fallback_channel.model_ids = [
                *fallback_channel.model_ids,
                {
                    "model_id": subagent_fallback_model,
                    "usage": "CHAT",
                    "protocol": "OPENAI_RESPONSES",
                    "context_window_k": 32,
                    "max_tokens": 512,
                },
            ]

        if main_fallback_model is not None:
            chat_channel.model_ids = [
                *chat_channel.model_ids,
                {
                    "model_id": main_fallback_model,
                    "usage": "CHAT",
                    "protocol": "OPENAI_RESPONSES",
                    "context_window_k": 32,
                    "max_tokens": 512,
                },
            ]

        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
        configs["channel"]["subagent_channel"]["rules"] = subagent_rules
        if main_fallback_model is not None:
            configs["channel"]["chat_channel"]["rules"] = [
                *configs["channel"]["chat_channel"]["rules"],
                {
                    "channel_id": 1,
                    "model_id": main_fallback_model,
                    "priority": 2,
                    "weight": 1,
                },
            ]
        profile.configs = ProfileConfig.model_validate(configs).model_dump(mode="json")
        await db.commit()


async def _run_dispatch(
    runtime,
    *,
    expected_child_model: str,
    expected_child_models: list[str] | None = None,
) -> tuple[SessionReplyWorkItem, SessionReplyWorkItem]:
    runtime.outputs.extend(
        [
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="create-independent-child",
                        name=AGENT_WORK_TOOL_NAME,
                        arguments={
                            "operation": "create",
                            "name": "independent child",
                            "message": "Complete the independent child task.",
                        },
                    )
                ],
            ),
            InternalMessage(role=MessageRole.ASSISTANT, content="Child task completed."),
        ]
    )

    accepted = await runtime.client.post(
        "/api/v1/chat/completions",
        json={
            "session_id": runtime.session_id,
            "request_id": "subagent-channel-dispatch-request",
            "message": "Create and run an independent child task.",
        },
    )
    assert accepted.status_code == 200
    parent_work_id = accepted.json()["data"]["work_id"]

    parent_provider_start = len(runtime.provider_payloads)
    parent_done = await runtime.run_next()
    assert parent_done is not None
    assert parent_done.id == parent_work_id
    assert parent_done.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
    parent_payloads = runtime.provider_payloads[parent_provider_start:]
    assert parent_payloads
    assert all(payload["model"] == "http-image-model" for payload in parent_payloads)

    child_provider_start = len(runtime.provider_payloads)
    child_done = await runtime.run_next()
    assert child_done is not None
    assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
    child_payloads = runtime.provider_payloads[child_provider_start:]
    assert child_payloads
    if expected_child_models is None:
        assert all(payload["model"] == expected_child_model for payload in child_payloads)
    else:
        assert [payload["model"] for payload in child_payloads] == expected_child_models

    return parent_done, child_done


@pytest.mark.asyncio
async def test_subagent_uses_independent_channel_in_real_dispatch(image_chat_runtime) -> None:
    async with image_chat_runtime(protocol="OPENAI_RESPONSES", goal_mode=False) as runtime:
        await _configure_runtime(
            runtime,
            [
                {
                    "channel_id": 2,
                    "model_id": "subagent-worker-model",
                    "priority": 1,
                    "weight": 1,
                }
            ],
        )
        await _run_dispatch(runtime, expected_child_model="subagent-worker-model")


@pytest.mark.asyncio
async def test_subagent_falls_back_to_chat_channel_when_subagent_rules_empty(image_chat_runtime) -> None:
    async with image_chat_runtime(protocol="OPENAI_RESPONSES", goal_mode=False) as runtime:
        await _configure_runtime(runtime, [])
        await _run_dispatch(runtime, expected_child_model="http-image-model")


@pytest.mark.asyncio
async def test_subagent_independent_channel_uses_its_own_fallback_after_primary_failure(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_model = "subagent-worker-model"
    fallback_model = "subagent-fallback-model"
    main_fallback_model = "main-only-fallback"
    child_result = "Child task completed."

    async with image_chat_runtime(protocol="OPENAI_RESPONSES", goal_mode=False) as runtime:
        await _configure_runtime(
            runtime,
            [
                {
                    "channel_id": 2,
                    "model_id": primary_model,
                    "priority": 1,
                    "weight": 1,
                },
                {
                    "channel_id": 3,
                    "model_id": fallback_model,
                    "priority": 2,
                    "weight": 1,
                },
            ],
            subagent_fallback_model=fallback_model,
            main_fallback_model=main_fallback_model,
        )

        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            config = ProfileConfig.model_validate(profile.configs)
            assert [rule.model_id for rule in config.channel.chat_channel.rules] == [runtime.chat_model_id, main_fallback_model]
            assert [rule.model_id for rule in config.channel.subagent_channel.rules] == [primary_model, fallback_model]

        fixture_post_json = OpenAIResponsesTransformer._post_json

        async def fail_subagent_primary(transformer, **kwargs):
            payload = kwargs["payload"]
            if payload["model"] == primary_model:
                runtime.provider_payloads.append(payload)
                transformer._raise_provider_error(
                    {"error": {"type": "server_error", "message": "subagent primary unavailable"}},
                    message=ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
                    status=503,
                )
            return await fixture_post_json(transformer, **kwargs)

        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fail_subagent_primary)
        parent_done, child_done = await _run_dispatch(
            runtime,
            expected_child_model=fallback_model,
            expected_child_models=[primary_model, fallback_model],
        )

        assert all(payload["model"] == runtime.chat_model_id for payload in runtime.provider_payloads[:1])
        assert main_fallback_model not in [payload["model"] for payload in runtime.provider_payloads]

        assert child_done.id is not None
        async with runtime.factory() as db:
            child = await db.get(SessionReplyWorkItem, child_done.id)
            assert child is not None
            assert child.status == SessionReplyWorkStatus.SUCCEEDED
            assert child.result_message_id is not None
            result_message = await db.get(Message, child.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == child_result

            round_row = await db.get(SubagentWork, child_done.id)
            assert round_row is not None
            assert round_row.result is not None
            assert round_row.result["completed"] is True
            assert round_row.result["status"] == "succeeded"
            assert round_row.result["result"]["content"] == child_result

            parent = await db.get(SessionReplyWorkItem, parent_done.id)
            assert parent is not None
            assert parent.status == SessionReplyWorkStatus.READY_FOR_LLM
            persisted_children = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(parent_done.id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [persisted_child.id for persisted_child in persisted_children] == [child_done.id]
