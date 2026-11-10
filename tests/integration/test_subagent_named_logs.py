from copy import deepcopy

import pytest
from loguru import logger
from sqlalchemy import select

from app.core.constants import AGENT_WORK_TOOL_NAME, FILE_TOOL_NAME
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.subagent import Subagent, SubagentToolCall


@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["openai", "openai-responses"])
@pytest.mark.parametrize("stream_delivery", [True, False], ids=["stream", "buffered"])
@pytest.mark.asyncio
async def test_named_subagent_identity_persists_across_real_work_rounds(
    image_chat_runtime,
    tmp_path,
    protocol: str,
    stream_delivery: bool,
) -> None:
    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        captured_logs: list[tuple[str, dict]] = []

        def capture_log(message) -> None:
            record = message.record
            captured_logs.append((record["message"], dict(record["extra"])))

        sink_id = logger.add(
            capture_log,
            level="DEBUG",
            filter=lambda record: record["extra"].get("uid") == runtime.uid and record["extra"].get("session_id") == runtime.session_id,
        )
        try:
            name = " 资料核查{员} "
            label = f"子代理：{name}"
            main_label = "主代理"
            file_content = "named-subagent-file-content-verified"
            target = (tmp_path / "named-subagent-log.txt").resolve()
            target.write_text(f"{file_content}\n", encoding="utf-8")
            child_instruction = f"请读取 {target}，并在总结中包含文件里的唯一内容。"
            first_user_message = "用户消息: named-subagent-first-parent-request"
            first_child_summary = f"首轮核查结果: {file_content}"
            first_parent_summary = "首轮主代理总结"

            def logs_for(work_id: int) -> list[tuple[str, dict]]:
                return [(text, extra) for text, extra in captured_logs if extra.get("work_id") == work_id]

            def assert_identity(entries: list[tuple[str, dict]], *, role: str, expected_label: str, expected_name: str | None) -> None:
                assert entries
                for text, extra in entries:
                    assert extra.get("agent_role") == role
                    assert extra.get("agent_label") == expected_label
                    assert extra.get("subagent_name") == expected_name
                    assert text.startswith(f"[{expected_label}] ")

            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="named-log-create-call",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={"operation": "create", "name": name, "message": child_instruction},
                        )
                    ],
                )
            )
            accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "named-subagent-first-parent-request",
                    "message": first_user_message,
                },
            )
            assert accepted.status_code == 200
            first_parent_work_id = accepted.json()["data"]["work_id"]
            assert isinstance(first_parent_work_id, int)
            async with runtime.factory() as db:
                first_parent = await db.get(SessionReplyWorkItem, first_parent_work_id)
                assert first_parent is not None
                state = dict(first_parent.execution_state)
                state["stream_requested"] = stream_delivery
                first_parent.execution_state = state
                await db.commit()

            first_parent_replied = await runtime.run_next()
            assert first_parent_replied is not None
            assert first_parent_replied.id == first_parent_work_id
            assert first_parent_replied.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async with runtime.factory() as db:
                first_child = (
                    (
                        await db.execute(
                            select(SessionReplyWorkItem).where(
                                SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                                SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                                SessionReplyWorkItem.source_id == str(first_parent_work_id),
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                first_child_work_id = first_child.id
                subagent_id = first_child.subagent_id
                assert first_child_work_id is not None and isinstance(subagent_id, str)
                subagent = await db.get(Subagent, subagent_id)
                assert subagent is not None
                assert subagent.name == name
                first_create_call = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == first_parent_work_id))).scalars().one()
                assert first_create_call.arguments["name"] == name
                assert first_create_call.arguments["message"] == child_instruction

            runtime.outputs.extend(
                [
                    InternalMessage(
                        role=MessageRole.ASSISTANT,
                        tool_calls=[
                            InternalToolCall(
                                id="named-log-file-read-call",
                                name=FILE_TOOL_NAME,
                                arguments={"operation": "read", "path": str(target)},
                            )
                        ],
                    ),
                    InternalMessage(role=MessageRole.ASSISTANT, content=first_child_summary),
                ]
            )
            first_child_done = await runtime.run_next(independent_only=True)
            assert first_child_done is not None
            assert first_child_done.id == first_child_work_id
            assert first_child_done.status == SessionReplyWorkStatus.SUCCEEDED

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=first_parent_summary))
            first_parent_done = await runtime.run_next()
            assert first_parent_done is not None
            assert first_parent_done.id == first_parent_work_id
            assert first_parent_done.status == SessionReplyWorkStatus.SUCCEEDED

            second_user_message = "named-subagent-second-parent-request"
            second_child_instruction = "继续使用已保存的核查上下文，给出补充结论。"
            second_child_summary = "第二轮核查补充结果"
            second_parent_summary = "第二轮主代理总结"
            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="named-log-send-message-call",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={
                                "operation": "send_message",
                                "subagent_id": subagent_id,
                                "message": second_child_instruction,
                            },
                        )
                    ],
                )
            )
            second_accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "named-subagent-second-parent-request",
                    "message": second_user_message,
                },
            )
            assert second_accepted.status_code == 200
            second_parent_work_id = second_accepted.json()["data"]["work_id"]
            assert isinstance(second_parent_work_id, int)
            async with runtime.factory() as db:
                second_parent = await db.get(SessionReplyWorkItem, second_parent_work_id)
                assert second_parent is not None
                state = dict(second_parent.execution_state)
                state["stream_requested"] = stream_delivery
                second_parent.execution_state = state
                await db.commit()

            second_parent_replied = await runtime.run_next()
            assert second_parent_replied is not None
            assert second_parent_replied.id == second_parent_work_id
            assert second_parent_replied.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            async with runtime.factory() as db:
                second_child = (
                    (
                        await db.execute(
                            select(SessionReplyWorkItem).where(
                                SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                                SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                                SessionReplyWorkItem.source_id == str(second_parent_work_id),
                            )
                        )
                    )
                    .scalars()
                    .one()
                )
                second_child_work_id = second_child.id
                assert second_child_work_id is not None
                assert second_child_work_id != first_child_work_id
                assert second_child.subagent_id == subagent_id
                calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
                assert [call.arguments["operation"] for call in calls] == ["create", "send_message"]
                assert calls[0].arguments["name"] == name
                assert "name" not in calls[1].arguments
                subagent = await db.get(Subagent, subagent_id)
                assert subagent is not None and subagent.name == name

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_child_summary))
            second_child_done = await runtime.run_next(independent_only=True)
            assert second_child_done is not None
            assert second_child_done.id == second_child_work_id
            assert second_child_done.status == SessionReplyWorkStatus.SUCCEEDED

            runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=second_parent_summary))
            second_parent_done = await runtime.run_next()
            assert second_parent_done is not None
            assert second_parent_done.id == second_parent_work_id
            assert second_parent_done.status == SessionReplyWorkStatus.SUCCEEDED

            first_parent_logs = logs_for(first_parent_work_id)
            assert_identity(first_parent_logs, role="main", expected_label=main_label, expected_name=None)
            assert any("用户消息:" in text and first_user_message in text for text, _extra in first_parent_logs)

            first_child_logs = logs_for(first_child_work_id)
            assert_identity(first_child_logs, role="subagent", expected_label=label, expected_name=name)
            initial_instruction_logs = [text for text, _extra in first_child_logs if child_instruction in text]
            assert any("主代理消息:" in text for text in initial_instruction_logs)
            assert all("用户消息" not in text and "workflow_image_user" not in text for text in initial_instruction_logs)

            file_call_logs = [(text, extra) for text, extra in first_child_logs if extra.get("tool_call") and FILE_TOOL_NAME in text and target.name in text]
            file_result_logs = [(text, extra) for text, extra in first_child_logs if extra.get("tool_result") and file_content in text]
            assert file_call_logs and file_result_logs
            assert any("LLM 响应:" in text and first_child_summary in text for text, _extra in first_child_logs)

            second_child_logs = logs_for(second_child_work_id)
            assert_identity(second_child_logs, role="subagent", expected_label=label, expected_name=name)
            second_initial_instruction_logs = [text for text, _extra in second_child_logs if second_child_instruction in text]
            assert any("主代理消息:" in text for text in second_initial_instruction_logs)
            assert all("用户消息" not in text and "workflow_image_user" not in text for text in second_initial_instruction_logs)
            assert any("LLM 响应:" in text and second_child_summary in text for text, _extra in second_child_logs)

            second_parent_logs = logs_for(second_parent_work_id)
            assert_identity(second_parent_logs, role="main", expected_label=main_label, expected_name=None)
            assert any("LLM 响应:" in text and second_parent_summary in text for text, _extra in second_parent_logs)
        finally:
            logger.remove(sink_id)


