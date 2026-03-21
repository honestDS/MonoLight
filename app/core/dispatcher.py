import json
import os
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.context import ContextManager
from app.core.exceptions import LLMException, ServerException
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor
from app.models.message import Message
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.providers.llm.client import LLMClient
from app.core.log import LogManager, get_logger
from app.core.middleware.auditor import audit_command

LogManager.setup()
logger = get_logger(__name__)
CONFIRMATION_TOKEN = "FORCE_EXECUTE_CONFIRMED"


class ChatDispatcher:
    @staticmethod
    async def _audit_tool_call(
        db: AsyncSession, profile: Profile, tool_name: str, args: dict, messages: list
    ) -> str | None:
        """内部审计逻辑：如果需要拦截则返回错误 JSON 字符串，否则返回 None"""
        if tool_name != "execute_shell":
            return None

        command = args.get("command", "")

        # --- 增强的动态令牌校验逻辑 ---
        if command.startswith(CONFIRMATION_TOKEN):
            # 校验上下文：寻找最近的一条 tool 消息，看它是否返回了 confirmation_required
            last_tool_result = None
            for m in reversed(messages):
                if m.get("role") == "tool":
                    last_tool_result = m.get("content")
                    break

            # 安全逻辑：只有上一轮执行确实被审计拦截要求确认时，当前带有令牌的指令才被视为合法
            if last_tool_result and "confirmation_required" in last_tool_result:
                return None  # 合法确认，放行
            else:
                # 否则视为非法注入或非预期调用，强制去除令牌并进入审计流程
                command = command[len(CONFIRMATION_TOKEN) :].strip()

        # 如果阈值设为 0，视为用户显式信任所有指令，直接放行
        if profile.audit_threshold == 0:
            return None

        # 如果未配置审计供应商 ID，则不执行审计，直接放行
        if not profile.audit_provider_id:
            return None

        stmt = select(ModelProvider).where(
            ModelProvider.id == profile.audit_provider_id
        )
        provider = (await db.execute(stmt)).scalars().first()
        
        # 如果配置的审计供应商不存在，为了业务连续性，执行降级放行
        if not provider:
            return None

        audit_res = await audit_command(
            command, provider.base_url, provider.api_key, profile.audit_model_id
        )
        
        # 审计系统故障（如网络超时或 API 错误）时，记录日志并返回错误，不建议降级放行以保安全
        if audit_res is None:
            return json.dumps(
                {
                    "error": "audit_system_failure",
                    "reason": "Security Audit System is currently unavailable. Please try again later.",
                },
                ensure_ascii=False,
            )

        score = audit_res.get("score", 10)
        reason = audit_res.get("reason", "Unknown")

        # 评分达到 8 分及以上：硬拦截，不允许人工确认绕过
        if score >= 8:
            return json.dumps(
                {
                    "error": "Security Blocked",
                    "reason": f"High risk score {score}: {reason}",
                },
                ensure_ascii=False,
            )
            
        # 评分达到用户设定的阈值：软拦截，要求用户手动发送带有 FORCE_EXECUTE_CONFIRMED 前缀的指令
        if score >= (profile.audit_threshold or 5):
            return json.dumps(
                {
                    "error": "confirmation_required",
                    "reason": f"Score {score}: {reason}. Re-send with prefix {CONFIRMATION_TOKEN}",
                    "risky_command": command,
                },
                ensure_ascii=False,
            )

        return None  # 审计通过，风险分低于阈值

    @staticmethod
    async def dispatch(
        db: AsyncSession, message: str, uid: str, session_id: str = "default"
    ):
        try:
            stmt = (
                select(Profile)
                .where(Profile.is_active)
                .options(selectinload(Profile.provider), selectinload(Profile.prompt))
            )
            profile = (await db.execute(stmt)).scalars().first()
            if not profile:
                raise LLMException(message="No active profile found.")

            messages = await ContextManager.get_messages(
                db, session_id, uid, profile, message
            )
            if profile.prompt and profile.prompt.content:
                messages = [m for m in messages if m.get("role") != "system"]
                messages.insert(
                    0, {"role": "system", "content": profile.prompt.content}
                )

            shell_executor = ShellExecutor(project_root=os.getcwd(), uid=uid)
            tools = [SHELL_TOOL_SCHEMA]

            db.add(
                Message(
                    session_id=session_id,
                    uid=uid,
                    role="user",
                    content=message,
                    profile_id=profile.id,
                )
            )
            await db.commit()

            max_turns, current_turn, final_ai_content = 20, 0, ""

            while current_turn < max_turns:
                current_turn += 1
                response = await LLMClient.generate(
                    api_key=profile.provider.api_key,
                    base_url=profile.provider.base_url,
                    model_id=profile.model_id,
                    messages=messages,
                    temperature=profile.temperature,
                    max_tokens=profile.max_tokens,
                    tools=tools,
                )

                message_obj = response["choices"][0]["message"]
                ai_content, tool_calls = (
                    message_obj.get("content") or "",
                    message_obj.get("tool_calls"),
                )
                messages.append(message_obj)

                db.add(
                    Message(
                        session_id=session_id,
                        uid=uid,
                        role="assistant",
                        content=json.dumps(message_obj, ensure_ascii=False)
                        if tool_calls
                        else ai_content,
                        profile_id=profile.id,
                    )
                )
                await db.commit()

                if not tool_calls:
                    final_ai_content = ai_content
                    break

                for tool_call in tool_calls:
                    tool_name = tool_call["function"]["name"]
                    args = json.loads(tool_call["function"]["arguments"])

                    # 1. 执行审计拦截（传入 messages 用于上下文校验）
                    cmd_result = await ChatDispatcher._audit_tool_call(
                        db, profile, tool_name, args, messages
                    )

                    # 2. 如果审计未拦截且是 shell 指令，则执行
                    if cmd_result is None:
                        if tool_name == "execute_shell":
                            command = args.get("command", "")
                            if command.startswith(CONFIRMATION_TOKEN):
                                command = command[len(CONFIRMATION_TOKEN) :].strip()
                            cmd_result = await shell_executor.execute(command)
                        else:
                            cmd_result = json.dumps(
                                {"error": "Unknown tool"}, ensure_ascii=False
                            )

                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": tool_name,
                        "content": cmd_result,
                    }
                    messages.append(tool_message)
                    db.add(
                        Message(
                            session_id=session_id,
                            uid=uid,
                            role="tool",
                            content=json.dumps(tool_message, ensure_ascii=False),
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
            logger.error(f"Dispatcher Error: {e}", exc_info=True)
            raise ServerException(message=str(e))
