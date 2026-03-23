import json
import os
import asyncio
from typing import List, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.context import ContextManager
from app.core.exceptions import LLMException, ServerException, ParameterException
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor
from app.models.message import Message
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.models.user import User
from app.providers.llm.client import LLMClient
from app.core.log import LogManager, get_logger
from app.core.middleware.auditor import audit_command
from app.core import constants
from app.schemas.profile import ProfileConfig
from app.schemas.message import InternalMessage, MessageRole

LogManager.setup()
logger = get_logger(__name__)
CONFIRMATION_TOKEN = "FORCE_EXECUTE_CONFIRMED"


class ChatDispatcher:
    @staticmethod
    async def _audit_tool_call(
        db: AsyncSession, profile: Profile, tool_name: str, args: dict, messages: List[InternalMessage]
    ) -> str | None:
        if tool_name != "execute_shell":
            return None
        command = args.get("command", "")
        if command.startswith(CONFIRMATION_TOKEN):
            last_tool_result = None
            for m in reversed(messages):
                if m.role == MessageRole.TOOL:
                    last_tool_result = m.content
                    break
            if last_tool_result and "confirmation_required" in last_tool_result:
                return None
            else:
                command = command[len(CONFIRMATION_TOKEN) :].strip()

        cfg = ProfileConfig.model_validate(profile.configs)
        if cfg.security.audit_threshold == 0:
            return None
        if not cfg.security.audit_provider_id or cfg.security.audit_provider_id <= 0:
            return None
        stmt = select(ModelProvider).where(
            ModelProvider.id == cfg.security.audit_provider_id
        )
        provider = (await db.execute(stmt)).scalars().first()
        if not provider:
            return None
        audit_res = await audit_command(
            command, provider.base_url, provider.api_key, cfg.security.audit_model_id
        )
        if audit_res is None:
            return json.dumps(
                {
                    "error": "audit_system_failure",
                    "reason": "Security Audit System is currently unavailable.",
                },
                ensure_ascii=False,
            )
        score = audit_res.get("score", 10)
        reason = audit_res.get("reason", "Unknown")
        if score >= 8:
            return json.dumps(
                {
                    "error": "Security Blocked",
                    "reason": f"High risk score {score}: {reason}",
                },
                ensure_ascii=False,
            )
        if score >= cfg.security.audit_threshold:
            return json.dumps(
                {
                    "error": "confirmation_required",
                    "reason": f"Score {score}: {reason}. Re-send with prefix {CONFIRMATION_TOKEN}",
                    "risky_command": command,
                },
                ensure_ascii=False,
            )
        return None

    @staticmethod
    async def _process_single_tool(
        tool_call: Any,
        db: AsyncSession,
        profile: Profile,
        messages: List[InternalMessage],
        shell_executor: ShellExecutor,
        username: str,
        session_id: str,
        turn: int
    ) -> InternalMessage:
        tool_name = tool_call.name
        args = tool_call.arguments
        cmd_log = f"[{username}] (Session: {session_id}) Turn {turn} | Tool Call: {tool_name} {{command: {args.get('command', '')}}}"
        logger.info(cmd_log)

        cmd_result = await ChatDispatcher._audit_tool_call(
            db, profile, tool_name, args, messages
        )
        
        if cmd_result is None:
            if tool_name == "execute_shell":
                command = args.get("command", "")
                if command.startswith(CONFIRMATION_TOKEN):
                    command = command[len(CONFIRMATION_TOKEN) :].strip()
                cmd_result = await shell_executor.execute(command)
            else:
                cmd_result = json.dumps({"error": "Unknown tool"}, ensure_ascii=False)

        res_log = f"[{username}] (Session: {session_id}) Turn {turn} | Tool Result: {cmd_result}"
        logger.info(res_log)

        return InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=cmd_result,
        )

    @staticmethod
    async def dispatch(
        db: AsyncSession, message: str, uid: str, session_id: str = "default"
    ):
        try:
            user_stmt = select(User).where(User.uid == uid)
            user = (await db.execute(user_stmt)).scalars().first()
            username = user.username if user else "Unknown"

            stmt = (
                select(Profile)
                .where(Profile.is_active)
                .options(selectinload(Profile.provider), selectinload(Profile.prompt))
            )
            profile = (await db.execute(stmt)).scalars().first()
            if not profile:
                raise LLMException(message="No active profile found.")
            
            cfg = ProfileConfig.model_validate(profile.configs)
            messages = await ContextManager.get_messages(
                db, session_id, uid, profile, message
            )

            if profile.prompt and profile.prompt.content:
                messages = [m for m in messages if m.role != MessageRole.SYSTEM]
                messages.insert(
                    0, InternalMessage(role=MessageRole.SYSTEM, content=profile.prompt.content)
                )

            shell_executor = ShellExecutor(project_root=os.getcwd(), uid=uid)
            tools = [SHELL_TOOL_SCHEMA]

            db.add(Message(session_id=session_id, uid=uid, role="user", content=message, profile_id=profile.id))
            await db.commit()

            max_turns, current_turn, final_ai_content = 20, 0, ""

            while current_turn < max_turns:
                current_turn += 1
                response = await LLMClient.generate(
                    api_key=profile.provider.api_key,
                    base_url=profile.provider.base_url,
                    model_id=cfg.provider.model_id,
                    messages=messages,
                    temperature=cfg.provider.temperature,
                    max_tokens=cfg.provider.max_tokens,
                    tools=tools,
                    protocol=getattr(profile.provider, "protocol", "openai"),
                )

                ai_msg = response.message
                messages.append(ai_msg)

                db.add(
                    Message(
                        session_id=session_id,
                        uid=uid,
                        role="assistant",
                        content=ai_msg.model_dump_json(exclude_none=True) if ai_msg.tool_calls else ai_msg.content,
                        profile_id=profile.id,
                    )
                )
                await db.commit()

                if not ai_msg.tool_calls:
                    final_ai_content = ai_msg.content
                    break

                # 检查并行工具调用数量是否超限
                if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                    logger.warning(f"[{username}] Tool calls count ({len(ai_msg.tool_calls)}) exceeds limit ({cfg.tool.max_parallel_tools}).")
                    error_msg = json.dumps({
                        "error": "parallel_limit_exceeded",
                        "message": f"Too many parallel tool calls. Requested: {len(ai_msg.tool_calls)}, Limit: {cfg.tool.max_parallel_tools}. Please refine your request to stay within the limit."
                    }, ensure_ascii=False)
                    
                    for tool_call in ai_msg.tool_calls:
                        tool_res = InternalMessage(
                            role=MessageRole.TOOL,
                            tool_call_id=tool_call.id,
                            content=error_msg,
                        )
                        messages.append(tool_res)
                        db.add(
                            Message(
                                session_id=session_id,
                                uid=uid,
                                role="tool",
                                content=tool_res.model_dump_json(exclude_none=True),
                                profile_id=profile.id,
                            )
                        )
                    await db.commit()
                    continue

                # 并发执行工具
                tasks = [
                    ChatDispatcher._process_single_tool(
                        tc, db, profile, messages, shell_executor, username, session_id, current_turn
                    )
                    for tc in ai_msg.tool_calls
                ]
                
                tool_responses = await asyncio.gather(*tasks)
                
                for tool_res in tool_responses:
                    messages.append(tool_res)
                    db.add(
                        Message(
                            session_id=session_id,
                            uid=uid,
                            role="tool",
                            content=tool_res.model_dump_json(exclude_none=True),
                            profile_id=profile.id,
                        )
                    )
                await db.commit()

            return {
                "choices": [
                    {"message": {"role": "assistant", "content": final_ai_content}}
                ]
            }
        except Exception as e:
            logger.exception("Dispatcher Error")
            raise ServerException(message=str(e))
