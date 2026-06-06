import base64
import io
import os
from typing import Any
from PIL import Image
from app.models.message import (
    InternalMessage,
    TextPart,
    ImagePart,
    FilePart
)
import logging

# 屏蔽 Pillow 库的 DEBUG 日志输出，防止刷屏
logging.getLogger("PIL").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

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
                # 尽量保持原格式，如果是 RGB 最好用 JPEG 以缩小体积
                img.save(buffer, format="JPEG", quality=95)
                return base64.b64encode(buffer.getvalue()).decode('utf-8')
                
            # 否则进行尺寸和质量压缩
            logger.info(f"Image {path} size ({file_size_kb:.2f}KB) exceeds limit, compressing...")
            
            # 缩放系数
            scale_factor = (max_size_kb / file_size_kb) ** 0.5
            # 防止过度缩放导致图片无法识别，最多缩小到原尺寸的 0.3
            scale_factor = max(0.3, scale_factor)
            
            new_width = int(img.width * scale_factor)
            new_height = int(img.height * scale_factor)
            
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # 以 JPEG 格式并降低质量进行保存
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=80, optimize=True)
            
            final_size = len(buffer.getvalue()) / 1024
            logger.info(f"Image compressed to {new_width}x{new_height}, new size: {final_size:.2f}KB")
            
            return base64.b64encode(buffer.getvalue()).decode('utf-8')

    @staticmethod
    def assemble(message: InternalMessage, multimodal_enabled: bool = False, is_history: bool = False) -> InternalMessage:
        logger.info(f"MessageAssembler triggered for msg {message.id}. attachments: {message.attachments}, multimodal: {multimodal_enabled}, is_history: {is_history}")
        
        if not message.attachments and isinstance(message.content, str):
            return message

        content_parts = []
        if isinstance(message.content, list):
            content_parts.extend(message.content)
        elif isinstance(message.content, str) and message.content:
            content_parts.append(TextPart(text=message.content))

        if message.attachments:
            for attachment in message.attachments:
                ext = os.path.splitext(attachment)[1].lower()
                logger.info(f"Processing attachment: {attachment}, ext: {ext}")
                if ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
                    if is_history:
                        # 对于历史消息，为节省 Token 直接以文本占位符发送
                        content_parts.append(TextPart(text=f"[历史图片]"))
                    elif multimodal_enabled:
                        try:
                            # If it's a local path
                            path = attachment
                            if path.startswith("file:///"):
                                path = path[8:]
                            if os.path.exists(path):
                                encoded_string = MessageAssembler._compress_image_to_base64(path, max_size_kb=500)
                                # 压缩后强制使用 image/jpeg 类型
                                mime_type = "image/jpeg"
                                
                                content_parts.append(ImagePart(
                                    image_url={
                                        "url": f"data:{mime_type};base64,{encoded_string}"
                                    }
                                ))
                            else:
                                content_parts.append(TextPart(text=f"[图片丢失: {attachment}]"))
                        except Exception as e:
                            logger.error(f"Failed to process image {attachment}: {e}")
                            content_parts.append(TextPart(text=f"[图片处理失败: {attachment}]"))
                    else:
                        content_parts.append(TextPart(text=f"[未开启多模态无法解析图片: {attachment}]"))
                else:
                    # Non-image file
                    content_parts.append(FilePart(path=attachment))

        message.content = content_parts
        return message
