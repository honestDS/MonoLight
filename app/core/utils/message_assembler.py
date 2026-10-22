"""消息组装器：按 image/audio/video_understanding 三个细分字段处理附件"""

import base64
import io
import logging
import os
from pathlib import Path
from urllib.parse import unquote_to_bytes

from PIL import Image

from app.core.constants import ERR_LLM_IMAGE_INPUT_INVALID
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import AudioPart, FilePart, ImagePart, InternalMessage, MessageRole, TextPart
from app.models.profile import ToolConfig

# 屏蔽 Pillow 库的 DEBUG 日志输出，防止刷屏
logging.getLogger("PIL").setLevel(logging.WARNING)

logger = get_logger(__name__)


class MessageAssembler:
    @staticmethod
    def _image_to_data_url(path: str, tool_config: ToolConfig) -> str:
        """Read an image and compress it when enabled and above the configured threshold."""
        image_bytes = Path(path).read_bytes()
        return MessageAssembler._image_bytes_to_data_url(image_bytes, tool_config, source=path)

    @staticmethod
    def _image_bytes_to_data_url(image_bytes: bytes, tool_config: ToolConfig, *, source: str) -> str:
        """Compress image bytes when enabled and above the configured threshold."""
        file_size_kb = len(image_bytes) / 1024

        with Image.open(io.BytesIO(image_bytes)) as img:
            if not tool_config.image_compression_enabled or len(image_bytes) <= tool_config.image_compression_threshold_kb * 1024:
                mime_type = Image.MIME[img.format]
                img.verify()
                encoded_string = base64.b64encode(image_bytes).decode("utf-8")
                return f"data:{mime_type};base64,{encoded_string}"

            logger.bind().info(t("LOG_IMAGE_COMPRESSING", path=source, file_size_kb=file_size_kb))

            scale_factor = (tool_config.image_compression_threshold_kb / file_size_kb) ** 0.5
            scale_factor = max(0.3, scale_factor)

            new_width = max(1, int(img.width * scale_factor))
            new_height = max(1, int(img.height * scale_factor))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)

            final_size = len(buffer.getvalue()) / 1024
            logger.bind().info(t("LOG_IMAGE_COMPRESSED", width=new_width, height=new_height, size_kb=final_size))

            encoded_string = base64.b64encode(buffer.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{encoded_string}"

    @staticmethod
    def prepare_inline_images(message: InternalMessage, tool_config: ToolConfig) -> InternalMessage:
        """Compress oversized inline image data URLs in the original user content."""
        if message.role != MessageRole.USER or not isinstance(message.content, list) or not tool_config.image_compression_enabled:
            return message

        attachment_part_count = message.assembled_attachment_part_count
        if attachment_part_count is None:
            attachment_part_count = len(message.attachments or [])
        original_count = max(0, len(message.content) - attachment_part_count)
        content_parts = list(message.content)
        changed = False
        threshold_bytes = tool_config.image_compression_threshold_kb * 1024

        for index, part in enumerate(message.content[:original_count]):
            if not isinstance(part, ImagePart):
                continue

            image_url = part.image_url
            url = image_url.get("url")
            if not isinstance(url, str) or not url.lower().startswith("data:image/"):
                continue

            try:
                header, separator, encoded_data = url.partition(",")
                if not separator:
                    raise ValueError

                is_base64 = any(parameter.strip().lower() == "base64" for parameter in header.split(";")[1:])
                image_bytes = unquote_to_bytes(encoded_data)
                if is_base64:
                    image_bytes = base64.b64decode(image_bytes, validate=True)
                if len(image_bytes) <= threshold_bytes:
                    continue

                compressed_url = MessageAssembler._image_bytes_to_data_url(image_bytes, tool_config, source="image_url")
            except (ValueError, OSError, Image.DecompressionBombError) as exc:
                raise ParameterException(message=ERR_LLM_IMAGE_INPUT_INVALID, cause=str(exc)) from exc

            if compressed_url != url:
                content_parts[index] = part.model_copy(update={"image_url": {**image_url, "url": compressed_url}})
                changed = True

        if not changed:
            return message

        return message.model_copy(update={"content": content_parts})

    @staticmethod
    def assemble(
        message: InternalMessage,
        image_understanding: bool = False,
        audio_understanding: bool = False,
        video_understanding: bool = False,
        is_history: bool = False,
        tool_config: ToolConfig | None = None,
    ) -> InternalMessage:
        """按细分模态能力处理附件。

        Args:
            message: 待组装的消息
            image_understanding: 是否支持图像理解
            audio_understanding: 是否支持音频理解
            video_understanding: 是否支持视频理解
            is_history: 是否为历史消息
            tool_config: 图片压缩配置
        """
        if not message.attachments and isinstance(message.content, str):
            message.assembled_attachment_part_count = 0
            return message

        content_parts = []
        if isinstance(message.content, list):
            # 仅移除明确记录的生成片段，未组装消息的原始片段全部保留。
            attachment_part_count = message.assembled_attachment_part_count
            if attachment_part_count is None:
                # 旧 checkpoint 没有记录实际生成片段数量；旧格式每个附件
                # 恰好生成一个尾部片段，因此仅在未知旧状态下使用该兼容规则。
                attachment_part_count = len(message.attachments or [])
            original_count = max(0, len(message.content) - attachment_part_count)
            content_parts.extend(message.content[:original_count])
        elif isinstance(message.content, str) and message.content:
            content_parts.append(TextPart(text=message.content))

        attachment_parts = []
        if message.attachments:
            for attachment in message.attachments:
                ext = os.path.splitext(attachment)[1].lower()

                # 图片处理
                if ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                    if is_history:
                        attachment_parts.append(TextPart(text=f"[历史图片：{attachment}]"))
                    elif image_understanding:
                        try:
                            path = attachment
                            if path.startswith("file:///"):
                                path = path[8:]
                            if os.path.exists(path):
                                image_data_url = MessageAssembler._image_to_data_url(
                                    path,
                                    tool_config if tool_config is not None else ToolConfig(),
                                )
                                attachment_parts.append(ImagePart(image_url={"url": image_data_url}))
                            else:
                                attachment_parts.append(TextPart(text=f"[图片丢失: {attachment}]"))
                        except Exception as e:
                            logger.bind().error(t("LOG_IMAGE_PROCESS_FAILED", attachment=attachment, error=str(e)))
                            attachment_parts.append(TextPart(text=f"[图片处理失败: {attachment}]"))
                    else:
                        attachment_parts.append(TextPart(text=f"[未开启图像理解无法解析图片: {attachment}]"))

                # 音频文件处理
                elif ext in [".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma"]:
                    if is_history:
                        attachment_parts.append(TextPart(text="[历史音频]"))
                    elif audio_understanding:
                        path = attachment[8:] if attachment.startswith("file:///") else attachment
                        attachment_parts.append(
                            AudioPart(
                                path=path,
                                format=os.path.splitext(path)[1].lower().lstrip("."),
                            )
                        )
                    else:
                        attachment_parts.append(TextPart(text=f"[未开启音频理解: {attachment}]"))

                # 视频文件处理
                elif ext in [".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"]:
                    if video_understanding:
                        attachment_parts.append(FilePart(path=attachment))
                    else:
                        attachment_parts.append(TextPart(text=f"[未开启视频理解: {attachment}]"))

                else:
                    # 其他文件
                    attachment_parts.append(FilePart(path=attachment))

        if attachment_parts:
            attachment_parts = [
                TextPart(text="[系统提示,此处不是用户说的话]"),
                *attachment_parts,
                TextPart(text="[系统提示结束]"),
            ]
            merged_attachment_parts = []
            for part in attachment_parts:
                if isinstance(part, TextPart) and merged_attachment_parts and isinstance(merged_attachment_parts[-1], TextPart):
                    merged_attachment_parts[-1].text += part.text
                else:
                    merged_attachment_parts.append(part)
            attachment_parts = merged_attachment_parts
            content_parts.extend(attachment_parts)

        message.assembled_attachment_part_count = len(attachment_parts)

        message.content = content_parts
        return message
