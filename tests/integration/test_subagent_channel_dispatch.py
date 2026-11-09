from copy import deepcopy

import pytest

from app.core.constants import AGENT_WORK_TOOL_NAME
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import Profile
from app.models.session_reply_work_item import SessionReplyWorkStatus


async def _configure_runtime(runtime, subagent_rules: list[dict]) -> None:
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

        profile = await db.get(Profile, runtime.profile.id)
        assert profile is not None
        configs = deepcopy(profile.configs)
        configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
        configs["channel"]["subagent_channel"]["rules"] = subagent_rules
        profile.configs = configs
        await db.commit()


async def _run_dispatch(runtime, *, expected_child_model: str) -> None:
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
            InternalMessage(role=MessageRole.ASSISTANT, content="Main task completed."),
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
    assert parent_done.status == SessionReplyWorkStatus.SUCCEEDED
    parent_payloads = runtime.provider_payloads[parent_provider_start:]
    assert parent_payloads
    assert all(payload["model"] == "http-image-model" for payload in parent_payloads)

    child_provider_start = len(runtime.provider_payloads)
    child_done = await runtime.run_next()
    assert child_done is not None
    assert child_done.status == SessionReplyWorkStatus.SUCCEEDED
    child_payloads = runtime.provider_payloads[child_provider_start:]
    assert child_payloads
    assert all(payload["model"] == expected_child_model for payload in child_payloads)


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
