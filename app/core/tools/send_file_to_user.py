import base64
import hashlib
import hmac
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.crypto import _get_encryption_key

from .base import BaseExecutor

DEFAULT_MAX_FILE_COUNT = 10
DEFAULT_MAX_SINGLE_FILE_SIZE_MB = 50
DEFAULT_MAX_TOTAL_FILE_SIZE_MB = 100
TOKEN_VERSION = "v1"
SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "known_hosts",
}


def _sign_payload(payload: str) -> str:
    key = _get_encryption_key()
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _encode_token(data: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = _sign_payload(payload)
    return f"{TOKEN_VERSION}.{payload}.{signature}"


def resolve_file_token(token: str) -> Path:
    parts = token.split(".", 2)
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        raise ValueError("Invalid file token")

    _version, payload, signature = parts
    expected_signature = _sign_payload(payload)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid file token signature")

    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid file token payload") from exc

    path = Path(data.get("path", "")).resolve()
    if _is_sensitive_path(path):
        raise ValueError("Sensitive file is not allowed")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("File not found")
    return path


def _normalize_allowed_dirs(allowed_dirs: list[str] | None = None) -> list[Path]:
    normalized_dirs = []
    for directory in allowed_dirs or []:
        try:
            directory_path = Path(directory)
            if directory_path.is_absolute():
                normalized_dirs.append(directory_path.resolve())
        except Exception:
            continue
    return normalized_dirs


def _is_allowed_path(path: Path, allowed_dirs: list[str] | None = None) -> bool:
    for root in _normalize_allowed_dirs(allowed_dirs):
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_sensitive_path(path: Path) -> bool:
    filename = path.name.lower()
    parts = {part.lower() for part in path.parts}

    if filename in SENSITIVE_FILENAMES:
        return True
    return bool({"secrets", ".ssh", ".git"}.intersection(parts))


def _is_previewable(mime_type: str) -> bool:
    return mime_type.startswith(("image/", "audio/", "video/")) or mime_type in {"application/pdf", "text/plain"}


def _normalize_files(files: Any) -> list[dict[str, Any]] | None:
    if isinstance(files, dict):
        return [files]
    if isinstance(files, list) and all(isinstance(item, dict) for item in files):
        return files
    return None


def _normalize_blocked_extensions(extensions: list[str] | None = None) -> set[str]:
    blocked_extensions = set()
    for extension in extensions or []:
        if not isinstance(extension, str):
            continue
        normalized_extension = extension.strip().lower()
        if not normalized_extension:
            continue
        if not normalized_extension.startswith("."):
            normalized_extension = f".{normalized_extension}"
        blocked_extensions.add(normalized_extension)
    return blocked_extensions


def summarize_files_to_user_result(content: str | None) -> str | None:
    if not isinstance(content, str):
        return content
    try:
        payload = json.loads(content)
    except Exception:
        return content
    if not isinstance(payload, dict) or payload.get("type") != "files_to_user":
        return content

    files = payload.get("files") or []
    success = bool(files)
    if success:
        message = "File sending succeeded. The sent files will be automatically appended after your assistant reply in the chat UI. Do not repeat file download links or file metadata in your text response."
    else:
        message = "File sending failed. Ask the user to check the file paths and profile file sending whitelist before retrying."

    return json.dumps(
        {
            "type": "files_to_user_result",
            "status": "success" if success else "failed",
            "message": message,
        },
        ensure_ascii=False,
    )


sanitize_files_to_user_result = summarize_files_to_user_result


class SendFileToUserExecutor(BaseExecutor):
    def _get_tool_config(self) -> Any:
        return getattr(self.cfg, "tool", None)

    def _get_allowed_dirs(self) -> list[str]:
        tool_config = self._get_tool_config()
        allowed_dirs = getattr(tool_config, "allowed_file_send_dirs", []) if tool_config else []
        if isinstance(allowed_dirs, list):
            return [directory for directory in allowed_dirs if isinstance(directory, str)]
        return []

    def _get_limit_config(self) -> tuple[int, int, int, set[str]]:
        tool_config = self._get_tool_config()
        max_count = getattr(tool_config, "file_send_max_count", DEFAULT_MAX_FILE_COUNT) if tool_config else DEFAULT_MAX_FILE_COUNT
        max_single_size_mb = getattr(tool_config, "file_send_max_single_size_mb", DEFAULT_MAX_SINGLE_FILE_SIZE_MB) if tool_config else DEFAULT_MAX_SINGLE_FILE_SIZE_MB
        max_total_size_mb = getattr(tool_config, "file_send_max_total_size_mb", DEFAULT_MAX_TOTAL_FILE_SIZE_MB) if tool_config else DEFAULT_MAX_TOTAL_FILE_SIZE_MB
        blocked_extensions = getattr(tool_config, "file_send_blocked_extensions", []) if tool_config else []

        return (
            max(1, int(max_count or DEFAULT_MAX_FILE_COUNT)),
            max(1, int(float(max_single_size_mb or DEFAULT_MAX_SINGLE_FILE_SIZE_MB) * 1024 * 1024)),
            max(1, int(float(max_total_size_mb or DEFAULT_MAX_TOTAL_FILE_SIZE_MB) * 1024 * 1024)),
            _normalize_blocked_extensions(blocked_extensions if isinstance(blocked_extensions, list) else []),
        )

    async def execute(self, files: Any) -> str:
        normalized_files = _normalize_files(files)
        allowed_dirs = self._get_allowed_dirs()
        if normalized_files is None:
            return json.dumps(
                {
                    "type": "files_to_user",
                    "files": [],
                    "status": "failed",
                    "errors": [{"path": "", "error": "Invalid files argument. Expected an object or an array of objects."}],
                    "allowed_file_send_dirs": allowed_dirs,
                },
                ensure_ascii=False,
            )

        max_file_count, max_single_file_size, max_total_file_size, blocked_extensions = self._get_limit_config()
        valid_files = []
        errors = []
        total_size = 0

        if not _normalize_allowed_dirs(allowed_dirs):
            return json.dumps(
                {
                    "type": "files_to_user",
                    "files": [],
                    "status": "failed",
                    "errors": [{"path": "", "error": "No allowed file sending directories are configured. Ask the user to configure tool.allowed_file_send_dirs in the active profile before calling send_file_to_user."}],
                    "allowed_file_send_dirs": allowed_dirs,
                },
                ensure_ascii=False,
            )

        if len(normalized_files) > max_file_count:
            normalized_files = normalized_files[:max_file_count]
            errors.append({"path": "", "error": f"Only the first {max_file_count} files are processed"})

        for item in normalized_files:
            raw_path = item.get("path") if isinstance(item, dict) else None
            try:
                if not raw_path:
                    raise ValueError("Missing file path")

                path = Path(raw_path)
                if not path.is_absolute():
                    raise ValueError("File path must be absolute")

                resolved_path = path.resolve()
                if not _is_allowed_path(resolved_path, allowed_dirs):
                    raise ValueError("File path is outside allowed directories")
                if _is_sensitive_path(resolved_path):
                    raise ValueError("Sensitive file is not allowed")
                if resolved_path.suffix.lower() in blocked_extensions:
                    raise ValueError("File extension is blocked")
                if not resolved_path.exists() or not resolved_path.is_file():
                    raise FileNotFoundError("File not found")

                size = resolved_path.stat().st_size
                if size > max_single_file_size:
                    raise ValueError("File exceeds the single file size limit")
                if total_size + size > max_total_file_size:
                    raise ValueError("Files exceed the total size limit")

                mime_type = item.get("mime_type") or mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
                display_name = item.get("display_name") or resolved_path.name
                token = _encode_token({"path": str(resolved_path), "uid": self.uid, "id": uuid.uuid4().hex})
                total_size += size

                valid_files.append(
                    {
                        "id": token,
                        "name": display_name,
                        "description": item.get("description"),
                        "mime_type": mime_type,
                        "size": size,
                        "download_url": f"/api/v1/download-sent?token={quote(token)}",
                        "previewable": _is_previewable(mime_type),
                    }
                )
            except Exception as exc:
                errors.append({"path": raw_path or "", "error": str(exc)})

        return json.dumps(
            {
                "type": "files_to_user",
                "files": valid_files,
                "status": "success" if valid_files and not errors else "partial_success" if valid_files else "failed",
                "errors": errors,
                "allowed_file_send_dirs": allowed_dirs,
            },
            ensure_ascii=False,
        )


SEND_FILE_TO_USER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_file_to_user",
        "description": "Send existing local files to the user. Provide one or more absolute file paths. The tool validates paths and returns safe downloadable file metadata without exposing real server paths.",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": "Files to send to the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Absolute local file path."},
                            "display_name": {"type": "string", "description": "Optional display name."},
                            "description": {"type": "string", "description": "Optional file description."},
                            "mime_type": {"type": "string", "description": "Optional MIME type. If omitted, it is inferred from the file name."},
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": ["files"],
        },
    },
}
