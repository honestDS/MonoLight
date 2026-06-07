from app.core.utils.message_assembler import MessageAssembler
from app.models.message import (
    InternalMessage,
)
from app.models.profile import (
    ProfileConfig,
)


def append_new_user_messages(
    cfg: ProfileConfig,
    messages: list[InternalMessage],
    new_user_msgs: list[InternalMessage],
):
    for nm in new_user_msgs:
        # 确保追加的用户消息中的附件也被正确组装
        if nm.attachments or isinstance(nm.content, list):
            assembled_nm = MessageAssembler.assemble(nm, cfg.provider.multimodal, False)
            messages.append(assembled_nm)
        else:
            messages.append(nm)
