import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.constants import (
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_PROVIDER_NOT_CONFIGURED,
    ERR_PROFILE_NOT_FOUND,
    ERR_PROVIDER_EMBEDDING_ONLY,
)
from app.core.context import (
    ContextManager,
)
from app.core.crud.message import (
    message_crud,
)
from app.core.crud.profile import (
    profile_crud,
)

# CRUD Imports
from app.core.crud.user import user_crud
from app.core.exceptions import (
    BaseBusinessException,
    LLMException,
    ServerException,
)
from app.core.log import (
    LogManager,
    get_logger,
)
from app.core.middleware.auditor import (
    AuditMiddleware,
)
from app.core.prompts import (
    ERR_PARALLEL_LIMIT_EXCEEDED,
    PROMPT_MAX_TURNS_REACHED,
    SYSTEM_CONTEXT_WRAPPER,
    SYSTEM_INSTRUCTIONS_WRAPPER,
)
from app.core.tools import (
    ALL_TOOLS_SCHEMAS,
    TOOL_EXECUTOR_MAP,
)
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.system import get_full_system_context
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
    MessageType,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)
from app.models.provider import ModelUsage
from app.providers.llm.client import (
    LLMClient,
)
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)


class ChatDispatcher:
    @staticmethod
    def _inject_system_prompt(
        profile: Profile,
        messages: list[InternalMessage],
    ) -> list[InternalMessage]:
        """
        注入系统提示词。无论是否关联 Profile Prompt，环境上下文都会注入。
        通过结构化标签隔离系统信息与环境上下文。
        """
        system_context = get_full_system_context()

        # 构造系统提示词
        context_part = SYSTEM_CONTEXT_WRAPPER.format(context=system_context)

        full_parts = [context_part]

        # 1. 如果 Profile 关联了 Prompt，则包裹后放入后续部分
        if profile.prompt and profile.prompt.content:
            instruction_part = SYSTEM_INSTRUCTIONS_WRAPPER.format(content=profile.prompt.content)
            full_parts.append(instruction_part)

        # 合并所有系统提示部分
        full_prompt = "\n\n".join(full_parts)

        # 清除原有的 System 消息并插入新的组合消息到顶部
        messages = [m for m in messages if m.role != MessageRole.SYSTEM]
        messages.insert(
            0,
            InternalMessage(
                role=MessageRole.SYSTEM,
                content=full_prompt,
            ),
        )

        # todo...此处应该将知识库信息追加到系统提示词的尾部
        return messages

    @staticmethod
    async def _fetch_new_user_messages(
        db: AsyncSession,
        session_id: str,
        uid: str,
        last_id: int,
    ) -> list[InternalMessage]:
        """
        检索并解析新产生的用户消息
        """
        raw_msgs = await message_crud.get_new_messages_since_id(db, session_id=session_id, uid=uid, last_id=last_id)
        # 仅追加用户消息，过滤掉系统自动注入或 AI 的响应（因为 AI 响应已经在循环中处理了）
        user_msgs = [m for m in raw_msgs if m.role == MessageRole.USER]
        if not user_msgs:
            return []

        return parse_db_messages_to_internal(user_msgs)

    @staticmethod
    async def _save_message(
        db: AsyncSession,
        session_id: str,
        uid: str,
        role: MessageRole,
        msg_type: MessageType,
        content: Any,
        profile_id: int,
    ):
        await message_crud.create(
            db,
            obj_in={
                "session_id": session_id,
                "uid": uid,
                "role": role,
                "type": msg_type,
                "content": (
                    content.content
                    if (
                        msg_type == MessageType.TEXT
                        and hasattr(
                            content,
                            "content",
                        )
                    )
                    else (
                        content.model_dump_json(exclude_none=True)
                        if hasattr(
                            content,
                            "model_dump_json",
                        )
                        else str(content)
                    )
                ),
                "profile_id": profile_id,
            },
        )

    @staticmethod
    async def _audit_tool_call(
        db,
        profile,
        cfg,
        tool_name,
        args,
        messages=None,
    ) -> str | None:
        return await AuditMiddleware.audit(
            db,
            profile,
            cfg,
            tool_name,
            args,
        )

    @staticmethod
    async def _process_single_tool(
        tool_call: Any,
        db: AsyncSession,
        profile: Profile,
        cfg: ProfileConfig,
        messages: list[InternalMessage],
        username: str,
        session_id: str,
        turn: int,
        uid: str,
    ) -> InternalMessage:
        tool_name = tool_call.name
        args = tool_call.arguments

        LogManager.log_tool_call(turn, tool_name, json.dumps(args, ensure_ascii=False), session_id, uid)

        cmd_result = await ChatDispatcher._audit_tool_call(
            db,
            profile,
            cfg,
            tool_name,
            args,
            messages,
        )

        if cmd_result is None:
            executor_cls = TOOL_EXECUTOR_MAP.get(tool_name)
            if executor_cls:
                instance = executor_cls(
                    project_root=os.getcwd(),
                    uid=uid,
                )
                cmd_result = await instance.execute(**args)
            else:
                cmd_result = json.dumps(
                    {"error": f"Tool {tool_name} not registered"},
                    ensure_ascii=False,
                )

        LogManager.log_tool_result(turn, cmd_result, session_id, uid)

        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=cmd_result,
        )

    @staticmethod
    async def dispatch(
        db: AsyncSession,
        message: str,
        uid: str,
        session_id: str = "default",
    ):
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(
                f"[{username}] (Session: {session_id}) User Message: {message}"
            )
            await ChatDispatcher._save_message(
                db,
                session_id,
                uid,
                MessageRole.USER,
                MessageType.TEXT,
                message,
                profile.id if profile.id else -1,
            )

            if not profile:
                raise LLMException(message=ERR_PROFILE_NOT_FOUND)

            cfg = ProfileConfig.model_validate(profile.configs)

            if not profile.provider:
                raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

            if profile.provider.usage == ModelUsage.EMBEDDING:
                raise LLMException(message=ERR_PROVIDER_EMBEDDING_ONLY)

            messages = await ContextManager.get_messages(
                db,
                session_id,
                uid,
                profile,
                message,
            )

            messages = ChatDispatcher._inject_system_prompt(profile, messages)

            # 初始化最后处理的消息 ID
            last_processed_id = 0
            for m in messages:
                if m.id and m.id > last_processed_id:
                    last_processed_id = m.id

            tools = ALL_TOOLS_SCHEMAS
            turn_messages: list[InternalMessage] = []

            (
                max_turns,
                current_turn,
                final_ai_content,
            ) = (
                cfg.tool.max_turns,
                0,
                "",
            )
            if not profile.provider:
                raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

            while current_turn <= max_turns:
                # 动态追加用户新消息
                new_user_msgs = await ChatDispatcher._fetch_new_user_messages(db, session_id, uid, last_processed_id)
                if new_user_msgs:
                    for nm in new_user_msgs:
                        logger.bind(uid=uid, session_id=session_id).info(
                            f"[{username}] (Session: {session_id}) Appending new user message: {nm.content}"
                        )
                        messages.append(nm)
                        if nm.id and nm.id > last_processed_id:
                            last_processed_id = nm.id

                current_turn += 1

                # 达到最大轮次时注入收官指令并确保协议合规
                if current_turn == max_turns:
                    summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                    notice_msg = InternalMessage(
                        role=MessageRole.USER,
                        content=summary_notice,
                    )
                    messages.append(notice_msg)
                    # 持久化注入的指令到数据库以保持审计一致性
                    await ChatDispatcher._save_message(
                        db,
                        session_id,
                        uid,
                        MessageRole.USER,
                        MessageType.TEXT,
                        summary_notice,
                        profile.id,
                    )

                    current_tools = None
                else:
                    current_tools = tools

                response = await LLMClient.generate(
                    api_key=profile.provider.api_key,
                    base_url=profile.provider.base_url,
                    model_id=cfg.provider.model_id,
                    messages=messages,
                    temperature=cfg.provider.temperature,
                    max_tokens=cfg.provider.max_tokens,
                    tools=current_tools,
                    protocol=getattr(
                        profile.provider,
                        "protocol",
                        "openai",
                    ),
                )

                ai_msg = response.message
                logger.bind(uid=uid, session_id=session_id).info(
                    f"[{username}] (Session: {session_id}) Turn {current_turn} | "
                    f"LLM Response: {ai_msg.content or '[Tool Call]'}"
                )
                # 空消息拦截逻辑
                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                messages.append(ai_msg)

                await ChatDispatcher._save_message(
                    db,
                    session_id,
                    uid,
                    MessageRole.ASSISTANT,
                    MessageType.TOOL_CALL if ai_msg.tool_calls else MessageType.TEXT,
                    ai_msg,
                    profile.id,
                )
                if ai_msg.tool_calls:
                    turn_messages.append(ai_msg)

                if not ai_msg.tool_calls:
                    final_ai_content = ai_msg.content
                    break

                if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                    error_msg = json.dumps(
                        {
                            "error": "parallel_limit_exceeded",
                            "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(
                                requested=len(ai_msg.tool_calls),
                                limit=cfg.tool.max_parallel_tools,
                            ),
                        },
                        ensure_ascii=False,
                    )

                    for tool_call in ai_msg.tool_calls:
                        tool_res = InternalMessage(
                            role=MessageRole.TOOL,
                            tool_call_id=tool_call.id,
                            content=error_msg,
                        )
                        messages.append(tool_res)
                        await ChatDispatcher._save_message(
                            db,
                            session_id,
                            uid,
                            MessageRole.TOOL,
                            MessageType.TOOL_RESULT,
                            tool_res,
                            profile.id,
                        )
                    continue

                tasks = [
                    ChatDispatcher._process_single_tool(
                        tc,
                        db,
                        profile,
                        cfg,
                        messages,
                        username,
                        session_id,
                        current_turn,
                        uid,
                    )
                    for tc in ai_msg.tool_calls
                ]

                tool_responses = await asyncio.gather(*tasks)

                for tool_res in tool_responses:
                    messages.append(tool_res)
                    await ChatDispatcher._save_message(
                        db,
                        session_id,
                        uid,
                        MessageRole.TOOL,
                        MessageType.TOOL_RESULT,
                        tool_res,
                        profile.id,
                    )
                    turn_messages.append(tool_res)

            return LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=final_ai_content),
                        finish_reason=True,
                        created_at=time.time(),
                    )
                ],
                history=[m.model_dump(exclude_none=True) for m in turn_messages],
            ).model_dump()
        except BaseBusinessException:
            raise
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).exception("Dispatcher Error")
            raise ServerException(message=str(e))

    @staticmethod
    async def dispatch_stream(
        db: AsyncSession,
        message: str,
        uid: str,
        session_id: str = "default",
    ) -> AsyncGenerator[dict[str, Any]]:
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(
                f"[{username}] (Session: {session_id}) User Message: {message}"
            )
            await ChatDispatcher._save_message(
                db,
                session_id,
                uid,
                MessageRole.USER,
                MessageType.TEXT,
                message,
                profile.id if profile.id else -1,
            )

            if not profile:
                raise LLMException(message=ERR_PROFILE_NOT_FOUND)

            cfg = ProfileConfig.model_validate(profile.configs)

            if not profile.provider:
                raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

            if profile.provider.usage == ModelUsage.EMBEDDING:
                raise LLMException(message=ERR_PROVIDER_EMBEDDING_ONLY)

            messages = await ContextManager.get_messages(
                db,
                session_id,
                uid,
                profile,
                message,
            )

            messages = ChatDispatcher._inject_system_prompt(profile, messages)

            # 初始化最后处理的消息 ID
            last_processed_id = 0
            for m in messages:
                if m.id and m.id > last_processed_id:
                    last_processed_id = m.id

            tools = ALL_TOOLS_SCHEMAS
            turn_messages: list[InternalMessage] = []

            (
                max_turns,
                current_turn,
                _final_ai_content,
            ) = (
                cfg.tool.max_turns,
                0,
                "",
            )
            if not profile.provider:
                raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

            while current_turn <= max_turns:
                # 动态追加用户新消息
                new_user_msgs = await ChatDispatcher._fetch_new_user_messages(db, session_id, uid, last_processed_id)
                if new_user_msgs:
                    for nm in new_user_msgs:
                        logger.bind(uid=uid, session_id=session_id).info(
                            f"[{username}] (Session: {session_id}) Appending new user message: {nm.content}"
                        )
                        messages.append(nm)
                        if nm.id and nm.id > last_processed_id:
                            last_processed_id = nm.id

                current_turn += 1

                # 达到最大轮次时注入收官指令并确保协议合规
                if current_turn == max_turns:
                    summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                    notice_msg = InternalMessage(
                        role=MessageRole.USER,
                        content=summary_notice,
                    )
                    messages.append(notice_msg)
                    # 持久化注入的指令到数据库以保持审计一致性
                    await ChatDispatcher._save_message(
                        db,
                        session_id,
                        uid,
                        MessageRole.USER,
                        MessageType.TEXT,
                        summary_notice,
                        profile.id,
                    )

                    current_tools = None
                else:
                    current_tools = tools

                # 用于拼接当前轮次的工具调用和文本
                current_tool_calls_map = {}  # index -> {id, name, arguments_chunks}
                current_content_chunks = []

                async for chunk in LLMClient.generate_stream(
                    api_key=profile.provider.api_key,
                    base_url=profile.provider.base_url,
                    model_id=cfg.provider.model_id,
                    messages=messages,
                    temperature=cfg.provider.temperature,
                    max_tokens=cfg.provider.max_tokens,
                    tools=current_tools,
                    protocol=getattr(
                        profile.provider,
                        "protocol",
                        "openai",
                    ),
                ):
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})

                    # 1. 处理文本内容增量
                    content = delta.get("content")
                    if content:
                        current_content_chunks.append(content)
                        yield {
                            "type": "content",
                            "content": content,
                            "turn": current_turn,
                        }

                    # 2. 处理工具调用增量
                    tool_calls = delta.get("tool_calls")
                    if tool_calls:
                        for tc in tool_calls:
                            idx = tc.get("index", 0)
                            if idx not in current_tool_calls_map:
                                current_tool_calls_map[idx] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                }
                            if tc.get("id"):
                                current_tool_calls_map[idx]["id"] = tc.get("id")
                            if tc.get("function", {}).get("name"):
                                current_tool_calls_map[idx]["name"] = tc.get("function", {}).get("name")
                            if tc.get("function", {}).get("arguments"):
                                current_tool_calls_map[idx]["arguments"] += tc.get("function", {}).get("arguments")

                # 流结束，整理最终 AI 响应
                final_content = "".join(current_content_chunks)
                final_tool_calls = []
                for idx, tc_data in sorted(current_tool_calls_map.items()):
                    if tc_data.get("name"):
                        args_dict = {}
                        if tc_data.get("arguments"):
                            try:
                                args_dict = json.loads(tc_data.get("arguments"))
                            except Exception as parse_err:
                                logger.bind(uid=uid, session_id=session_id).warning(
                                    "Failed to parse arguments json: %s, error: %s",
                                    tc_data.get("arguments"),
                                    parse_err,
                                )
                        final_tool_calls.append(
                            InternalToolCall(
                                id=tc_data.get("id") or f"call_{idx}",
                                name=tc_data.get("name"),
                                arguments=args_dict,
                            )
                        )

                # 空消息拦截逻辑
                if not final_tool_calls and not final_content.strip():
                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

                ai_msg = InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content=final_content if final_content else None,
                    tool_calls=final_tool_calls if final_tool_calls else None,
                )

                logger.bind(uid=uid, session_id=session_id).info(
                    f"[{username}] (Session: {session_id}) Turn {current_turn} | "
                    f"LLM Response: {ai_msg.content or '[Tool Call]'}"
                )

                messages.append(ai_msg)

                await ChatDispatcher._save_message(
                    db,
                    session_id,
                    uid,
                    MessageRole.ASSISTANT,
                    MessageType.TOOL_CALL if ai_msg.tool_calls else MessageType.TEXT,
                    ai_msg,
                    profile.id,
                )

                # 更新最后处理 ID 为刚发送的消息（虽然 _save_message 不直接返回 ID，
                # 但下一轮循环的 _fetch_new_user_messages 会基于此 ID 过滤）
                # 注意：由于数据库自增 ID 由数据库生成，我们这里无法即时获得，
                # 但新输入的 USER 消息 ID 肯定大于进入 dispatch 时最大的 ID。
                if ai_msg.tool_calls:
                    turn_messages.append(ai_msg)

                if not ai_msg.tool_calls:
                    _final_ai_content = ai_msg.content
                    break

                if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                    error_msg = json.dumps(
                        {
                            "error": "parallel_limit_exceeded",
                            "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(
                                requested=len(ai_msg.tool_calls),
                                limit=cfg.tool.max_parallel_tools,
                            ),
                        },
                        ensure_ascii=False,
                    )

                    for tool_call in ai_msg.tool_calls:
                        tool_res = InternalMessage(
                            role=MessageRole.TOOL,
                            tool_call_id=tool_call.id,
                            content=error_msg,
                        )
                        messages.append(tool_res)
                        await ChatDispatcher._save_message(
                            db,
                            session_id,
                            uid,
                            MessageRole.TOOL,
                            MessageType.TOOL_RESULT,
                            tool_res,
                            profile.id,
                        )
                    continue

                # 实时推送每个工具执行的开始
                for tc in ai_msg.tool_calls:
                    yield {
                        "type": "tool_start",
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "tool_call_id": tc.id,
                    }

                tasks = [
                    ChatDispatcher._process_single_tool(
                        tc,
                        db,
                        profile,
                        cfg,
                        messages,
                        username,
                        session_id,
                        current_turn,
                        uid,
                    )
                    for tc in ai_msg.tool_calls
                ]

                tool_responses = await asyncio.gather(*tasks)

                for tool_res in tool_responses:
                    messages.append(tool_res)
                    await ChatDispatcher._save_message(
                        db,
                        session_id,
                        uid,
                        MessageRole.TOOL,
                        MessageType.TOOL_RESULT,
                        tool_res,
                        profile.id,
                    )
                    turn_messages.append(tool_res)
                    # 实时推送工具执行的结束与结果
                    tool_name = next(
                        (tc.name for tc in ai_msg.tool_calls if tc.id == tool_res.tool_call_id),
                        "unknown",
                    )
                    yield {
                        "type": "tool_end",
                        "name": tool_name,
                        "result": tool_res.content,
                        "tool_call_id": tool_res.tool_call_id,
                    }

            yield {
                "type": "done",
                "session_id": session_id,
                "history": [m.model_dump(exclude_none=True) for m in turn_messages],
            }
        except BaseBusinessException as bbe:
            yield {
                "type": "error",
                "message": bbe.message,
            }
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).exception("Dispatcher Stream Error")
            yield {
                "type": "error",
                "message": str(e),
            }
