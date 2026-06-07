from app.core.prompts import (
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


def inject_system_prompt(
    profile: Profile,
    messages: list[InternalMessage],
) -> list[InternalMessage]:
    """
    注入系统提示词。无论是否关联 Profile Prompt，环境上下文都会注入。
    通过结构化标签隔离系统信息与环境上下文。
    """
    system_context = get_full_system_context()

    # 构造系统提示词
    context_part = SYSTEM_CONTEXT_WRAPPER.format(context=system_context)

    full_parts = [context_part]

    # 1. 如果 Profile 关联了 Prompt，则包裹后放入后续部分
    if profile.prompt and profile.prompt.content:
        instruction_part = SYSTEM_INSTRUCTIONS_WRAPPER.format(content=profile.prompt.content)
        full_parts.append(instruction_part)

    # 合并所有系统提示部分
    full_prompt = "\n\n".join(full_parts)

    # 清除原有的 System 消息并插入新的组合消息到顶部
    messages = [m for m in messages if m.role != MessageRole.SYSTEM]
    messages.insert(
        0,
        InternalMessage(
            role=MessageRole.SYSTEM,
            content=full_prompt,
        ),
    )

    # todo...此处应该将知识库信息追加到系统提示词的尾部
    return messages
