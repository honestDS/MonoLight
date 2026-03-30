import asyncio
import json
import os
import time
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.constants import (
    ERR_LLM_PROVIDER_NOT_CONFIGURED,
    ERR_PROFILE_NOT_FOUND,
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
)
from app.core.tools import (
    ALL_TOOLS_SCHEMAS,
    TOOL_EXECUTOR_MAP,
)
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)
from app.providers.llm.client import (
    LLMClient,
)

LogManager.setup()
logger = get_logger(__name__)


class ChatDispatcher:
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
        cmd_log = (
            f"[{username}] (Session: {session_id}) Turn {turn} | "
            f"Tool Call: {tool_name} {{command: {args.get('command', '')}}}"
        )
        logger.info(cmd_log)

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
        res_log = f"[{username}] (Session: {session_id}) Turn {turn} | Tool Result: {cmd_result}"
        logger.info(res_log)

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
            if not profile:
                raise LLMException(message=ERR_PROFILE_NOT_FOUND)

            cfg = ProfileConfig.model_validate(profile.configs)
            messages = await ContextManager.get_messages(
                db,
                session_id,
                uid,
                profile,
                message,
            )

            if profile.prompt and profile.prompt.content:
                messages = [m for m in messages if m.role != MessageRole.SYSTEM]
                messages.insert(
                    0,
                    InternalMessage(
                        role=MessageRole.SYSTEM,
                        content=profile.prompt.content,
                    ),
                )

            tools = ALL_TOOLS_SCHEMAS
            turn_messages: list[InternalMessage] = []

            logger.info(f"[{username}] (Session: {session_id}) User Message: {message}")
            await ChatDispatcher._save_message(
                db,
                session_id,
                uid,
                MessageRole.USER,
                MessageType.TEXT,
                message,
                profile.id,
            )

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
                logger.info(
                    f"[{username}] (Session: {session_id}) Turn {current_turn} | "
                    f"LLM Response: {ai_msg.content or '[Tool Call]'}"
                )
                # 空消息拦截逻辑
                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                    from app.core.constants import (
                        ERR_LLM_EMPTY_RESPONSE,
                    )

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

            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": final_ai_content,
                        },
                        "finish_reason":True,
                        "created_at": time.time(),
                    }
                ],
                "history": [m.model_dump(exclude_none=True) for m in turn_messages],
            }
        except BaseBusinessException:
            raise
        except Exception as e:
            logger.exception("Dispatcher Error")
            raise ServerException(message=str(e))
