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
    image_understanding: bool = False,
    audio_understanding: bool = False,
    video_understanding: bool = False,
):
    for nm in new_user_msgs:
        # 确保追加的用户消息中的附件也被正确组装
        if nm.attachments or isinstance(nm.content, list):
            assembled_nm = MessageAssembler.assemble(
                nm,
                image_understanding=image_understanding,
                audio_understanding=audio_understanding,
                video_understanding=video_understanding,
                is_history=False,
            )
            messages.append(assembled_nm)
        else:
            messages.append(nm)
