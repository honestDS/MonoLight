"""消息组装器：按 image/audio/video_understanding 三个细分字段处理附件"""

import base64
import io
import logging
import os

from PIL import Image

from app.core.log import get_logger
from app.models.message import FilePart, ImagePart, InternalMessage, TextPart

# 屏蔽 Pillow 库的 DEBUG 日志输出，防止刷屏
logging.getLogger("PIL").setLevel(logging.WARNING)

logger = get_logger(__name__)


class MessageAssembler:
    @staticmethod
    def _compress_image_to_base64(path: str, max_size_kb: int = 500) -> str:
        """
        读取图片，如果大小超过阈值则进行压缩（降质或缩放），返回 base64 字符串
        """
        file_size_kb = os.path.getsize(path) / 1024

        with Image.open(path) as img:
            # 统一转为 RGB 以兼容 jpeg 压缩，丢弃 alpha 通道
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            if file_size_kb <= max_size_kb:
                # 够小直接读
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=95)
                return base64.b64encode(buffer.getvalue()).decode("utf-8")

            # 否则进行尺寸和质量压缩
            logger.bind().info(f"图片 {path} 大小 ({file_size_kb:.2f}KB) 超过限制，正在压缩...")

            scale_factor = (max_size_kb / file_size_kb) ** 0.5
            scale_factor = max(0.3, scale_factor)

            new_width = int(img.width * scale_factor)
            new_height = int(img.height * scale_factor)

            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)

            final_size = len(buffer.getvalue()) / 1024
            logger.bind().info(f"图片已压缩至 {new_width}x{new_height}，新大小: {final_size:.2f}KB")

            return base64.b64encode(buffer.getvalue()).decode("utf-8")

    @staticmethod
    def assemble(
        message: InternalMessage,
        image_understanding: bool = False,
        audio_understanding: bool = False,
        video_understanding: bool = False,
        is_history: bool = False,
    ) -> InternalMessage:
        """按细分模态能力处理附件。

        Args:
            message: 待组装的消息
            image_understanding: 是否支持图像理解
            audio_understanding: 是否支持音频理解
            video_understanding: 是否支持视频理解
            is_history: 是否为历史消息
        """
        if not message.attachments and isinstance(message.content, str):
            return message

        content_parts = []
        if isinstance(message.content, list):
            # 幂等保证：每个 attachment 恰好衍生一个 content_part，重复组装时
            # 剔除上一次由 attachments 衍生的尾部 parts，仅保留原始 parts，
            # 避免多次组装（prepare/降级换渠道等）导致内容累积重复。
            if message.attachments:
                original_count = max(0, len(message.content) - len(message.attachments))
                content_parts.extend(message.content[:original_count])
            else:
                content_parts.extend(message.content)
        elif isinstance(message.content, str) and message.content:
            content_parts.append(TextPart(text=message.content))

        if message.attachments:
            for attachment in message.attachments:
                ext = os.path.splitext(attachment)[1].lower()

                # 图片处理
                if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                    if is_history:
                        content_parts.append(TextPart(text="[系统提示,此处不是用户说的话][历史图片][系统提示结束]"))
                    elif image_understanding:
                        try:
                            path = attachment
                            if path.startswith("file:///"):
                                path = path[8:]
                            if os.path.exists(path):
                                encoded_string = MessageAssembler._compress_image_to_base64(path, max_size_kb=500)
                                mime_type = "image/jpeg"
                                content_parts.append(ImagePart(image_url={"url": f"data:{mime_type};base64,{encoded_string}"}))
                            else:
                                content_parts.append(TextPart(text=f"[图片丢失: {attachment}]"))
                        except Exception as e:
                            logger.bind().error(f"处理图片 {attachment} 失败: {e}")
                            content_parts.append(TextPart(text=f"[系统提示,此处不是用户说的话][图片处理失败: {attachment}][系统提示结束]"))
                    else:
                        content_parts.append(TextPart(text=f"[系统提示,此处不是用户说的话][未开启图像理解无法解析图片: {attachment}][系统提示结束]"))

                # 音频文件处理
                elif ext in [".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma"]:
                    if audio_understanding:
                        content_parts.append(FilePart(path=attachment))
                    else:
                        content_parts.append(TextPart(text=f"[系统提示,此处不是用户说的话][未开启音频理解: {attachment}][系统提示结束]"))

                # 视频文件处理
                elif ext in [".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"]:
                    if video_understanding:
                        content_parts.append(FilePart(path=attachment))
                    else:
                        content_parts.append(TextPart(text=f"[系统提示,此处不是用户说的话][未开启视频理解: {attachment}][系统提示结束]"))

                else:
                    # 其他文件
                    content_parts.append(FilePart(path=attachment))

        message.content = content_parts
        return message
