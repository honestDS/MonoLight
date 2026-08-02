import json
from pathlib import Path
from typing import Any

from PIL import Image

from app.core.constants import (
    ERR_TOOL_MULTIMODAL_FILE_NOT_FOUND,
    ERR_TOOL_MULTIMODAL_INVALID_IMAGE,
    ERR_TOOL_MULTIMODAL_LOCAL_READ_UNIMPLEMENTED,
    ERR_TOOL_MULTIMODAL_NOT_REGULAR,
    ERR_TOOL_MULTIMODAL_PATH_INVALID,
    ERR_TOOL_MULTIMODAL_PATH_NOT_ABSOLUTE,
    ERR_TOOL_MULTIMODAL_UNSUPPORTED_TYPE,
    ERR_TOOL_OPERATION_DIRS_UNCONFIGURED,
    ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS,
    MSG_TOOL_MULTIMODAL_FILE_READ_SUCCESS,
)
from app.core.i18n import t
from app.core.utils.operation_directories import (
    get_allowed_operation_dirs,
    is_path_within_allowed_operation_dirs,
    normalize_allowed_operation_dirs,
)

from .base import BaseExecutor

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".wma"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"})
SUPPORTED_MODALITIES = frozenset({"image", "audio", "video"})

READ_MULTIMODAL_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_multimodal_file",
        "description": ("Read an existing multimodal file from the hard drive. After success, the system appends the tool artifact to the next model request; it is not new user input. Only images are actually read and sent to the model. Audio and video explicitly report that local reading is not implemented."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of the existing file to read.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


def _detect_modality(path: Path) -> str | None:
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return None


def parse_multimodal_file_read_result(content: str | None) -> dict[str, str] | None:
    """Return only a strictly valid successful multimodal-file result."""
    try:
        payload = json.loads(content or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "multimodal_file_read" or payload.get("status") != "success":
        return None

    modality = payload.get("modality")
    path = payload.get("path")
    message = payload.get("message")
    if modality not in SUPPORTED_MODALITIES or not isinstance(path, str) or not Path(path).is_absolute() or not isinstance(message, str) or not message:
        return None
    return {
        "path": path,
        "modality": modality,
        "message": message,
    }


class ReadMultimodalFileExecutor(BaseExecutor):
    requires_audit = False

    @staticmethod
    def _verify_image(path: Path) -> None:
        with Image.open(path) as image:
            image.verify()

    @staticmethod
    def _failed_result(
        error: str,
        *,
        path: Path | None = None,
        modality: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "type": "multimodal_file_read",
            "status": "failed",
            "error": error,
        }
        if path is not None:
            payload["path"] = str(path)
        if modality is not None:
            payload["modality"] = modality
        return json.dumps(payload, ensure_ascii=False)

    async def execute(self, path: str) -> str:
        if not isinstance(path, str) or not path.strip():
            return self._failed_result(t(ERR_TOOL_MULTIMODAL_PATH_INVALID))

        candidate = Path(path)
        if not candidate.is_absolute():
            return self._failed_result(t(ERR_TOOL_MULTIMODAL_PATH_NOT_ABSOLUTE))

        try:
            resolved_path = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return self._failed_result(t(ERR_TOOL_MULTIMODAL_PATH_INVALID))

        allowed_dirs = get_allowed_operation_dirs(self.cfg)
        if not normalize_allowed_operation_dirs(allowed_dirs):
            return self._failed_result(t(ERR_TOOL_OPERATION_DIRS_UNCONFIGURED), path=resolved_path)
        if not is_path_within_allowed_operation_dirs(resolved_path, allowed_dirs):
            return self._failed_result(t(ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS), path=resolved_path)

        try:
            if not resolved_path.exists():
                return self._failed_result(t(ERR_TOOL_MULTIMODAL_FILE_NOT_FOUND), path=resolved_path)
            if not resolved_path.is_file():
                return self._failed_result(t(ERR_TOOL_MULTIMODAL_NOT_REGULAR), path=resolved_path)
        except OSError:
            return self._failed_result(t(ERR_TOOL_MULTIMODAL_PATH_INVALID), path=resolved_path)

        modality = _detect_modality(resolved_path)
        if modality is None:
            return self._failed_result(t(ERR_TOOL_MULTIMODAL_UNSUPPORTED_TYPE), path=resolved_path)
        if modality in {"audio", "video"}:
            return self._failed_result(
                t(ERR_TOOL_MULTIMODAL_LOCAL_READ_UNIMPLEMENTED),
                path=resolved_path,
                modality=modality,
            )

        try:
            await self.run_sync(self._verify_image, resolved_path)
        except Exception:
            return self._failed_result(
                t(ERR_TOOL_MULTIMODAL_INVALID_IMAGE),
                path=resolved_path,
                modality=modality,
            )

        return json.dumps(
            {
                "type": "multimodal_file_read",
                "status": "success",
                "modality": modality,
                "path": str(resolved_path),
                "message": t(MSG_TOOL_MULTIMODAL_FILE_READ_SUCCESS),
            },
            ensure_ascii=False,
        )
