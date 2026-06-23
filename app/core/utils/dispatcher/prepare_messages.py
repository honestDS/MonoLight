from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.context import (
    ContextManager,
)
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt, inject_system_prompt_text
from app.core.utils.dispatcher.markdown_instruction import append_user_runtime_instruction_text, build_user_runtime_instructions
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.tokenizer import estimate_tokens
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
    context_window_k: int = 4,
    embedding_profile_available: bool | None = None,
) -> list[InternalMessage]:
    # 预先构造系统提示词并估算其 Token 数，作为压缩预算的预留量，
    # 确保系统消息被纳入上下文窗口计算，避免压缩后叠加系统词导致实际请求超限。
    system_prompt = await build_system_prompt(db, profile, embedding_profile_available=embedding_profile_available)
    user_runtime_instructions = await build_user_runtime_instructions(db, session_id) if is_first_iter else ""
    reserved_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_runtime_instructions)

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
        context_window_k=context_window_k,
        reserved_tokens=reserved_tokens,
    )
    if is_first_iter:
        current_msg = initial_msg.model_copy(deep=True)
        append_user_runtime_instruction_text(current_msg, user_runtime_instructions)
        messages.append(current_msg)

    # 动态组装含有附件的多模态消息（默认不启用多模态，由 dispatcher 在渠道选择后重新处理）
    for idx, m in enumerate(messages):
        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
            is_history = idx != len(messages) - 1
            messages[idx] = MessageAssembler.assemble(m, image_understanding=False, is_history=is_history)

    return inject_system_prompt_text(messages, system_prompt)
