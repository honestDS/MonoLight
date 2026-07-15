from __future__ import annotations

import base64
import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from app.adapters.weixin_openclaw.client import WeixinOpenClawClient
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.adapters.weixin_openclaw.constants import (
    FILE_ITEM_TYPE,
    FILE_UPLOAD_TYPE,
    IMAGE_EXTENSIONS,
    IMAGE_ITEM_TYPE,
    IMAGE_UPLOAD_TYPE,
)
from app.adapters.weixin_openclaw.crypto import (
    aes_ecb_decrypt,
    aes_ecb_encrypt,
    aes_padded_size,
    parse_media_aes_key,
    pkcs7_pad,
    pkcs7_unpad,
)
from app.core.constants import (
    ERR_WEIXIN_OPENCLAW_INBOUND_MEDIA_TOO_LARGE,
    ERR_WEIXIN_OPENCLAW_UPLOAD_URL_MISSING,
)
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import TEMP_DIR
from app.core.tools.send_file_to_user import resolve_file_token

logger = get_logger(__name__)


class WeixinOpenClawMediaMixin:
    config: WeixinOpenClawConfig
    client: WeixinOpenClawClient

    async def resolve_inbound_image(self, item: dict[str, Any]) -> Path | None:
        image_item = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
        media = image_item.get("media") if isinstance(image_item.get("media"), dict) else {}
        encrypted_query_param = str(media.get("encrypt_query_param", "")).strip()
        if not encrypted_query_param:
            logger.bind(item_type=IMAGE_ITEM_TYPE, image_item_keys=list(image_item.keys()), media_keys=list(media.keys())).warning(t("LOG_WEIXIN_OPENCLAW_INBOUND_IMAGE_PARAM_MISSING"))
            return None

        aes_key_value = ""
        image_aes_key = str(image_item.get("aeskey", "")).strip()
        if image_aes_key:
            try:
                aes_key_value = base64.b64encode(bytes.fromhex(image_aes_key)).decode("utf-8")
            except ValueError:
                logger.bind(item_type=IMAGE_ITEM_TYPE).warning(t("LOG_WEIXIN_OPENCLAW_INBOUND_IMAGE_AESKEY_FALLBACK"))
                aes_key_value = image_aes_key
        if not aes_key_value:
            aes_key_value = str(media.get("aes_key", "")).strip()

        try:
            if aes_key_value:
                content = await self.download_and_decrypt_media(encrypted_query_param, aes_key_value)
            else:
                content = await self.client.download_cdn_bytes(encrypted_query_param)
            image_suffix = self.detect_image_suffix(content)
            return self.save_inbound_media(content, prefix="weixin_openclaw_img", file_name=f"image{image_suffix}", fallback_suffix=image_suffix)
        except Exception as exc:
            logger.bind(item_type=IMAGE_ITEM_TYPE).warning(t("LOG_WEIXIN_OPENCLAW_INBOUND_IMAGE_RESOLVE_FAILED", error=str(exc)))
            return None

    async def resolve_inbound_file(self, item: dict[str, Any]) -> Path | None:
        file_item = item.get("file_item") if isinstance(item.get("file_item"), dict) else {}
        media = file_item.get("media") if isinstance(file_item.get("media"), dict) else {}
        encrypted_query_param = str(media.get("encrypt_query_param", "")).strip()
        aes_key_value = str(media.get("aes_key", "")).strip()
        if not encrypted_query_param or not aes_key_value:
            logger.bind(item_type=FILE_ITEM_TYPE, file_item_keys=list(file_item.keys()), media_keys=list(media.keys()), has_encrypt_query_param=bool(encrypted_query_param), has_aes_key=bool(aes_key_value)).warning(t("LOG_WEIXIN_OPENCLAW_INBOUND_FILE_PARAM_MISSING"))
            return None

        file_name = self.normalize_inbound_filename(str(file_item.get("file_name", "")).strip(), "file.bin")
        try:
            content = await self.download_and_decrypt_media(encrypted_query_param, aes_key_value)
            return self.save_inbound_media(content, prefix="weixin_openclaw_file", file_name=file_name, fallback_suffix=".bin")
        except Exception as exc:
            logger.bind(item_type=FILE_ITEM_TYPE, file_name=file_name).warning(t("LOG_WEIXIN_OPENCLAW_INBOUND_FILE_RESOLVE_FAILED", error=str(exc)))
            return None

    def save_inbound_media(self, content: bytes, *, prefix: str, file_name: str, fallback_suffix: str) -> Path:
        max_bytes = self.config.max_inbound_media_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise RuntimeError(
                t(
                    ERR_WEIXIN_OPENCLAW_INBOUND_MEDIA_TOO_LARGE,
                    actual_bytes=len(content),
                    max_bytes=max_bytes,
                )
            )

        normalized_name = self.normalize_inbound_filename(file_name, f"{prefix}{fallback_suffix}")
        stem = Path(normalized_name).stem or prefix
        suffix = Path(normalized_name).suffix or fallback_suffix
        target_dir = TEMP_DIR / "weixin_openclaw"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{prefix}_{uuid.uuid4().hex}_{stem}{suffix}"
        target.write_bytes(content)
        return target

    @staticmethod
    def normalize_inbound_filename(file_name: str, fallback_name: str) -> str:
        normalized = Path(file_name or "").name.strip()
        return normalized or fallback_name

    @staticmethod
    def detect_image_suffix(content: bytes) -> str:
        if content.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return ".webp"
        if content.startswith(b"BM"):
            return ".bmp"
        return ".jpg"

    async def download_and_decrypt_media(self, encrypted_query_param: str, aes_key_value: str) -> bytes:
        encrypted = await self.client.download_cdn_bytes(encrypted_query_param)
        key = parse_media_aes_key(aes_key_value)
        return pkcs7_unpad(aes_ecb_decrypt(encrypted, key))

    async def reply_file_item(self, user_id: str, file_item: dict[str, Any], *, context_token: str = "") -> bool:
        try:
            media_path = self.resolve_outbound_file_path(file_item)
            if media_path is None:
                logger.bind(user_id=user_id, file_id=file_item.get("id"), file_path=file_item.get("path"), file_name=file_item.get("name")).warning(t("LOG_WEIXIN_OPENCLAW_OUTBOUND_FILE_NOT_RESOLVABLE"))
                return False

            file_name = self.resolve_outbound_file_name(file_item, media_path)
            mime_type = str(file_item.get("mime_type") or mimetypes.guess_type(file_name)[0] or "").lower()
            is_image = mime_type.startswith("image/") or media_path.suffix.lower() in IMAGE_EXTENSIONS
            upload_media_type = IMAGE_UPLOAD_TYPE if is_image else FILE_UPLOAD_TYPE
            item_type = IMAGE_ITEM_TYPE if is_image else FILE_ITEM_TYPE
            media_item = await self.prepare_media_item(
                user_id=user_id,
                media_path=media_path,
                upload_media_type=upload_media_type,
                item_type=item_type,
                file_name=file_name,
            )
            return await self.reply_items(user_id, [media_item], context_token=context_token)
        except Exception as exc:
            logger.bind(user_id=user_id, file_name=file_item.get("name")).exception(t("LOG_WEIXIN_OPENCLAW_OUTBOUND_FILE_SEND_FAILED", error=str(exc)))
            return False

    @staticmethod
    def resolve_outbound_file_path(file_item: dict[str, Any]) -> Path | None:
        token = str(file_item.get("id") or "").strip()
        if not token:
            logger.bind(file_id=file_item.get("id"), file_name=file_item.get("name")).warning(t("LOG_WEIXIN_OPENCLAW_OUTBOUND_FILE_PATH_MISSING"))
            return None
        return resolve_file_token(token)

    @staticmethod
    def resolve_outbound_file_name(file_item: dict[str, Any], media_path: Path) -> str:
        raw_name = str(file_item.get("name") or file_item.get("display_name") or media_path.name).strip()
        return Path(raw_name).name or media_path.name

    async def prepare_media_item(self, *, user_id: str, media_path: Path, upload_media_type: int, item_type: int, file_name: str) -> dict[str, Any]:
        raw_bytes = media_path.read_bytes()
        raw_size = len(raw_bytes)
        raw_md5 = hashlib.md5(raw_bytes).hexdigest()
        file_key = uuid.uuid4().hex
        aes_key_hex = uuid.uuid4().bytes.hex()
        ciphertext_size = aes_padded_size(raw_size)

        payload = await self.client.request_json(
            "POST",
            "ilink/bot/getuploadurl",
            payload={
                "filekey": file_key,
                "media_type": upload_media_type,
                "to_user_id": user_id,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": ciphertext_size,
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
                "base_info": {"channel_version": self.config.channel_version},
            },
            token_required=True,
            timeout_ms=self.config.api_timeout_ms,
        )
        upload_url = self.client.resolve_cdn_upload_url(payload, file_key)
        if not upload_url:
            logger.bind(user_id=user_id, file_name=file_name, file_key=file_key, payload_keys=list(payload.keys())).warning(t("LOG_WEIXIN_OPENCLAW_UPLOAD_URL_MISSING"))
            raise RuntimeError(t(ERR_WEIXIN_OPENCLAW_UPLOAD_URL_MISSING))

        encrypted_query_param = await self.upload_to_cdn(upload_url, aes_key_hex, media_path)
        aes_key_b64 = base64.b64encode(aes_key_hex.encode("utf-8")).decode("utf-8")
        media_payload = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key": aes_key_b64,
            "encrypt_type": 1,
        }

        if item_type == IMAGE_ITEM_TYPE:
            return {
                "type": IMAGE_ITEM_TYPE,
                "image_item": {
                    "media": media_payload,
                    "mid_size": ciphertext_size,
                },
            }

        return {
            "type": FILE_ITEM_TYPE,
            "file_item": {
                "media": media_payload,
                "file_name": file_name,
                "len": str(raw_size),
            },
        }

    async def upload_to_cdn(self, upload_url: str, aes_key_hex: str, media_path: Path) -> str:
        raw_data = media_path.read_bytes()
        encrypted = aes_ecb_encrypt(pkcs7_pad(raw_data), bytes.fromhex(aes_key_hex))
        return await self.client.upload_cdn_bytes(upload_url, encrypted)
