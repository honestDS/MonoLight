from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.context import (
    ContextManager,
)
from app.core.utils.dispatcher.inject_system_prompt import inject_system_prompt
from app.core.utils.dispatcher.markdown_instruction import append_session_markdown_instruction
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


async def prepare_messages(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    initial_msg: InternalMessage,
    message: Any,
    is_first_iter: bool,
) -> list[InternalMessage]:
    # 获取上下文
    # 第一轮必须锚定在当前消息，确保上下文一致性
    # 随后的重入轮次（如果有新消息追加）则加载全部历史以包含上一轮产生的响应
    messages = await ContextManager.get_messages(
        db,
        session_id,
        uid,
        profile,
        message,
        before_id=initial_msg.id if is_first_iter else None,
    )
    if is_first_iter:
        await append_session_markdown_instruction(db, session_id, initial_msg)
        messages.append(initial_msg)

    # 动态组装含有附件的多模态消息
    for idx, m in enumerate(messages):
        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
            is_history = idx != len(messages) - 1
            messages[idx] = MessageAssembler.assemble(m, cfg.provider.multimodal, is_history)

    return await inject_system_prompt(db, profile, messages)
