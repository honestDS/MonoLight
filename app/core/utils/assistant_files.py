import asyncio
import base64
import hashlib
import io
import json
import os
import uuid
import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

from app.core.constants import (
    ERR_FILE_ARGUMENT_INVALID,
    ERR_FILE_EXTENSION_BLOCKED,
    ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED,
    ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED,
    ERR_LLM_IMAGE_OUTPUT_INVALID,
    ERR_LLM_IMAGE_OUTPUT_SAVE_FAILED,
)
from app.core.exceptions import LLMException
from app.core.paths import get_user_temp_dir
from app.core.tools.send_file_to_user import (
    DEFAULT_MAX_FILE_COUNT,
    DEFAULT_MAX_SINGLE_FILE_SIZE_MB,
    DEFAULT_MAX_TOTAL_FILE_SIZE_MB,
    _encode_token,
    _is_previewable,
    _normalize_blocked_extensions,
)
from app.models.message import InternalMessage


def build_assistant_files_content(text: Any, files: list[dict[str, Any]]) -> str:
    safe_text = parse_assistant_files_content(text)
    return json.dumps(
        {
            "type": "assistant_files",
            "text": safe_text,
            "files": files,
        },
        ensure_ascii=False,
    )


def parse_assistant_files_content(content: Any) -> str:
    if not isinstance(content, str):
        return str(content or "").strip()
    try:
        parsed = json.loads(content)
    except Exception:
        return content.strip()
    if not isinstance(parsed, dict) or parsed.get("type") != "assistant_files":
        return content.strip()
    text = str(parsed.get("text") or "").strip()
    return text


def merge_assistant_files(*file_groups: Any) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for items in file_groups:
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or item.get("path") or "").strip()
            if file_id and file_id in seen_ids:
                continue
            if file_id:
                seen_ids.add(file_id)
            files.append(item)
    return files


async def materialize_generated_images(
    message: InternalMessage,
    *,
    project_root: str | Path,
    uid: str,
    session_id: str,
    cfg: Any,
) -> list[dict[str, Any]]:
    generated_images = message.generated_images
    if not generated_images:
        return []

    def _materialize() -> list[dict[str, Any]]:
        from PIL import Image

        tool_config = getattr(cfg, "tool", None)
        max_count = int(getattr(tool_config, "file_send_max_count", DEFAULT_MAX_FILE_COUNT) or DEFAULT_MAX_FILE_COUNT)
        max_single_size = int((getattr(tool_config, "file_send_max_single_size_mb", DEFAULT_MAX_SINGLE_FILE_SIZE_MB) or DEFAULT_MAX_SINGLE_FILE_SIZE_MB) * 1024 * 1024)
        max_total_size = int((getattr(tool_config, "file_send_max_total_size_mb", DEFAULT_MAX_TOTAL_FILE_SIZE_MB) or DEFAULT_MAX_TOTAL_FILE_SIZE_MB) * 1024 * 1024)
        blocked_extensions = _normalize_blocked_extensions(getattr(tool_config, "file_send_blocked_extensions", []) or [])

        if not isinstance(uid, str) or not uid or "\x00" in uid or uid in {".", ".."} or PurePosixPath(uid).name != uid or PureWindowsPath(uid).name != uid:
            raise LLMException(message=ERR_FILE_ARGUMENT_INVALID)

        def _resolve_child(path: Path, parent: Path) -> Path:
            resolved = path.resolve(strict=False)
            if resolved.parent != parent or resolved.name != path.name:
                raise LLMException(message=ERR_FILE_ARGUMENT_INVALID)
            return resolved

        try:
            user_dir = get_user_temp_dir(project_root, uid)
            user_parent = user_dir.parent.resolve(strict=False)
            user_dir = _resolve_child(user_dir, user_parent)
            generated_dir = _resolve_child(user_dir / "generated_images", user_dir)
        except (ValueError, RuntimeError) as exc:
            raise LLMException(message=ERR_FILE_ARGUMENT_INVALID) from exc
        except OSError as exc:
            raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_SAVE_FAILED) from exc

        prepared: list[tuple[bytes, str, str, str]] = []
        seen_ids: set[str] = set()
        total_size = 0
        max_encoded_size = ((max_single_size + 2) // 3) * 4
        formats = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}

        for generated_image in generated_images:
            if generated_image.id in seen_ids:
                continue
            seen_ids.add(generated_image.id)
            if len(seen_ids) > max_count:
                raise LLMException(message=ERR_FILE_ARGUMENT_INVALID)
            if len(generated_image.data) > max_encoded_size:
                raise LLMException(message=ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED)
            try:
                image_bytes = base64.b64decode(generated_image.data, validate=True)
            except Exception as exc:
                raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_INVALID) from exc
            if len(image_bytes) > max_single_size:
                raise LLMException(message=ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED)
            total_size += len(image_bytes)
            if total_size > max_total_size:
                raise LLMException(message=ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED)

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(image_bytes)) as image:
                        image_format = image.format.upper()
                        image.verify()
            except Exception as exc:
                raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_INVALID) from exc

            format_detail = formats.get(image_format)
            if format_detail is None:
                raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_INVALID)
            extension, mime_type = format_detail
            if generated_image.mime_type and generated_image.mime_type.strip().lower() != mime_type:
                raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_INVALID)
            blocked_names = {f".{extension}"}
            if extension == "jpg":
                blocked_names.add(".jpeg")
            if blocked_names & blocked_extensions:
                raise LLMException(message=ERR_FILE_EXTENSION_BLOCKED)

            deterministic_id = hashlib.sha256(generated_image.id.encode("utf-8") + b"\x00" + image_bytes).hexdigest()
            prepared.append((image_bytes, mime_type, f"generated_image_{deterministic_id}.{extension}", deterministic_id))

        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            generated_dir.mkdir(parents=True, exist_ok=True)
            user_dir = _resolve_child(user_dir, user_parent)
            generated_dir = _resolve_child(generated_dir, user_dir)
        except (ValueError, RuntimeError) as exc:
            raise LLMException(message=ERR_FILE_ARGUMENT_INVALID) from exc
        except OSError as exc:
            raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_SAVE_FAILED) from exc

        files: list[dict[str, Any]] = []
        try:
            for image_bytes, mime_type, filename, deterministic_id in prepared:
                target_path = generated_dir / filename
                temporary_path = generated_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
                temporary_created = False
                try:
                    with temporary_path.open("xb") as output_file:
                        temporary_created = True
                        output_file.write(image_bytes)
                    os.replace(temporary_path, target_path)
                    temporary_path = None
                finally:
                    if temporary_created and temporary_path is not None:
                        try:
                            temporary_path.unlink()
                        except OSError:
                            pass

                token = _encode_token({"path": str(target_path), "uid": uid, "id": deterministic_id})
                files.append(
                    {
                        "id": token,
                        "name": filename,
                        "mime_type": mime_type,
                        "size": len(image_bytes),
                        "download_url": f"/api/v1/download-sent?token={quote(token)}",
                        "previewable": _is_previewable(mime_type),
                    }
                )
        except OSError as exc:
            raise LLMException(message=ERR_LLM_IMAGE_OUTPUT_SAVE_FAILED) from exc

        return files

    files = await asyncio.to_thread(_materialize)
    message.generated_images = None
    return files