@pytest.mark.asyncio
async def test_send_message_to_ready_subagent_logs_as_subagent_main_message(image_chat_runtime) -> None:
    async with image_chat_runtime("OPENAI_RESPONSES", goal_mode=False) as runtime:
        async with runtime.factory() as db:
            profile = await db.get(Profile, runtime.profile.id)
            assert profile is not None
            configs = deepcopy(profile.configs)
            configs["tool"]["enabled_tools"] = list(dict.fromkeys([*configs["tool"]["enabled_tools"], AGENT_WORK_TOOL_NAME]))
            profile.configs = configs
            await db.commit()

        name = "实时核查"
        initial_instruction = "读取给定材料并总结核查结果。"
        runtime.outputs.append(
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id="running-append-create-call",
                        name=AGENT_WORK_TOOL_NAME,
                        arguments={"operation": "create", "name": name, "message": initial_instruction},
                    )
                ],
            )
        )
        first_accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "running-append-first-parent-request",
                "message": "创建实时核查子任务。",
            },
        )
        assert first_accepted.status_code == 200
        first_parent_work_id = first_accepted.json()["data"]["work_id"]
        first_parent_replied = await runtime.run_next(independent_only=False)
        assert first_parent_replied is not None
        assert first_parent_replied.id == first_parent_work_id
        assert first_parent_replied.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

        async with runtime.factory() as db:
            child = (
                (
                    await db.execute(
                        select(SessionReplyWorkItem).where(
                            SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                            SessionReplyWorkItem.source_type == SessionReplySourceType.PARENT_WORK,
                            SessionReplyWorkItem.source_id == str(first_parent_work_id),
                        )
                    )
                )
                .scalars()
                .one()
            )
            child_work_id = child.id
            subagent_id = child.subagent_id
            assert child_work_id is not None and isinstance(subagent_id, str)
            assert child.status == SessionReplyWorkStatus.READY_FOR_LLM

        appended_instruction = "补充检查最新材料，并报告新增发现。"
        captured_logs: list[tuple[str, dict]] = []

        def capture_log(message) -> None:
            record = message.record
            captured_logs.append((record["message"], dict(record["extra"])))

        sink_id = logger.add(
            capture_log,
            level="DEBUG",
            filter=lambda record: record["extra"].get("uid") == runtime.uid and record["extra"].get("session_id") == runtime.session_id,
        )
        try:
            runtime.outputs.append(
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="running-append-send-call",
                            name=AGENT_WORK_TOOL_NAME,
                            arguments={
                                "operation": "send_message",
                                "subagent_id": subagent_id,
                                "message": appended_instruction,
                            },
                        )
                    ],
                )
            )
            second_accepted = await runtime.client.post(
                "/api/v1/chat/completions",
                json={
                    "session_id": runtime.session_id,
                    "request_id": "running-append-second-parent-request",
                    "message": "向实时核查子任务追加指令。",
                },
            )
            assert second_accepted.status_code == 200
            second_parent_work_id = second_accepted.json()["data"]["work_id"]
            second_parent_replied = await runtime.run_next(independent_only=False)
            assert second_parent_replied is not None
            assert second_parent_replied.id == second_parent_work_id
            assert second_parent_replied.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK

            matching_logs = [(text, extra) for text, extra in captured_logs if extra.get("work_id") == child_work_id and appended_instruction in text]
            assert matching_logs
            assert any(text.startswith(f"[子代理：{name}] ") and "主代理消息:" in text and "用户消息" not in text for text, _extra in matching_logs)
            assert all(extra.get("agent_role") == "subagent" and extra.get("subagent_name") == name for _text, extra in matching_logs)

            async with runtime.factory() as db:
                child_after_send = await db.get(SessionReplyWorkItem, child_work_id)
                assert child_after_send is not None
                assert child_after_send.status == SessionReplyWorkStatus.READY_FOR_LLM
                assert child_after_send.subagent_id == subagent_id
                subagent_calls = (await db.execute(select(SubagentToolCall).where(SubagentToolCall.subagent_id == subagent_id).order_by(SubagentToolCall.id))).scalars().all()
                assert [(call.arguments["operation"], call.child_work_id) for call in subagent_calls] == [
                    ("create", child_work_id),
                    ("send_message", child_work_id),
                ]
        finally:
            logger.remove(sink_id)
