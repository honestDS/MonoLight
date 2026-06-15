import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import (
    Any,
    MutableSet,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.constants import (
    ERR_CHAT_PROVIDER_NOT_FOUND,
    ERR_LLM_EMPTY_RESPONSE,
)
from app.core.crud.active_session import (
    active_session_crud,
)
from app.core.crud.profile import (
    profile_crud,
)
from app.core.crud.provider import provider_crud

# CRUD Imports
from app.core.crud.user import user_crud
from app.core.exceptions import (
    BaseBusinessException,
    LLMException,
    ServerException,
)
from app.core.i18n import t
from app.core.log import (
    get_logger,
)
from app.core.prompts import (
    PROMPT_MAX_TURNS_REACHED,
)
from app.core.tools import get_tools_for_profile
from app.core.utils.dispatcher.append_new_user_messages import append_new_user_messages
from app.core.utils.dispatcher.fetch_and_merge_new_user_messages import fetch_and_merge_new_user_messages
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.mark_initial_message_processed import mark_initial_message_processed
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.process_single_tool import process_single_tool
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_initial_message import save_initial_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
)
from app.providers.llm.client import (
    LLMClient,
)
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)


class ChatDispatcher:
    @staticmethod
    async def dispatch(
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ):
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 用户消息: {message} 附件列表: {str(attachments)}")

            # 1. 初始保存消息
            initial_msg = await save_initial_message(db, session_id, uid, profile, message, attachments)

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
                    cfg = await validate_profile_and_cfg(db, profile)

                    if is_first_iter:
                        await mark_initial_message_processed(db, initial_msg.id)

                    messages = await prepare_messages(db, session_id, uid, profile, cfg, initial_msg, message, is_first_iter)

                    chat_provider = await provider_crud.get(db, cfg.provider.provider_id)
                    if not chat_provider:
                        raise LLMException(message=ERR_CHAT_PROVIDER_NOT_FOUND)

                    tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile)
                    max_turns = cfg.tool.max_turns
                    current_turn = 0

                    while current_turn <= max_turns:
                        # 检查新指令并合并
                        new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                        if new_user_msgs:
                            logger.bind(uid=uid, session_id=session_id).info("【调度器/非流】检测到追加消息，已合并并重置轮次计数。")
                            current_turn = 0  # 重置轮次限制
                            append_new_user_messages(cfg, messages, new_user_msgs)

                        current_turn += 1

                        if current_turn == max_turns:
                            summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                            notice_msg = InternalMessage(role=MessageRole.USER, content=summary_notice)
                            messages.append(notice_msg)
                            current_tools = None
                        else:
                            current_tools = tools

                        response = await LLMClient.generate(
                            api_key=chat_provider.api_key,
                            base_url=chat_provider.base_url,
                            model_id=cfg.provider.model_id,
                            messages=messages,
                            temperature=cfg.provider.temperature,
                            max_tokens=cfg.provider.max_tokens,
                            tools=current_tools,
                            protocol=getattr(chat_provider, "protocol", "openai"),
                            timeout=cfg.provider.chat_timeout,
                        )

                        ai_msg = response.message
                        logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 第 {current_turn} 轮 | LLM 响应: {ai_msg.content or '[工具调用]'}")

                        if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

                        messages.append(ai_msg)
                        turn_messages.append(ai_msg)  # 记录到增量历史

                        # save_assistant_message 内部会对 ai_msg 引用进行 Markdown 洗理，因此其 content 会被更新
                        await save_assistant_message(db, session_id, uid, profile.id, ai_msg)

                        if not ai_msg.tool_calls:
                            final_ai_content = ai_msg.content
                            new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                            if not new_user_msgs:
                                break

                            logger.bind(uid=uid, session_id=session_id).info("【调度器/非流】响应完成，但检测到追加消息，合并后继续轮询。")
                            append_new_user_messages(cfg, messages, new_user_msgs)

                            current_turn = 0
                            continue

                        # 并行工具调用处理
                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            await handle_parallel_tool_limit(db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages)
                            continue

                        # 使用信号量控制并发执行数，确保同步和异步工具都受 executor_max_workers 约束
                        sem = asyncio.Semaphore(cfg.tool.executor_max_workers)

                        async def wrapped_tool_call(tc):
                            async with sem:
                                # 只有获取信号量后，才创建并运行实际的任务，以确保并发控制生效
                                task = asyncio.create_task(
                                    process_single_tool(
                                        tc,
                                        db,
                                        profile,
                                        cfg,
                                        messages,
                                        username,
                                        session_id,
                                        current_turn,
                                        uid,
                                        allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                                    )
                                )

                                if active_tasks is not None:
                                    active_tasks.add(task)

                                try:
                                    return await task
                                finally:
                                    if active_tasks is not None:
                                        active_tasks.discard(task)

                        tasks = [wrapped_tool_call(tc) for tc in ai_msg.tool_calls]
                        tool_responses = await asyncio.gather(*tasks)

                        for tool_res in tool_responses:
                            await save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)

                finally:
                    await active_session_crud.release_lock(db, session_id)
                    is_first_iter = False

                # 锁释放后的“捕获检查”：防止在释放锁的瞬间有新消息到达
                new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
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
            logger.bind(uid=uid, session_id=session_id).error("调度器错误", exc_info=True)
            raise ServerException(message=str(e))

    @staticmethod
    async def dispatch_stream(
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        request_id: str | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        try:
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_active(db)

            logger.bind(uid=uid, session_id=session_id).info(f"[{username}] 用户消息: {message} 附件列表: {str(attachments)}")

            # 1. 初始保存消息
            initial_msg = await save_initial_message(db, session_id, uid, profile, message, attachments)

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
                    # 获取到锁并进入调度流程时，发送任务开始广播（供前端清空 queued 视觉效果）
                    yield {"type": "task_start", "request_id": request_id}

                    cfg = await validate_profile_and_cfg(db, profile)

                    if is_first_iter:
                        await mark_initial_message_processed(db, initial_msg.id)

                    messages = await prepare_messages(db, session_id, uid, profile, cfg, initial_msg, message, is_first_iter)

                    chat_provider = await provider_crud.get(db, cfg.provider.provider_id)
                    if not chat_provider:
                        raise LLMException(message=ERR_CHAT_PROVIDER_NOT_FOUND)

                    tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile)
                    max_turns = cfg.tool.max_turns
                    current_turn = 0

                    while current_turn <= max_turns:
                        # 检查新指令并合并
                        new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                        if new_user_msgs:
                            current_turn = 0
                            append_new_user_messages(cfg, messages, new_user_msgs)

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
                            api_key=chat_provider.api_key,
                            base_url=chat_provider.base_url,
                            model_id=cfg.provider.model_id,
                            messages=messages,
                            temperature=cfg.provider.temperature,
                            max_tokens=cfg.provider.max_tokens,
                            tools=current_tools,
                            protocol=getattr(chat_provider, "protocol", "openai"),
                            timeout=cfg.provider.chat_timeout,
                        ):
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            choice = choices[0]
                            delta = choice.get("delta", {})

                            content = delta.get("content")
                            if content:
                                current_content_chunks.append(content)
                                yield {
                                    "type": "content",
                                    "content": content,
                                    "turn": current_turn,
                                    "response_id": response_id,
                                    "request_id": request_id,
                                    "session_id": session_id,
                                }

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

                        saved_msg = await save_assistant_message(db, session_id, uid, profile.id, ai_msg)

                        # 向前端推送当前轮次结束以及清洗后的最终内容，供前端通过 response_id 覆盖
                        if saved_msg:
                            yield {
                                "type": "turn_end",
                                "response_id": response_id,
                                "content": saved_msg.content,
                                "request_id": request_id,
                                "session_id": session_id,
                            }

                        if not ai_msg.tool_calls:
                            new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                            if not new_user_msgs:
                                break

                            append_new_user_messages(cfg, messages, new_user_msgs)
                            current_turn = 0
                            continue

                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            await handle_parallel_tool_limit(db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages)
                            continue

                        for tc in ai_msg.tool_calls:
                            yield {
                                "type": "tool_start",
                                "name": tc.name,
                                "arguments": tc.arguments,
                                "tool_call_id": tc.id,
                                "response_id": response_id,
                                "request_id": request_id,
                                "session_id": session_id,
                            }

                        # 使用信号量控制并发执行数，确保同步和异步工具都受 executor_max_workers 约束
                        sem = asyncio.Semaphore(cfg.tool.executor_max_workers)

                        async def wrapped_tool_call(tc):
                            async with sem:
                                # 只有获取信号量后，才创建并运行实际的任务，以确保并发控制生效
                                task = asyncio.create_task(
                                    process_single_tool(
                                        tc,
                                        db,
                                        profile,
                                        cfg,
                                        messages,
                                        username,
                                        session_id,
                                        current_turn,
                                        uid,
                                        allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                                    )
                                )
                                
                                if active_tasks is not None:
                                    active_tasks.add(task)
                                
                                try:
                                    return await task
                                finally:
                                    if active_tasks is not None:
                                        active_tasks.discard(task)

                        tasks = [wrapped_tool_call(tc) for tc in ai_msg.tool_calls]
                        tool_responses = await asyncio.gather(*tasks)

                        for tool_res in tool_responses:
                            await save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)
                            tool_name = next((tc.name for tc in ai_msg.tool_calls if tc.id == tool_res.tool_call_id), "unknown")
                            yield {
                                "type": "tool_end",
                                "name": tool_name,
                                "result": tool_res.content,
                                "tool_call_id": tool_res.tool_call_id,
                                "response_id": response_id,
                                "request_id": request_id,
                                "session_id": session_id,
                            }

                finally:
                    await active_session_crud.release_lock(db, session_id)
                    is_first_iter = False

                # 锁释放后的“捕获检查”
                new_user_msgs = await fetch_and_merge_new_user_messages(db, session_id, uid)
                if not new_user_msgs:
                    break

            yield {"type": "done", "session_id": session_id, "history": [m.model_dump(exclude_none=True) for m in turn_messages], "request_id": request_id}

        except BaseBusinessException as bbe:
            yield {"type": "error", "message": t(bbe.message, **bbe.kwargs), "request_id": request_id}
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error("流式调度器错误", exc_info=True)
            yield {"type": "error", "message": str(e), "request_id": request_id}
