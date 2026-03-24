import json
import os
import asyncio
from typing import List, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.context import ContextManager
from app.core.exceptions import BaseBusinessException, LLMException, ServerException
from app.core.constants import *
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor
from app.models.profile import Profile, ProfileConfig
from app.models.message import MessageRole, MessageType, InternalMessage
from app.providers.llm.client import LLMClient
from app.core.log import LogManager, get_logger
from app.core.middleware.auditor import audit_command

# CRUD Imports
from app.core.crud.user import user_crud
from app.core.crud.provider import provider_crud
from app.core.crud.profile import profile_crud
from app.core.crud.message import message_crud

LogManager.setup()
logger = get_logger(__name__)
CONFIRMATION_TOKEN = "FORCE_EXECUTE_CONFIRMED"


class ChatDispatcher:
    @staticmethod
    async def _audit_tool_call(
        db: AsyncSession,
        profile: Profile,
        cfg: ProfileConfig,
        tool_name: str,
        args: dict,
        messages: List[InternalMessage],
    ) -> str | None:
        logger.debug(f"Auditing tool call: {tool_name}")

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

        if cfg.security.audit_threshold == 0:
            logger.debug("Audit threshold is 0, skipping audit.")
            return None
        if not cfg.security.audit_provider_id or cfg.security.audit_provider_id <= 0:
            logger.debug("Audit provider ID not configured, skipping audit.")
            return None

        provider = await provider_crud.get(db, cfg.security.audit_provider_id)
        if not provider:
            logger.debug(
                f"Audit provider {cfg.security.audit_provider_id} not found in DB."
            )
            return None

        logger.debug(f"Executing security audit for command: {command[:50]}...")
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
        logger.debug(f"Audit Result - Score: {score}, Reason: {reason}")

        if score >= 8:
            return json.dumps(
                {
                    "error": "Security Blocked",
                    "reason": f"High risk score {score}: Security Blocked",
                },
                ensure_ascii=False,
            )
        if score >= cfg.security.audit_threshold:
            return json.dumps(
                {
                    "error": "confirmation_required",
                    "reason": f"Score {score}: Re-send with prefix {CONFIRMATION_TOKEN}. You need to request a second confirmation from the user, and if the user confirms, you need to re-execute the command and add before the command: {CONFIRMATION_TOKEN}",
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
        cfg: ProfileConfig,
        messages: List[InternalMessage],
        shell_executor: ShellExecutor,
        username: str,
        session_id: str,
        turn: int,
    ) -> InternalMessage:
        tool_name = tool_call.name
        args = tool_call.arguments
        cmd_log = f"[{username}] (Session: {session_id}) Turn {turn} | Tool Call: {tool_name} {{command: {args.get('command', '')}}}"
        logger.info(cmd_log)

        cmd_result = await ChatDispatcher._audit_tool_call(
            db, profile, cfg, tool_name, args, messages
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
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"

            profile = await profile_crud.get_active(db)
            if not profile:
                raise LLMException(message=ERR_PROFILE_NOT_FOUND)

            cfg = ProfileConfig.model_validate(profile.configs)
            messages = await ContextManager.get_messages(
                db, session_id, uid, profile, message
            )

            if profile.prompt and profile.prompt.content:
                messages = [m for m in messages if m.role != MessageRole.SYSTEM]
                messages.insert(
                    0,
                    InternalMessage(
                        role=MessageRole.SYSTEM, content=profile.prompt.content
                    ),
                )

            shell_executor = ShellExecutor(project_root=os.getcwd(), uid=uid)
            tools = [SHELL_TOOL_SCHEMA]

            await message_crud.create(
                db,
                obj_in={
                    "session_id": session_id,
                    "uid": uid,
                    "role": MessageRole.USER,
                    "type": MessageType.TEXT,
                    "content": message,
                    "profile_id": profile.id,
                },
            )

            max_turns, current_turn, final_ai_content = 20, 0, ""
            if not profile.provider:
                raise LLMException(message=ERR_LLM_PROVIDER_NOT_CONFIGURED)

            while current_turn < max_turns:
                current_turn += 1
                await db.execute(select(1))

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

                await message_crud.create(
                    db,
                    obj_in={
                        "session_id": session_id,
                        "uid": uid,
                        "role": MessageRole.ASSISTANT,
                        "type": MessageType.TOOL_CALL
                        if ai_msg.tool_calls
                        else MessageType.TEXT,
                        "content": ai_msg.model_dump_json(exclude_none=True)
                        if ai_msg.tool_calls
                        else ai_msg.content,
                        "profile_id": profile.id,
                    },
                )

                if not ai_msg.tool_calls:
                    final_ai_content = ai_msg.content
                    break

                if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                    error_msg = json.dumps(
                        {
                            "error": "parallel_limit_exceeded",
                            "message": f"Too many parallel tool calls. Requested: {len(ai_msg.tool_calls)}, Limit: {cfg.tool.max_parallel_tools}.",
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
                        await message_crud.create(
                            db,
                            obj_in={
                                "session_id": session_id,
                                "uid": uid,
                                "role": MessageRole.TOOL,
                                "type": MessageType.TOOL_RESULT,
                                "content": tool_res.model_dump_json(exclude_none=True),
                                "profile_id": profile.id,
                            },
                        )
                    continue

                tasks = [
                    ChatDispatcher._process_single_tool(
                        tc,
                        db,
                        profile,
                        cfg,
                        messages,
                        shell_executor,
                        username,
                        session_id,
                        current_turn,
                    )
                    for tc in ai_msg.tool_calls
                ]

                tool_responses = await asyncio.gather(*tasks)

                for tool_res in tool_responses:
                    messages.append(tool_res)
                    await message_crud.create(
                        db,
                        obj_in={
                            "session_id": session_id,
                            "uid": uid,
                            "role": MessageRole.TOOL,
                            "type": MessageType.TOOL_RESULT,
                            "content": tool_res.model_dump_json(exclude_none=True),
                            "profile_id": profile.id,
                        },
                    )

            return {
                "choices": [
                    {"message": {"role": "assistant", "content": final_ai_content}}
                ]
            }
        except BaseBusinessException:
            raise
        except Exception as e:
            logger.exception("Dispatcher Error")
            raise ServerException(message=str(e))
