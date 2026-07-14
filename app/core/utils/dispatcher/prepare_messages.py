from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.context import (
    ContextManager,
)
from app.core.utils.context_summary.service import get_context_summary_state
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
    initial_msg: InternalMessage | None,
    message: Any,
    is_first_iter: bool,
    context_window_k: int = 4,
    max_tokens: int = 0,
    tools: list[dict] | None = None,
    history_before_id: int | None = None,
) -> list[InternalMessage]:
    # 预先构造系统提示词并估算其 Token 数，作为历史消息加载的预留量。
    # 总结阈值与硬窗口由模型调用前的统一检查点按完整请求重新计算。
    system_prompt = await build_system_prompt(db, profile)
    user_runtime_instructions = await build_user_runtime_instructions(db, session_id) if is_first_iter else ""
    current_msg = initial_msg.model_copy(deep=True) if is_first_iter and initial_msg is not None else None
    if current_msg is not None:
        append_user_runtime_instruction_text(current_msg, user_runtime_instructions)
        if current_msg.attachments or isinstance(current_msg.content, list):
            current_msg = MessageAssembler.assemble(
                current_msg,
                image_understanding=False,
                is_history=False,
            )
    history_before_id = history_before_id if is_first_iter and history_before_id is not None else initial_msg.id if is_first_iter else None
    summary_state = await get_context_summary_state(
        db,
        session_id=session_id,
        uid=uid,
    )
    summary_message = summary_state.as_message()
    reserved_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_runtime_instructions)
    if summary_message is not None:
        reserved_tokens += estimate_tokens(str(summary_message.content or ""))

    # 获取上下文
    # 第一轮必须锚定在当前消息，确保上下文一致性
    # 随后的重入轮次（如果有新消息追加）则加载全部历史以包含上一轮产生的响应
    messages = await ContextManager.get_messages(
        db,
        session_id,
        uid,
        profile,
        message,
        before_id=history_before_id,
        after_id=summary_state.message_id,
        context_window_k=context_window_k,
        reserved_tokens=reserved_tokens,
    )
    if summary_message is not None:
        messages.insert(0, summary_message)
    if current_msg is not None:
        messages.append(current_msg)

    # 动态组装含有附件的多模态消息（默认不启用多模态，由 dispatcher 在渠道选择后重新处理）
    for idx, m in enumerate(messages):
        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
            is_history = idx != len(messages) - 1
            messages[idx] = MessageAssembler.assemble(m, image_understanding=False, is_history=is_history)

    return inject_system_prompt_text(messages, system_prompt)
