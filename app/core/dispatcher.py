import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

from sqlalchemy import update
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
from app.core.crud.active_session import (
    active_session_crud,
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
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.system import get_full_system_context
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    Message,
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
    async def _fetch_and_merge_new_user_messages(
        db: AsyncSession,
        session_id: str,
        uid: str,
    ) -> list[InternalMessage]:
        """
        检索并合并未处理的新产生用户消息
        """
        raw_msgs = await message_crud.get_unprocessed_messages(db, session_id=session_id, uid=uid)
        # 仅处理未标记的用户消息
        user_msgs = [m for m in raw_msgs if m.role == MessageRole.USER]
        if not user_msgs:
            return []

        # 获取数据库记录的ID集合以便更新
        msg_ids = [m.id for m in user_msgs if m.id is not None]

        # 合并内容与附件
        merged_content = []
        merged_attachments = []
        for m in user_msgs:
            if m.content:
                merged_content.append(str(m.content).strip())
            if m.attachments:
                merged_attachments.extend(m.attachments)

        # 标记为已处理 (通过 ORM 对象属性更新方式)
        if user_msgs:
            for m in user_msgs:
                m.is_processed = True
                db.add(m)
            await db.commit()

        # 返回合并后的单条 InternalMessage
        combined_msg = InternalMessage(
            id=msg_ids[-1] if msg_ids else None,  # 使用最后一条的 ID
            role=MessageRole.USER,
            content="\n".join(merged_content) if merged_content else None,
            attachments=list(dict.fromkeys(merged_attachments)) if merged_attachments else None,
        )
        return [combined_msg]

    @staticmethod
    async def _save_message(
        db: AsyncSession,
        session_id: str,
        uid: str,
        role: MessageRole,
        msg_type: MessageType,
        content: Any,
        profile_id: int,
        is_processed: bool = True,
    ) -> InternalMessage:
        # Determine attachments and final content payload
        attachments_to_save = None
        if hasattr(content, "attachments"):
            attachments_to_save = content.attachments

        obj_in_data = {
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
            "attachments": attachments_to_save,
            "profile_id": profile_id,
            "is_processed": is_processed,
        }

        db_obj = await message_crud.create(
            db,
            obj_in=obj_in_data,
        )
        return InternalMessage(
            id=db_obj.id,
            role=role,
            content=db_obj.content,
            attachments=db_obj.attachments,
            created_at=db_obj.created_at.timestamp(),
        )

    @staticmethod
    def _validate_profile_and_cfg(profile: Profile) -> ProfileConfig:
        if not profile:
            raise LLMException(message=ERR_PROFILE_NOT_FOUND)

        cfg = ProfileConfig.model_validate(profile.configs)

        if not profile.provider:
            raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

        if profile.provider.usage == ModelUsage.EMBEDDING:
            raise LLMException(message=ERR_PROVIDER_EMBEDDING_ONLY)

        return cfg

    @staticmethod
    async def _save_initial_message(
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        message: Any,
        attachments: list[str] | None,
    ) -> InternalMessage:
        # 初始保存消息 (设置 is_processed=False，锁获取后才标记 True)
        initial_msg_obj = InternalMessage(
            role=MessageRole.USER,
            content=message,
            attachments=attachments,
        )
        return await ChatDispatcher._save_message(
            db,
            session_id,
            uid,
            MessageRole.USER,
            MessageType.TEXT,
            initial_msg_obj,
            profile.id if profile and profile.id else -1,
            is_processed=False,
        )

    @staticmethod
    async def _prepare_messages(
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        cfg: ProfileConfig,
        initial_msg: InternalMessage,
        message: Any,
        is_first_iter: bool,
    ) -> list[InternalMessage]:
        # 获取上下文
        # 第一轮必须锚定在当前消息，确保上下文一致性
        # 随后的重入轮次（如果有新消息追加）则加载全部历史以包含上一轮产生的响应
        messages = await ContextManager.get_messages(
            db,
            session_id,
            uid,
            profile,
            message,
            before_id=initial_msg.id if is_first_iter else None,
        )
        if is_first_iter:
            messages.append(initial_msg)

        # 动态组装含有附件的多模态消息
        for idx, m in enumerate(messages):
            if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
                is_history = idx != len(messages) - 1
                messages[idx] = MessageAssembler.assemble(m, cfg.provider.multimodal, is_history)

        return ChatDispatcher._inject_system_prompt(profile, messages)

    @staticmethod
    def _append_new_user_messages(
        cfg: ProfileConfig,
        messages: list[InternalMessage],
        new_user_msgs: list[InternalMessage],
    ):
        for nm in new_user_msgs:
            # 确保追加的用户消息中的附件也被正确组装
            if nm.attachments or isinstance(nm.content, list):
                assembled_nm = MessageAssembler.assemble(nm, cfg.provider.multimodal, False)
                messages.append(assembled_nm)
            else:
                messages.append(nm)

    @staticmethod
    async def _handle_parallel_tool_limit(
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        cfg: ProfileConfig,
        ai_msg: InternalMessage,
        messages: list[InternalMessage],
        turn_messages: list[InternalMessage],
    ):
        error_msg = json.dumps(
            {
                "error": "parallel_limit_exceeded",
                "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(
                    requested=len(ai_msg.tool_calls), limit=cfg.tool.max_parallel_tools
                ),
            },
            ensure_ascii=False,
        )
        for tool_call in ai_msg.tool_calls:
            tool_res = InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=error_msg)
            messages.append(tool_res)
            turn_messages.append(tool_res)
            await ChatDispatcher._save_message(
                db, session_id, uid, MessageRole.TOOL, MessageType.TOOL_RESULT, tool_res, profile.id
            )

    @staticmethod
    async def _mark_initial_message_processed(db: AsyncSession, initial_msg_id: int):
        # 核心修复：拿到锁后才标记初始消息已处理，确保若进入队列，消息仍能被活跃调度器捡起
        await db.execute(update(Message).where(Message.id == initial_msg_id).values(is_processed=True))
        await db.commit()

    @staticmethod
    async def _save_assistant_message(
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile_id: int,
        ai_msg: InternalMessage,
    ):
        await ChatDispatcher._save_message(
            db,
            session_id,
            uid,
            MessageRole.ASSISTANT,
            MessageType.TOOL_CALL if ai_msg.tool_calls else MessageType.TEXT,
            ai_msg,
            profile_id,
            is_processed=True,
        )

    @staticmethod
    async def _save_tool_response(
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile_id: int,
        tool_res: InternalMessage,
        messages: list[InternalMessage],
        turn_messages: list[InternalMessage],
    ):
        messages.append(tool_res)
        turn_messages.append(tool_res)
        await ChatDispatcher._save_message(
            db, session_id, uid, MessageRole.TOOL, MessageType.TOOL_RESULT, tool_res, profile_id, is_processed=True
        )

    @staticmethod
    async def _audit_tool_call(
        db,
        profile,
        cfg,
        tool_name,
        args,
        messages=None,
        session_id: str = None,
        uid: str = None,
    ) -> str | None:
        return await AuditMiddleware.audit(
            db,
            profile,
            cfg,
            tool_name,
            args,
            session_id=session_id,
            uid=uid,
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
            session_id=session_id,
            uid=uid,
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
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
    ):
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 用户消息: {message} 附件列表: {str(attachments)}")

            # 1. 初始保存消息
            initial_msg = await ChatDispatcher._save_initial_message(db, session_id, uid, profile, message, attachments)

            final_ai_content = ""
            turn_messages: list[InternalMessage] = []
            is_first_iter = True

            # 2. 分布式会话状态机
            while True:
                await active_session_crud.cleanup_expired_locks(db)
                lock_acquired = await active_session_crud.acquire_lock(db, session_id)

                if not lock_acquired:
                    logger.bind(uid=uid, session_id=session_id).info(f"【调度器/非流】会话 {session_id} 已有活跃调度器，当前请求进入队列。")
                    return LLMResponse(
                        choices=[
                            LLMChoice(
                                message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=""),
                                finish_reason="queued",
                                created_at=time.time(),
                            )
                        ],
                        history=[],
                    ).model_dump()

                try:
                    cfg = ChatDispatcher._validate_profile_and_cfg(profile)

                    if is_first_iter:
                        await ChatDispatcher._mark_initial_message_processed(db, initial_msg.id)

                    messages = await ChatDispatcher._prepare_messages(
                        db, session_id, uid, profile, cfg, initial_msg, message, is_first_iter
                    )

                    tools = ALL_TOOLS_SCHEMAS
                    max_turns = cfg.tool.max_turns
                    current_turn = 0

                    while current_turn <= max_turns:
                        # 检查新指令并合并
                        new_user_msgs = await ChatDispatcher._fetch_and_merge_new_user_messages(db, session_id, uid)
                        if new_user_msgs:
                            logger.bind(uid=uid, session_id=session_id).info("【调度器/非流】检测到追加消息，已合并并重置轮次计数。")
                            current_turn = 0  # 重置轮次限制
                            ChatDispatcher._append_new_user_messages(cfg, messages, new_user_msgs)

                        current_turn += 1

                        if current_turn == max_turns:
                            summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                            notice_msg = InternalMessage(role=MessageRole.USER, content=summary_notice)
                            messages.append(notice_msg)
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
                            protocol=getattr(profile.provider, "protocol", "openai"),
                        )

                        ai_msg = response.message
                        logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 第 {current_turn} 轮 | LLM 响应: {ai_msg.content or '[工具调用]'}")

                        if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

                        messages.append(ai_msg)
                        turn_messages.append(ai_msg)  # 记录到增量历史

                        await ChatDispatcher._save_assistant_message(db, session_id, uid, profile.id, ai_msg)

                        if not ai_msg.tool_calls:
                            final_ai_content = ai_msg.content
                            new_user_msgs = await ChatDispatcher._fetch_and_merge_new_user_messages(db, session_id, uid)
                            if not new_user_msgs:
                                break

                            logger.bind(uid=uid, session_id=session_id).info("【调度器/非流】响应完成，但检测到追加消息，合并后继续轮询。")
                            ChatDispatcher._append_new_user_messages(cfg, messages, new_user_msgs)

                            current_turn = 0
                            continue

                        # 并行工具调用处理
                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            await ChatDispatcher._handle_parallel_tool_limit(
                                db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages
                            )
                            continue

                        tasks = [ChatDispatcher._process_single_tool(tc, db, profile, cfg, messages, username, session_id, current_turn, uid) for tc in ai_msg.tool_calls]
                        tool_responses = await asyncio.gather(*tasks)

                        for tool_res in tool_responses:
                            await ChatDispatcher._save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)

                finally:
                    await active_session_crud.release_lock(db, session_id)
                    is_first_iter = False

                # 锁释放后的“捕获检查”：防止在释放锁的瞬间有新消息到达
                new_user_msgs = await ChatDispatcher._fetch_and_merge_new_user_messages(db, session_id, uid)
                if not new_user_msgs:
                    break
                # 如果发现新消息，while True 会继续，重新竞争锁并进入下一轮处理

            return LLMResponse(
                choices=[LLMChoice(message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=final_ai_content), finish_reason=True, created_at=time.time())],
                history=[m.model_dump(exclude_none=True) for m in turn_messages],
            ).model_dump()

        except BaseBusinessException:
            raise
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).exception("调度器错误")
            raise ServerException(message=str(e))

    @staticmethod
    async def dispatch_stream(
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 用户消息: {message} 附件列表: {str(attachments)}")

            # 1. 初始保存消息
            initial_msg = await ChatDispatcher._save_initial_message(db, session_id, uid, profile, message, attachments)

            turn_messages: list[InternalMessage] = []
            is_first_iter = True

            # 2. 分布式会话状态机
            while True:
                await active_session_crud.cleanup_expired_locks(db)
                lock_acquired = await active_session_crud.acquire_lock(db, session_id)

                if not lock_acquired:
                    logger.bind(uid=uid, session_id=session_id).info(f"【调度器/流式】会话 {session_id} 已有活跃调度器，当前请求进入队列。")
                    yield {"type": "content", "content": "", "turn": 0, "finish_reason": "queued", "request_id": request_id}
                    yield {"type": "done", "session_id": session_id, "history": [], "request_id": request_id}
                    return

                try:
                    cfg = ChatDispatcher._validate_profile_and_cfg(profile)

                    if is_first_iter:
                        await ChatDispatcher._mark_initial_message_processed(db, initial_msg.id)

                    messages = await ChatDispatcher._prepare_messages(
                        db, session_id, uid, profile, cfg, initial_msg, message, is_first_iter
                    )

                    tools = ALL_TOOLS_SCHEMAS
                    max_turns = cfg.tool.max_turns
                    current_turn = 0

                    while current_turn <= max_turns:
                        # 检查新指令并合并
                        new_user_msgs = await ChatDispatcher._fetch_and_merge_new_user_messages(db, session_id, uid)
                        if new_user_msgs:
                            current_turn = 0
                            ChatDispatcher._append_new_user_messages(cfg, messages, new_user_msgs)

                        current_turn += 1

                        if current_turn == max_turns:
                            summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                            notice_msg = InternalMessage(role=MessageRole.USER, content=summary_notice)
                            messages.append(notice_msg)
                            current_tools = None
                        else:
                            current_tools = tools

                        current_tool_calls_map = {}
                        current_content_chunks = []

                        response_id = str(uuid.uuid4())

                        async for chunk in LLMClient.generate_stream(
                            api_key=profile.provider.api_key,
                            base_url=profile.provider.base_url,
                            model_id=cfg.provider.model_id,
                            messages=messages,
                            temperature=cfg.provider.temperature,
                            max_tokens=cfg.provider.max_tokens,
                            tools=current_tools,
                            protocol=getattr(profile.provider, "protocol", "openai"),
                        ):
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            choice = choices[0]
                            delta = choice.get("delta", {})

                            content = delta.get("content")
                            if content:
                                current_content_chunks.append(content)
                                yield {"type": "content", "content": content, "turn": current_turn, "response_id": response_id, "request_id": request_id}

                            tool_calls = delta.get("tool_calls")
                            if tool_calls:
                                for tc in tool_calls:
                                    idx = tc.get("index", 0)
                                    if idx not in current_tool_calls_map:
                                        current_tool_calls_map[idx] = {"id": "", "name": "", "arguments": ""}
                                    if tc.get("id"):
                                        current_tool_calls_map[idx]["id"] = tc.get("id")
                                    if tc.get("function", {}).get("name"):
                                        current_tool_calls_map[idx]["name"] = tc.get("function", {}).get("name")
                                    if tc.get("function", {}).get("arguments"):
                                        current_tool_calls_map[idx]["arguments"] += tc.get("function", {}).get("arguments")

                        final_content = "".join(current_content_chunks)
                        final_tool_calls = []
                        for idx, tc_data in sorted(current_tool_calls_map.items()):
                            if tc_data.get("name"):
                                args_dict = {}
                                if tc_data.get("arguments"):
                                    try:
                                        args_dict = json.loads(tc_data.get("arguments"))
                                    except Exception:
                                        pass
                                final_tool_calls.append(InternalToolCall(id=tc_data.get("id") or f"call_{idx}", name=tc_data.get("name"), arguments=args_dict))

                        if not final_tool_calls and not final_content.strip():
                            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

                        ai_msg = InternalMessage(role=MessageRole.ASSISTANT, content=final_content if final_content else None, tool_calls=final_tool_calls if final_tool_calls else None)

                        logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 第 {current_turn} 轮 | LLM 响应: {ai_msg.content or '[工具调用]'}")

                        messages.append(ai_msg)
                        turn_messages.append(ai_msg)

                        await ChatDispatcher._save_assistant_message(db, session_id, uid, profile.id, ai_msg)

                        if not ai_msg.tool_calls:
                            new_user_msgs = await ChatDispatcher._fetch_and_merge_new_user_messages(db, session_id, uid)
                            if not new_user_msgs:
                                break

                            ChatDispatcher._append_new_user_messages(cfg, messages, new_user_msgs)
                            current_turn = 0
                            continue

                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            await ChatDispatcher._handle_parallel_tool_limit(
                                db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages
                            )
                            continue

                        for tc in ai_msg.tool_calls:
                            yield {"type": "tool_start", "name": tc.name, "arguments": tc.arguments, "tool_call_id": tc.id, "response_id": response_id, "request_id": request_id}

                        tasks = [ChatDispatcher._process_single_tool(tc, db, profile, cfg, messages, username, session_id, current_turn, uid) for tc in ai_msg.tool_calls]
                        tool_responses = await asyncio.gather(*tasks)

                        for tool_res in tool_responses:
                            await ChatDispatcher._save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)
                            tool_name = next((tc.name for tc in ai_msg.tool_calls if tc.id == tool_res.tool_call_id), "unknown")
                            yield {"type": "tool_end", "name": tool_name, "result": tool_res.content, "tool_call_id": tool_res.tool_call_id, "response_id": response_id, "request_id": request_id}

                finally:
                    await active_session_crud.release_lock(db, session_id)
                    is_first_iter = False

                # 锁释放后的“捕获检查”
                new_user_msgs = await ChatDispatcher._fetch_and_merge_new_user_messages(db, session_id, uid)
                if not new_user_msgs:
                    break

            yield {"type": "done", "session_id": session_id, "history": [m.model_dump(exclude_none=True) for m in turn_messages], "request_id": request_id}

        except BaseBusinessException as bbe:
            yield {"type": "error", "message": bbe.message, "request_id": request_id}
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).exception("流式调度器错误")
            yield {"type": "error", "message": str(e), "request_id": request_id}
