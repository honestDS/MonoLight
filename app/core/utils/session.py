from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_LLM_EMPTY_RESPONSE, ERR_NO_VALID_CHANNEL
from app.core.crud.profile import profile_crud
from app.core.crud.session import session_crud
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.prompts import SESSION_TITLE_PROMPT
from app.core.utils.dispatcher.helpers import format_exception_message
from app.models.channel import ChannelConfig
from app.models.message import InternalMessage, MessageRole
from app.providers.database import AsyncSessionLocal
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
    raise_on_error: bool = False,
) -> str | None:
    """
    异步生成会话标题并保存到数据库

    raise_on_error 为 True 时，调用失败将向上抛出异常（供调用方做渠道降级重试）；
    为 False 时沿用旧行为，捕获异常并返回 None。
    """
    logger.bind(uid=uid, session_id=session_id).info(t("LOG_SESSION_TITLE_STARTED", uid=uid, session_id=session_id, model_id=model_id, message=first_message))
    try:
        first_message = first_message.strip()
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

        title = (response.message.content or "").strip()
        logger.bind(uid=uid, session_id=session_id).info(t("LOG_SESSION_TITLE_GENERATED", title=title))
        if title:
            # 移除可能存在的引号
            title = title.strip('"').strip("'")
            # 限制长度
            title = title[:50]

            # 这里的 db 需要重新获取，因为是异步任务
            async with AsyncSessionLocal() as db:
                await session_crud.create_or_update_title(db=db, session_id=session_id, uid=uid, title=title)
            return title

        raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

    except Exception as e:
        msg = t(e.message, default=e.message, **e.kwargs) if isinstance(e, BaseBusinessException) else str(e)
        logger.bind(uid=uid, session_id=session_id).error(t("LOG_SESSION_TITLE_FAILED", error=msg))
        if raise_on_error:
            raise
        return None


async def generate_session_title_for_active_profile(db: AsyncSession, uid: str, session_id: str, first_message: str) -> str | None:
    first_message = first_message.strip()
    if not first_message:
        return None

    try:
        session = await session_crud.get_by_session_id(db, session_id)
        if session and session.title:
            return session.title

        profile = await profile_crud.get_active(db, uid=uid)
        if not profile:
            logger.bind(uid=uid, session_id=session_id).warning(t("LOG_TITLE_CHANNEL_FAILED", error=t(ERR_NO_VALID_CHANNEL)))
            return None

        channel_cfg = (profile.configs or {}).get("channel", {})
        chat_channel_raw = channel_cfg.get("chat_channel")
        if not chat_channel_raw:
            logger.bind(uid=uid, session_id=session_id).warning(t("LOG_TITLE_CHANNEL_FAILED", error=t(ERR_NO_VALID_CHANNEL)))
            return None

        try:
            chat_channel = ChannelConfig.model_validate(chat_channel_raw)
        except Exception as exc:
            logger.bind(uid=uid, session_id=session_id).warning(t("LOG_TITLE_CHANNEL_FAILED", error=format_exception_message(exc)))
            return None

        excluded_priorities: set[int] = set()
        selection = await select_channel(db, chat_channel, "CHAT", call_context="session_title_generation", cursor_key=None)
        while selection:
            channel, model_entry, rule = selection
            try:
                return await generate_session_title(
                    uid=uid,
                    session_id=session_id,
                    first_message=first_message,
                    api_key=channel.get_decrypted_api_key(),
                    base_url=channel.base_url,
                    model_id=model_entry["model_id"],
                    protocol=getattr(channel, "protocol", "openai"),
                    max_tokens=model_entry.get("max_tokens") or 200,
                    raise_on_error=True,
                )
            except Exception as exc:
                excluded_priorities.add(rule.priority)
                logger.bind(
                    uid=uid,
                    session_id=session_id,
                    **channel_log_extra(channel, model_entry),
                ).warning(t("LOG_TITLE_CHANNEL_FAILED", error=format_exception_message(exc)))

            selection = await select_channel(db, chat_channel, "CHAT", call_context="session_title_generation_retry", excluded_priorities=excluded_priorities, cursor_key=None)

        logger.bind(uid=uid, session_id=session_id).warning(t("LOG_TITLE_CHANNEL_FAILED", error=t(ERR_NO_VALID_CHANNEL)))
    except Exception as exc:
        logger.bind(uid=uid, session_id=session_id).warning(t("LOG_TITLE_CHANNEL_FAILED", error=format_exception_message(exc)), exc_info=True)

    return None
