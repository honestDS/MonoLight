import base64
import binascii
import io
import json
import mimetypes
import ssl
import uuid
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from PIL import Image

from app.core.channel_router import select_channel
from app.core.constants import (
    ERR_FILE_EXTENSION_BLOCKED,
    ERR_FILE_NOT_FOUND,
    ERR_FILE_PATH_NOT_ABSOLUTE,
    ERR_FILE_SENSITIVE_NOT_ALLOWED,
    ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED,
    ERR_FILE_TOOL_NOT_REGULAR,
    ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED,
    ERR_IMAGE_CONTENT_TYPE_UNSUPPORTED,
    ERR_LLM_IMAGE_OUTPUT_INVALID,
    ERR_TOOL_IMAGE_CHANNEL_NOT_CONFIGURED,
    ERR_TOOL_IMAGE_CHANNEL_UNAVAILABLE,
    ERR_TOOL_IMAGE_EMPTY_RESPONSE,
    ERR_TOOL_IMAGE_INVALID_ITEM,
    ERR_TOOL_IMAGE_PROMPT_REQUIRED,
    ERR_TOOL_IMAGE_REFERENCE_INVALID,
    ERR_TOOL_OPERATION_DIRS_UNCONFIGURED,
    ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    IMAGE_GENERATION_MAX_INPUT_BYTES,
    IMAGE_GENERATION_MAX_REFERENCE_IMAGES,
    MSG_TOOL_IMAGE_SEND_INSTRUCTION,
)
from app.core.exceptions import BaseBusinessException, LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.paths import get_user_temp_dir
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs, get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.operation_directories import (
    get_allowed_operation_dirs,
    is_path_within_allowed_operation_dirs,
    normalize_allowed_operation_dirs,
)
from app.models.channel import ChannelConfig, resolve_model_protocol
from app.providers.image_generation import ImageGenerationClient

from .base import BaseExecutor
from .send_file_to_user import (
    DEFAULT_MAX_SINGLE_FILE_SIZE_MB,
    DEFAULT_MAX_TOTAL_FILE_SIZE_MB,
    _encode_token,
    _is_sensitive_path,
    _normalize_blocked_extensions,
)

logger = get_logger(__name__)

IMAGE_GENERATION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Create, generate, or edit images using the selected profile's configured image generation model. Supports text-to-image generation, generation from local reference images, and image editing.",
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
                "reference_images": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": IMAGE_GENERATION_MAX_REFERENCE_IMAGES,
                    "description": "Optional existing absolute local image paths, including uploaded images, previously generated images, or images in configured authorized directories.",
                },
            },
            "required": ["prompt"],
        },
    },
}


