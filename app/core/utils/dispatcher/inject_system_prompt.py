import json

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.embedding.knowledge_base import (
    build_knowledge_base_prompt_items,
    is_embedding_profile_available,
    list_available_knowledge_bases,
)
from app.core.log import (
    get_logger,
)
from app.core.prompts import (
    KNOWLEDGE_BASES_WRAPPER,
    SYSTEM_CONTEXT_WRAPPER,
    SYSTEM_INSTRUCTIONS_WRAPPER,
)
from app.core.utils.system import get_full_system_context
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.models.profile import (
    Profile,
)

logger = get_logger(__name__)


async def build_system_prompt(
    db: AsyncSession,
    profile: Profile,
    embedding_profile_available: bool | None = None,
) -> str:
    """
    构造系统提示词字符串（不注入消息列表）。

    抽离构造逻辑，便于在上下文压缩前预先计算系统提示词的 Token 数，
    从而将其计入压缩预算，避免系统消息未被纳入窗口计算导致实际请求超限。
    """
    system_context = get_full_system_context()

    # 构造系统提示词
    context_part = SYSTEM_CONTEXT_WRAPPER.format(context=system_context)

    full_parts = [context_part]

    # 1. 如果 Profile 关联了 Prompt，则包裹后放入后续部分
    if profile.prompt and profile.prompt.content:
        instruction_part = SYSTEM_INSTRUCTIONS_WRAPPER.format(content=profile.prompt.content)
        full_parts.append(instruction_part)

    # 2. 查询该 Profile 关联的可用知识库并注入
    # 向量模型不可用（未配置或已禁用）时，知识库检索无法工作，
    # 不注入知识库目录提示词，保持与工具暴露逻辑一致。
    try:
        is_embedding_available = embedding_profile_available
        if is_embedding_available is None:
            is_embedding_available = await is_embedding_profile_available(db, profile)

        if not is_embedding_available:
            kbs = []
        else:
            kbs = await list_available_knowledge_bases(db, profile)
        if kbs:
            kb_items = build_knowledge_base_prompt_items(kbs)
            # 序列化为美化后的 JSON
            kb_json_str = json.dumps(kb_items, ensure_ascii=False, indent=2)
            kb_part = KNOWLEDGE_BASES_WRAPPER.format(content=kb_json_str)
            full_parts.append(kb_part)
    except Exception:
        # 即使查询知识库失败，也不影响正常对话
        pass

    # 合并所有系统提示部分
    return "\n\n".join(full_parts)


def inject_system_prompt_text(messages: list[InternalMessage], full_prompt: str) -> list[InternalMessage]:
    """
    将已构造的系统提示词文本注入消息列表顶部（清除原有 System 消息）。
    """
    messages = [m for m in messages if m.role != MessageRole.SYSTEM]
    messages.insert(
        0,
        InternalMessage(
            role=MessageRole.SYSTEM,
            content=full_prompt,
        ),
    )
    return messages


async def inject_system_prompt(
    db: AsyncSession,
    profile: Profile,
    messages: list[InternalMessage],
    embedding_profile_available: bool | None = None,
) -> list[InternalMessage]:
    """
    注入系统提示词。无论是否关联 Profile Prompt，环境上下文都会注入。
    通过结构化标签隔离系统信息与环境上下文。
    若 Profile 有可用知识库，在尾部注入结构化知识库目录。
    """
    full_prompt = await build_system_prompt(db, profile, embedding_profile_available=embedding_profile_available)
    return inject_system_prompt_text(messages, full_prompt)
