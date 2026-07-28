import base64
import json
import mimetypes
import ssl
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from app.core.channel_router import select_channel
from app.core.constants import (
    ERR_IMAGE_CONTENT_TYPE_UNSUPPORTED,
    ERR_TOOL_IMAGE_CHANNEL_NOT_CONFIGURED,
    ERR_TOOL_IMAGE_CHANNEL_UNAVAILABLE,
    ERR_TOOL_IMAGE_EMPTY_RESPONSE,
    ERR_TOOL_IMAGE_INVALID_ITEM,
    ERR_TOOL_IMAGE_PROMPT_REQUIRED,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    MSG_TOOL_IMAGE_SEND_INSTRUCTION,
)
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.models.channel import ChannelConfig, resolve_model_protocol
from app.providers.image_generation import ImageGenerationClient

from .base import BaseExecutor
from .send_file_to_user import _encode_token

logger = get_logger(__name__)

IMAGE_GENERATION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate one image using the selected profile's configured image generation model. Use it when the user asks to create, draw, render, or generate an image.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A detailed image generation prompt describing the subject, scene, style, composition, colors, and constraints.",
                },
                "size": {
                    "type": "string",
                    "enum": ["1024x1024", "1024x1536", "1536x1024"],
                    "description": "Image size.",
                    "default": "1024x1024",
                },
                "quality": {
                    "type": "string",
                    "enum": ["auto", "low", "medium", "high"],
                    "description": "Image quality.",
                    "default": "auto",
                },
            },
            "required": ["prompt"],
        },
    },
}


