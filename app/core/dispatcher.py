import json, os, logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from fastapi import HTTPException

from app.models.profile import Profile
from app.models.message import Message
from app.providers.llm.client import LLMClient
from app.core.context import ContextManager
from app.core.tools.shell import ShellExecutor, SHELL_TOOL_SCHEMA

logger = logging.getLogger(__name__)

class ChatDispatcher:
    @staticmethod
    async def dispatch(db: AsyncSession, message: str, session_id: str = 'default'):
        try:
            # 1. 获取激活的 Profile
            stmt = select(Profile).where(Profile.is_active == True).options(
                selectinload(Profile.provider),
                selectinload(Profile.prompt)
            )
            profile = (await db.execute(stmt)).scalars().first()
            if not profile: raise Exception("No active profile found. Please configure and activate a profile first.")

            # 2. 获取上下文
            messages = await ContextManager.get_messages(db, session_id, profile, message)

            # 3. 系统提示词注入
            if profile.prompt and profile.prompt.content:
                if not any(m['role'] == 'system' for m in messages):
                    messages.insert(0, {'role': 'system', 'content': profile.prompt.content})

            # 4. 初始化工具
            project_root = os.getcwd() 
            shell_executor = ShellExecutor(project_root=project_root)
            tools = [SHELL_TOOL_SCHEMA]

            # 记录用户消息
            db.add(Message(session_id=session_id, role='user', content=message, profile_id=profile.id))
            await db.commit()

            # 5. Agent 循环调用逻辑
            max_turns = 10
            current_turn = 0
            final_ai_content = ""

            while current_turn < max_turns:
                current_turn += 1
                try:
                    response = await LLMClient.generate(profile, messages, tools=tools)
                except Exception as e:
                    raise Exception(f"LLM API Call Failed: {str(e)}")
                
                message_obj = response['choices'][0]['message']
                ai_content = message_obj.get('content') or ""
                tool_calls = message_obj.get('tool_calls')

                messages.append(message_obj)
                
                db.add(Message(
                    session_id=session_id, 
                    role='assistant', 
                    content=json.dumps(message_obj, ensure_ascii=False) if tool_calls else ai_content, 
                    profile_id=profile.id
                ))
                await db.commit()

                if not tool_calls:
                    final_ai_content = ai_content
                    break

                for tool_call in tool_calls:
                    if tool_call['function']['name'] == 'execute_shell':
                        try:
                            args = json.loads(tool_call['function']['arguments'])
                            cmd_result = await shell_executor.execute(args.get('command'), args.get('timeout', 30))
                        except Exception as e:
                            cmd_result = json.dumps({"error": f"Tool Execution Error: {str(e)}"}, ensure_ascii=False)
                        
                        tool_message = {
                            "role": "tool",
                            "tool_call_id": tool_call['id'],
                            "name": "execute_shell",
                            "content": cmd_result
                        }
                        messages.append(tool_message)
                        
                        db.add(Message(
                            session_id=session_id, 
                            role='tool', 
                            content=json.dumps(tool_message, ensure_ascii=False), 
                            profile_id=profile.id
                        ))
                
                await db.commit()

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": final_ai_content
                    }
                }]
            }

        except Exception as e:
            logger.error(f"Dispatcher Error: {str(e)}", exc_info=True)
            # 统一异常返回格式，模拟 OpenAI 的错误响应或直接返回错误文本
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": f"System Error: {str(e)}"
                    }
                }],
                "error": True
            }
