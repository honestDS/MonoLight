import json

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.profile.prompt import prompt_crud
from app.core.embedding.knowledge_base import build_knowledge_base_prompt_items
from app.core.log import (
    get_logger,
)
from app.core.prompts import (
    KNOWLEDGE_BASES_WRAPPER,
    LONGTERM_MEMORY_SYSTEM_PROMPT,
    SYSTEM_INSTRUCTIONS_WRAPPER,
    SYSTEM_RUNTIME_CONTEXT_POLICY,
    UNIFIED_KNOWLEDGE_BASES_WRAPPER,
)
from app.models.knowledge_base import KnowledgeBaseType
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
    *,
    include_longterm_memory: bool = True,
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

    # 长期记忆规则放在 Profile Prompt 之后，避免被普通 Profile 指令覆盖。
    profile_configs = profile.configs if isinstance(profile.configs, dict) else {}
    memory_config = profile_configs.get("memory")
    memory_enabled = isinstance(memory_config, dict) and memory_config.get("enabled") is True
    if include_longterm_memory and memory_enabled:
        full_parts.append(LONGTERM_MEMORY_SYSTEM_PROMPT)

    # 仅注入当前真正可召回的知识来源，避免提示模型调用未暴露的独立查询工具。
    try:
        profile_id = profile.id
        knowledge_bases = []
        if isinstance(profile_id, int):
            knowledge_bases = await knowledge_base_crud.list_recall_sources_by_profile(
                db,
                uid=profile.uid,
                profile_id=profile_id,
                include_managed=memory_enabled,
            )
        if not memory_enabled:
            knowledge_bases = [knowledge_base for knowledge_base in knowledge_bases if getattr(knowledge_base.knowledge_base_type, "value", knowledge_base.knowledge_base_type) == KnowledgeBaseType.USER.value]
        if knowledge_bases:
            knowledge_base_items = build_knowledge_base_prompt_items(knowledge_bases)
            # 序列化为美化后的 JSON
            knowledge_base_json = json.dumps(knowledge_base_items, ensure_ascii=False, indent=2)
            wrapper = UNIFIED_KNOWLEDGE_BASES_WRAPPER if memory_enabled else KNOWLEDGE_BASES_WRAPPER
            knowledge_base_part = wrapper.format(content=knowledge_base_json)
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