class ImageGenerationExecutor(BaseExecutor):
    requires_audit = False

    def _get_image_input_limits(self) -> tuple[int, int, set[str]]:
        tool_config = getattr(self.cfg, "tool", None)
        configured_single_size_mb = getattr(tool_config, "file_send_max_single_size_mb", DEFAULT_MAX_SINGLE_FILE_SIZE_MB) if tool_config else DEFAULT_MAX_SINGLE_FILE_SIZE_MB
        configured_total_size_mb = getattr(tool_config, "file_send_max_total_size_mb", DEFAULT_MAX_TOTAL_FILE_SIZE_MB) if tool_config else DEFAULT_MAX_TOTAL_FILE_SIZE_MB
        configured_blocked_extensions = getattr(tool_config, "file_send_blocked_extensions", []) if tool_config else []

        def size_limit(value: Any, default_mb: int) -> int:
            try:
                return max(1, int(float(value or default_mb) * 1024 * 1024))
            except (TypeError, ValueError):
                return max(1, int(float(default_mb) * 1024 * 1024))

        single_size_limit = min(size_limit(configured_single_size_mb, DEFAULT_MAX_SINGLE_FILE_SIZE_MB), IMAGE_GENERATION_MAX_INPUT_BYTES)
        total_size_limit = size_limit(configured_total_size_mb, DEFAULT_MAX_TOTAL_FILE_SIZE_MB)
        blocked_extensions = _normalize_blocked_extensions(configured_blocked_extensions if isinstance(configured_blocked_extensions, list) else [])
        return single_size_limit, total_size_limit, blocked_extensions

    @staticmethod
    def _resolve_image_input_path(raw_path: str, allowed_dirs: list[str], blocked_extensions: set[str]) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError(t(ERR_FILE_PATH_NOT_ABSOLUTE))

        try:
            resolved_path = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise ValueError(t(ERR_FILE_NOT_FOUND))

        if not is_path_within_allowed_operation_dirs(resolved_path, allowed_dirs):
            raise ValueError(t(ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS))
        if _is_sensitive_path(resolved_path):
            raise ValueError(t(ERR_FILE_SENSITIVE_NOT_ALLOWED))
        if resolved_path.suffix.lower() in blocked_extensions:
            raise ValueError(t(ERR_FILE_EXTENSION_BLOCKED))
        if not resolved_path.exists():
            raise ValueError(t(ERR_FILE_NOT_FOUND))
        if not resolved_path.is_file():
            raise ValueError(t(ERR_FILE_TOOL_NOT_REGULAR))
        return resolved_path

    @staticmethod
    def _read_bounded_image_file(path: Path, max_bytes: int, overflow_error: str) -> bytes:
        try:
            with path.open("rb") as image_file:
                image_bytes = image_file.read(max_bytes + 1)
        except OSError as exc:
            raise ValueError(t(ERR_FILE_NOT_FOUND)) from exc
        if len(image_bytes) > max_bytes:
            raise ValueError(t(overflow_error))
        return image_bytes

    @staticmethod
    def _inspect_image_bytes(image_bytes: bytes, error_key: str) -> tuple[str, str]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(image_bytes)) as image:
                    image_format = (image.format or "").upper()
                    image.verify()
        except Exception as exc:
            raise ValueError(t(error_key)) from exc

        format_details = {
            "PNG": (".png", "image/png"),
            "JPEG": (".jpg", "image/jpeg"),
            "WEBP": (".webp", "image/webp"),
        }.get(image_format)
        if not format_details:
            raise ValueError(t(error_key))
        return format_details

    def _load_local_image_inputs(
        self,
        reference_paths: list[str],
    ) -> list[tuple[str, bytes, str]]:
        allowed_dirs = get_allowed_operation_dirs(self.cfg)
        if not normalize_allowed_operation_dirs(allowed_dirs):
            raise ValueError(t(ERR_TOOL_OPERATION_DIRS_UNCONFIGURED))

        single_size_limit, total_size_limit, blocked_extensions = self._get_image_input_limits()
        total_size = 0
        reference_payloads: list[tuple[str, bytes, str]] = []

        for raw_path in reference_paths:
            resolved_path = self._resolve_image_input_path(raw_path, allowed_dirs, blocked_extensions)
            image_bytes = self._read_bounded_image_file(
                resolved_path,
                single_size_limit,
                ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED,
            )
            extension, mime_type = self._inspect_image_bytes(
                image_bytes,
                ERR_TOOL_IMAGE_REFERENCE_INVALID,
            )
            if total_size + len(image_bytes) > total_size_limit:
                raise ValueError(t(ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED))
            total_size += len(image_bytes)
            reference_payloads.append(
                (
                    f"{resolved_path.stem or 'image'}{extension}",
                    image_bytes,
                    mime_type,
                )
            )

        return reference_payloads

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
        def write_image() -> Path:
            image_path = (self._get_generated_image_dir() / file_name).resolve()
            image_path.write_bytes(image_bytes)
            return image_path

        image_path = await self.run_sync(write_image)
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
        try:
            image_bytes = base64.b64decode(b64_json, validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise ValueError(t(ERR_LLM_IMAGE_OUTPUT_INVALID)) from exc
        extension, mime_type = await self.run_sync(
            self._inspect_image_bytes,
            image_bytes,
            ERR_LLM_IMAGE_OUTPUT_INVALID,
        )
        file_item = await self._write_image_file(image_bytes, f"generated_image_{uuid.uuid4().hex}{extension}", mime_type)
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

    async def _download_remote_image(self, url: str, *, http_proxy: str | None = None) -> tuple[bytes, str]:
        client_timeout = aiohttp.ClientTimeout(total=float(getattr(getattr(self.cfg, "tool", None), "image_generation_timeout", 60.0) or 60.0))
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            try:
                return await self._fetch_remote_image(session, url, http_proxy=http_proxy)
            except aiohttp.ClientConnectorCertificateError:
                return await self._fetch_remote_image(session, url, ssl=False, http_proxy=http_proxy)
            except aiohttp.ClientConnectorSSLError as exc:
                if not isinstance(exc.__cause__, ssl.SSLCertVerificationError):
                    raise
                return await self._fetch_remote_image(session, url, ssl=False, http_proxy=http_proxy)

    async def _fetch_remote_image(
        self,
        session: aiohttp.ClientSession,
        url: str,
        ssl: bool | None = None,
        *,
        http_proxy: str | None = None,
    ) -> tuple[bytes, str]:
        async with session.get(url, ssl=ssl, **build_aiohttp_proxy_kwargs(http_proxy)) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            image_bytes = await response.read()
            return image_bytes, content_type or "application/octet-stream"

    async def _save_downloaded_image(self, url: str, *, http_proxy: str | None = None) -> dict[str, Any]:
        self._log_image_save_started(source="remote_url", source_url=url)
        image_bytes, content_type = await self._download_remote_image(url, http_proxy=http_proxy)
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
        reference_images: list[str] | None = None,
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

        if reference_images is None:
            normalized_reference_images: list[str] = []
        elif not isinstance(reference_images, list) or len(reference_images) > IMAGE_GENERATION_MAX_REFERENCE_IMAGES:
            return json.dumps({"status": "failed", "error": t(ERR_TOOL_IMAGE_REFERENCE_INVALID)}, ensure_ascii=False)
        else:
            normalized_reference_images = list(reference_images)
            if any(not isinstance(path, str) or not path.strip() for path in normalized_reference_images):
                return json.dumps({"status": "failed", "error": t(ERR_TOOL_IMAGE_REFERENCE_INVALID)}, ensure_ascii=False)

        reference_payloads: list[tuple[str, bytes, str]] = []
        if normalized_reference_images:
            try:
                reference_payloads = await self.run_sync(
                    self._load_local_image_inputs,
                    normalized_reference_images,
                )
            except Exception as exc:
                return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)

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
                http_proxy = get_channel_http_proxy(channel)
                resolved_size = size or model_entry.get("size") or "1024x1024"
                resolved_quality = quality or model_entry.get("quality") or "auto"
                input_kwargs: dict[str, Any] = {}
                if reference_payloads:
                    input_kwargs = {
                        "reference_images": reference_payloads,
                    }
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
                    http_proxy=http_proxy,
                    custom_headers=get_model_custom_headers(model_entry),
                    **input_kwargs,
                )
                images = response.get("data") if isinstance(response, dict) else None
                if not isinstance(images, list) or not images:
                    raise LLMException(ERR_TOOL_IMAGE_EMPTY_RESPONSE)

                image = images[0] if isinstance(images[0], dict) else {}

                if image.get("url"):
                    file_item = await self._save_downloaded_image(str(image["url"]), http_proxy=http_proxy)
                    return self._build_success_payload(file_item)

                if image.get("b64_json"):
                    file_item = await self._save_base64_image(str(image["b64_json"]))
                    return self._build_success_payload(file_item)

                raise LLMException(ERR_TOOL_IMAGE_INVALID_ITEM)
            except BaseBusinessException as exc:
                last_error = t(exc.message, default=exc.message, **exc.kwargs)
            except Exception as exc:
                last_error = str(exc)

            excluded_priorities.add(rule.priority)
