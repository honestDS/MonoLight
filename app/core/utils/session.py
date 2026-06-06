from app.core.crud.session import session_crud
from app.core.log import get_logger
from app.core.prompts import SESSION_TITLE_PROMPT
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


async def generate_session_title(
    uid: str,
    session_id: str,
    first_message: str,
    api_key: str,
    base_url: str,
    model_id: str,
    protocol: str = "openai",
    max_tokens: int = 200,
) -> str | None:
    """
    异步生成会话标题并保存到数据库
    """
    logger.bind(uid=uid, session_id=session_id).info(f"开始生成会话标题任务: uid={uid}, session_id={session_id}, model={model_id}, 用户消息={first_message}")
    try:
        if first_message == "":
            return "无标题"

        # 构造起名专用消息列表
        messages = [InternalMessage(role=MessageRole.USER, content=SESSION_TITLE_PROMPT.format(message=first_message))]

        # 调用 LLM 生成标题
        response = await LLMClient.generate(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=0.3,  # 较低随机性以获得更准确的标题
            max_tokens=max_tokens,
            protocol=protocol,
        )

        title = response.message.content.strip()
        logger.bind(uid=uid, session_id=session_id).info(f"LLM 成功生成会话标题: {title}")
        if title:
            # 移除可能存在的引号
            title = title.strip('"').strip("'")
            # 限制长度
            title = title[:50]

            # 这里的 db 需要重新获取，因为是异步任务
            from app.providers.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                await session_crud.create_or_update_title(db=db, session_id=session_id, uid=uid, title=title)
            return title

        return None

    except Exception as e:
        logger.bind(uid=uid, session_id=session_id).error(f"生成会话标题失败: {e}")
        return None