class ImageGenerationExecutor(BaseExecutor):
    requires_audit = False

    def _get_channel_config(self) -> ChannelConfig | None:
        channel_group = getattr(self.cfg, "channel", None)
        if not channel_group:
            return None
        return getattr(channel_group, "image_generation_channel", None)

    def _get_generated_image_dir(self) -> Path:
        image_dir = get_user_temp_dir(self.project_root, self.uid) / "generated_images"
        image_dir.mkdir(parents=True, exist_ok=True)
        return image_dir

    async def _write_image_file(self, image_bytes: bytes, file_name: str, mime_type: str) -> dict[str, Any]:
        image_path = (self._get_generated_image_dir() / file_name).resolve()

        def write_image():
            image_path.write_bytes(image_bytes)

        await self.run_sync(write_image)
        token = _encode_token({"path": str(image_path), "uid": self.uid, "id": uuid.uuid4().hex})
        return {
            "id": token,
            "name": file_name,
            "path": str(image_path),
            "description": "Generated image",
            "mime_type": mime_type,
            "size": len(image_bytes),
            "download_url": f"/api/v1/download-sent?token={quote(token)}",
            "previewable": True,
        }

    async def _save_base64_image(self, b64_json: str) -> dict[str, Any]:
        self._log_image_save_started(source="base64")
        image_bytes = base64.b64decode(b64_json)
        file_item = await self._write_image_file(image_bytes, f"generated_image_{uuid.uuid4().hex}.png", "image/png")
        self._log_image_saved(file_item, source="base64")
        return file_item

    def _log_image_save_started(self, *, source: str, source_url: str | None = None) -> None:
        logger.bind(
            uid=self.uid,
            source=source,
            source_url=source_url,
        ).info(t("LOG_IMAGE_GENERATION_SAVE_STARTED"))

    def _log_image_saved(self, file_item: dict[str, Any], *, source: str, source_url: str | None = None) -> None:
        logger.bind(
            uid=self.uid,
            source=source,
            source_url=source_url,
            file_name=file_item["name"],
            path=file_item["path"],
            mime_type=file_item["mime_type"],
            size=file_item["size"],
        ).info(t("LOG_IMAGE_GENERATION_SAVE_SUCCEEDED"))

    async def _download_remote_image(self, url: str) -> tuple[bytes, str]:
        client_timeout = aiohttp.ClientTimeout(total=float(getattr(getattr(self.cfg, "tool", None), "image_generation_timeout", 60.0) or 60.0))
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            try:
                return await self._fetch_remote_image(session, url)
            except aiohttp.ClientConnectorCertificateError:
                return await self._fetch_remote_image(session, url, ssl=False)
            except aiohttp.ClientConnectorSSLError as exc:
                if not isinstance(exc.__cause__, ssl.SSLCertVerificationError):
                    raise
                return await self._fetch_remote_image(session, url, ssl=False)

    async def _fetch_remote_image(self, session: aiohttp.ClientSession, url: str, ssl: bool | None = None) -> tuple[bytes, str]:
        async with session.get(url, ssl=ssl) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            image_bytes = await response.read()
            return image_bytes, content_type or "application/octet-stream"

    async def _save_downloaded_image(self, url: str) -> dict[str, Any]:
        self._log_image_save_started(source="remote_url", source_url=url)
        image_bytes, content_type = await self._download_remote_image(url)
        if not content_type.startswith("image/"):
            raise ValueError(t(ERR_IMAGE_CONTENT_TYPE_UNSUPPORTED, content_type=content_type))
        extension = mimetypes.guess_extension(content_type) or ".img"
        if extension == ".jpe":
            extension = ".jpg"
        file_item = await self._write_image_file(image_bytes, f"generated_image_{uuid.uuid4().hex}{extension}", content_type)
        self._log_image_saved(file_item, source="remote_url", source_url=url)
        return file_item

    def _build_success_payload(
        self,
        file_item: dict[str, Any],
    ) -> str:
        return json.dumps(
            {
                "status": "success",
                "instruction": t(MSG_TOOL_IMAGE_SEND_INSTRUCTION),
                "send_file_to_user": {
                    "files": [
                        {
                            "path": file_item["path"],
                            "display_name": file_item["name"],
                            "description": file_item["description"],
                            "mime_type": file_item["mime_type"],
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

    async def execute(
        self,
        prompt: str,
        size: str | None = None,
        quality: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.db or not self.profile or not self.cfg:
            return json.dumps({"status": "failed", "error": t(ERR_TOOL_RUNTIME_CONTEXT_MISSING)}, ensure_ascii=False)

        image_channel = self._get_channel_config()
        if not image_channel or not image_channel.rules:
            return json.dumps(
                {
                    "status": "failed",
                    "error": t(ERR_TOOL_IMAGE_CHANNEL_NOT_CONFIGURED),
                },
                ensure_ascii=False,
            )

        prompt_text = (prompt or "").strip()
        if not prompt_text:
            return json.dumps({"status": "failed", "error": t(ERR_TOOL_IMAGE_PROMPT_REQUIRED)}, ensure_ascii=False)

        excluded_priorities: set[int] = set()
        last_error = ""
        cursor_key = f"{self.profile.id}:IMAGE_GENERATION" if self.profile.id else None

        while True:
            selection = await select_channel(
                self.db,
                image_channel,
                "IMAGE_GENERATION",
                call_context="image_generation_tool",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )
            if not selection:
                return json.dumps(
                    {
                        "status": "failed",
                        "error": last_error or t(ERR_TOOL_IMAGE_CHANNEL_UNAVAILABLE),
                    },
                    ensure_ascii=False,
                )

            channel, model_entry, rule = selection
            try:
                resolved_size = size or model_entry.get("size") or "1024x1024"
                resolved_quality = quality or model_entry.get("quality") or "auto"
                await self.db.commit()
                response = await ImageGenerationClient.generate_image(
                    api_key=channel.get_decrypted_api_key(),
                    base_url=channel.base_url or "",
                    model_id=model_entry["model_id"],
                    protocol=resolve_model_protocol(model_entry),
                    prompt=prompt_text,
                    size=resolved_size,
                    n=1,
                    quality=resolved_quality,
                    timeout=float(getattr(getattr(self.cfg, "tool", None), "image_generation_timeout", 60.0) or 60.0),
                    http_proxy=get_channel_http_proxy(channel),
                    custom_headers=get_model_custom_headers(model_entry),
                )
                images = response.get("data") if isinstance(response, dict) else None
                if not isinstance(images, list) or not images:
                    return json.dumps(
                        {
                            "status": "failed",
                            "error": t(ERR_TOOL_IMAGE_EMPTY_RESPONSE),
                            "model": model_entry["model_id"],
                        },
                        ensure_ascii=False,
                    )

                image = images[0] if isinstance(images[0], dict) else {}
                model_name = response.get("model", model_entry["model_id"]) if isinstance(response, dict) else model_entry["model_id"]

                if image.get("url"):
                    file_item = await self._save_downloaded_image(str(image["url"]))
                    return self._build_success_payload(file_item)

                if image.get("b64_json"):
                    file_item = await self._save_base64_image(str(image["b64_json"]))
                    return self._build_success_payload(file_item)

                return json.dumps(
                    {
                        "status": "failed",
                        "error": t(ERR_TOOL_IMAGE_INVALID_ITEM),
                        "model": model_name,
                    },
                    ensure_ascii=False,
                )
            except BaseBusinessException as exc:
                last_error = t(exc.message, default=exc.message, **exc.kwargs)
            except Exception as exc:
                last_error = str(exc)

            excluded_priorities.add(rule.priority)
