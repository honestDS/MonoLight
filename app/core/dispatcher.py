import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import constants
from app.core.context import ContextManager
from app.core.exceptions import LLMException, ServerException
from app.core.tools.shell import SHELL_TOOL_SCHEMA, ShellExecutor
from app.models.message import Message
from app.models.profile import Profile
from app.providers.llm.client import LLMClient

logger = logging.getLogger(__name__)


class ChatDispatcher:
    @staticmethod
    async def dispatch(
        db: AsyncSession, message: str, uid: str, session_id: str = "default"
    ):
        try:
            # 1. 获取激活的 Profile
            stmt = (
                select(Profile)
                .where(Profile.is_active)
                .options(selectinload(Profile.provider), selectinload(Profile.prompt))
            )
            profile = (await db.execute(stmt)).scalars().first()
            if not profile:
                raise LLMException(message="No active profile found. Please configure and activate a profile first.")

            # 强一致性校验：确保 Provider 关系已加载且存在
            if not profile.provider:
                from app.core import constants as app_constants

                raise LLMException(message=app_constants.ERR_PROFILE_PROVIDER_MISMATCH)

            # 2. 获取上下文
            messages = await ContextManager.get_messages(
                db, session_id, uid, profile, message
            )

            # 3. 系统提示词注入
            if profile.prompt and profile.prompt.content:
                # 确保系统提示词始终位于首位且不重复
                messages = [m for m in messages if m.get("role") != "system"]
                messages.insert(
                    0, {"role": "system", "content": profile.prompt.content}
                )
            project_root = os.getcwd()
            shell_executor = ShellExecutor(project_root=project_root)
            tools = [SHELL_TOOL_SCHEMA]

            # 记录用户消息
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

            # 5. Agent 循环调用逻辑
            max_turns = 10
            current_turn = 0
            final_ai_content = ""

            while current_turn < max_turns:
                current_turn += 1
                try:
                    response = await LLMClient.generate(
                        api_key=profile.provider.api_key,
                        base_url=profile.provider.base_url,
                        model_id=profile.model_id,
                        messages=messages,
                        temperature=profile.temperature,
                        max_tokens=profile.max_tokens,
                        tools=tools,
                    )
                except Exception as e:
                    error_msg = str(e)
                    if "'NoneType' object has no attribute 'api_key'" in error_msg:
                        raise LLMException(message=constants.ERR_LLM_PROVIDER_NOT_CONFIGURED)
                    raise LLMException(message=f"大模型接口调用失败: {error_msg}")

                message_obj = response["choices"][0]["message"]
                ai_content = message_obj.get("content") or ""
                tool_calls = message_obj.get("tool_calls")

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
                    if tool_call["function"]["name"] == "execute_shell":
                        try:
                            args = json.loads(tool_call["function"]["arguments"])
                            cmd_result = await shell_executor.execute(
                                args.get("command"), args.get("timeout", 30)
                            )
                        except Exception as e:
                            cmd_result = json.dumps(
                                {"error": f"Tool Execution Error: {str(e)}"},
                                ensure_ascii=False,
                            )

                        tool_message = {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": "execute_shell",
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
            error_msg = str(e)
            logger.error(f"Dispatcher Error: {error_msg}", exc_info=True)
            if isinstance(e, (LLMException, ServerException)):
                raise e
            # 将原始异常消息透传给 ServerException
            raise ServerException(message=error_msg)
