import json

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crud.prompt import prompt_crud
from app.core.embedding.knowledge_base import (
    build_knowledge_base_prompt_items,
    list_available_knowledge_bases,
)
from app.core.log import (
    get_logger,
)
from app.core.prompts import (
    KNOWLEDGE_BASES_WRAPPER,
    SYSTEM_INSTRUCTIONS_WRAPPER,
    SYSTEM_RUNTIME_CONTEXT_POLICY,
)
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
) -> str:
    """
    构造系统提示词字符串（不注入消息列表）。

    抽离构造逻辑，便于在上下文压缩前预先计算系统提示词的 Token 数，
    从而将其计入压缩预算，避免系统消息未被纳入窗口计算导致实际请求超限。
    """
    full_parts = [SYSTEM_RUNTIME_CONTEXT_POLICY]

    # 1. 如果 Profile 业务关联了 Prompt，则包裹后放入后续部分
    if profile.prompt_id:
        prompt = await prompt_crud.get_visible(db, profile.prompt_id, uid=profile.uid)
        if prompt and prompt.content:
            instruction_part = SYSTEM_INSTRUCTIONS_WRAPPER.format(content=prompt.content)
            full_parts.append(instruction_part)

    # 2. 查询该 Profile 关联的可用知识库并注入
    try:
        knowledge_bases = await list_available_knowledge_bases(db, profile)
        if knowledge_bases:
            knowledge_base_items = build_knowledge_base_prompt_items(knowledge_bases)
            # 序列化为美化后的 JSON
            knowledge_base_json = json.dumps(knowledge_base_items, ensure_ascii=False, indent=2)
            knowledge_base_part = KNOWLEDGE_BASES_WRAPPER.format(content=knowledge_base_json)
            full_parts.append(knowledge_base_part)
    except Exception:
        # 即使查询知识库失败，也不影响正常对话
        pass

    # 合并所有系统提示部分
    return "\n\n".join(full_parts)


def inject_system_prompt_text(messages: list[InternalMessage], full_prompt: str) -> list[InternalMessage]:
    """
    将已构造的系统提示词文本注入消息列表顶部（清除原有 System 消息）。
    """
    non_system_messages = []
    for message in messages:
        if message.role != MessageRole.SYSTEM:
            non_system_messages.append(message)

    if full_prompt.strip():
        non_system_messages.insert(
            0,
            InternalMessage(
                role=MessageRole.SYSTEM,
                content=full_prompt,
            ),
        )
    return non_system_messages


async def inject_system_prompt(
    db: AsyncSession,
    profile: Profile,
    messages: list[InternalMessage],
) -> list[InternalMessage]:
    """
    注入系统提示词。
    若 Profile 有可用知识库，在尾部注入结构化知识库目录。
    """
    full_prompt = await build_system_prompt(db, profile)
    return inject_system_prompt_text(messages, full_prompt)
