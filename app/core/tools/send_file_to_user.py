import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.constants import (
    ERR_FILE_ARGUMENT_INVALID,
    ERR_FILE_EXTENSION_BLOCKED,
    ERR_FILE_NOT_FOUND,
    ERR_FILE_PATH_MISSING,
    ERR_FILE_PATH_NOT_ABSOLUTE,
    ERR_FILE_SENSITIVE_NOT_ALLOWED,
    ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED,
    ERR_FILE_TOKEN_INVALID,
    ERR_FILE_TOKEN_PAYLOAD_INVALID,
    ERR_FILE_TOKEN_SIGNATURE_INVALID,
    ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED,
    ERR_TOOL_OPERATION_DIRS_UNCONFIGURED,
    ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS,
    ERR_UNAUTHORIZED,
    MSG_FILE_COUNT_TRUNCATED,
)
from app.core.crypto import _get_encryption_key
from app.core.i18n import t
from app.core.paths import DATA_DIR
from app.core.utils.operation_directories import (
    get_allowed_operation_dirs,
    is_path_within_allowed_operation_dirs,
    normalize_allowed_operation_dirs,
)

from .base import BaseExecutor

DEFAULT_MAX_FILE_COUNT = 10
DEFAULT_MAX_SINGLE_FILE_SIZE_MB = 50
DEFAULT_MAX_TOTAL_FILE_SIZE_MB = 100
TOKEN_VERSION = "v2"
LEGACY_TOKEN_VERSION = "v1"
FILE_TOKEN_DIR = DATA_DIR / "file_download_tokens"
_TOKEN_DATA_KEYS = frozenset({"uid", "path", "id"})
_TOKEN_RECORD_KEYS = frozenset({"data", "secret"})
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


def _token_payload_error() -> ValueError:
    return ValueError(t(ERR_FILE_TOKEN_PAYLOAD_INVALID))


def _normalize_token_data(data: Any, *, require_id: bool) -> dict[str, str]:
    if not isinstance(data, dict):
        raise _token_payload_error()
    if require_id and set(data) != _TOKEN_DATA_KEYS:
        raise _token_payload_error()

    uid = data.get("uid")
    if not isinstance(uid, str) or not uid.strip():
        raise _token_payload_error()

    raw_path = data.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise _token_payload_error()
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            raise _token_payload_error()
        path = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _token_payload_error() from exc

    if _is_sensitive_path(path):
        raise ValueError(t(ERR_FILE_SENSITIVE_NOT_ALLOWED))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))

    normalized_data = {"uid": uid, "path": str(path)}
    if require_id:
        file_id = data.get("id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise _token_payload_error()
        normalized_data["id"] = file_id
    return normalized_data


def _binding_key(data: dict[str, str]) -> str:
    normalized_payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid.uuid5(uuid.NAMESPACE_URL, normalized_payload).hex


def _token_record_path(binding_key: str) -> Path:
    if not isinstance(binding_key, str) or len(binding_key) != 32 or any(character not in "0123456789abcdef" for character in binding_key):
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))

    try:
        token_dir_path = Path(FILE_TOKEN_DIR).resolve(strict=False)
        record_path = token_dir_path / f"{binding_key}.json"
        if record_path.is_symlink():
            raise ValueError(t(ERR_FILE_TOKEN_INVALID))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID)) from exc
    return record_path


def _read_token_record(binding_key: str) -> tuple[dict[str, str], str]:
    record_path = _token_record_path(binding_key)
    try:
        with record_path.open("r", encoding="utf-8") as record_file:
            record = json.load(record_file)
    except FileNotFoundError as exc:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID)) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _token_payload_error() from exc

    if not isinstance(record, dict) or set(record) != _TOKEN_RECORD_KEYS:
        raise _token_payload_error()
    stored_secret = record.get("secret")
    if not isinstance(stored_secret, str) or not stored_secret.isascii() or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in stored_secret):
        raise _token_payload_error()

    data = _normalize_token_data(record.get("data"), require_id=True)
    if _binding_key(data) != binding_key:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))
    return data, stored_secret


def _encode_token(data: dict[str, Any]) -> str:
    normalized_data = _normalize_token_data(data, require_id=True)
    binding_key = _binding_key(normalized_data)
    record_path = _token_record_path(binding_key)
    token_dir = Path(FILE_TOKEN_DIR)
    token_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{binding_key}.", suffix=".tmp", dir=str(token_dir))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as record_file:
            json.dump(
                {"data": normalized_data, "secret": secret},
                record_file,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            record_file.flush()
            os.fsync(record_file.fileno())

        try:
            os.link(temp_path, record_path)
        except FileExistsError:
            _, stored_secret = _read_token_record(binding_key)
            return f"{TOKEN_VERSION}.{binding_key}.{stored_secret}"
        return f"{TOKEN_VERSION}.{binding_key}.{secret}"
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _check_token_owner(data: dict[str, str], uid: str | None, is_admin: bool) -> None:
    if uid is not None and not is_admin and data["uid"] != uid:
        raise ValueError(t(ERR_UNAUTHORIZED))


def resolve_file_token(token: str, *, uid: str | None = None, is_admin: bool = False) -> Path:
    if not isinstance(token, str):
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))

    version, payload, signature = parts
    if version == LEGACY_TOKEN_VERSION:
        if not signature.isascii():
            raise ValueError(t(ERR_FILE_TOKEN_SIGNATURE_INVALID))
        expected_signature = _sign_payload(payload)
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError(t(ERR_FILE_TOKEN_SIGNATURE_INVALID))

        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        except Exception as exc:
            raise _token_payload_error() from exc
        normalized_data = _normalize_token_data(data, require_id=False)
        _check_token_owner(normalized_data, uid, is_admin)
        return Path(normalized_data["path"])

    if version != TOKEN_VERSION:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))

    data, expected_secret = _read_token_record(payload)
    if not signature.isascii() or not secrets.compare_digest(signature, expected_secret):
        raise ValueError(t(ERR_FILE_TOKEN_SIGNATURE_INVALID))
    _check_token_owner(data, uid, is_admin)
    return Path(data["path"])


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


class SendFileToUserExecutor(BaseExecutor):
    requires_audit = False

    def _get_tool_config(self) -> Any:
        return getattr(self.cfg, "tool", None)

    def _get_allowed_operation_dirs(self) -> list[str]:
        return get_allowed_operation_dirs(self.cfg)

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
        allowed_dirs = self._get_allowed_operation_dirs()
        if normalized_files is None:
            return json.dumps(
                {
                    "type": "files_to_user",
                    "files": [],
                    "status": "failed",
                    "errors": [{"path": "", "error": t(ERR_FILE_ARGUMENT_INVALID)}],
                    "allowed_operation_dirs": allowed_dirs,
                },
                ensure_ascii=False,
            )

        max_file_count, max_single_file_size, max_total_file_size, blocked_extensions = self._get_limit_config()
        valid_files = []
        errors = []
        total_size = 0

        if not normalize_allowed_operation_dirs(allowed_dirs):
            return json.dumps(
                {
                    "type": "files_to_user",
                    "files": [],
                    "status": "failed",
                    "errors": [{"path": "", "error": t(ERR_TOOL_OPERATION_DIRS_UNCONFIGURED)}],
                    "allowed_operation_dirs": allowed_dirs,
                },
                ensure_ascii=False,
            )

        if len(normalized_files) > max_file_count:
            normalized_files = normalized_files[:max_file_count]
            errors.append({"path": "", "error": t(MSG_FILE_COUNT_TRUNCATED, max_file_count=max_file_count)})

        for item in normalized_files:
            raw_path = item.get("path") if isinstance(item, dict) else None
            try:
                if not raw_path:
                    raise ValueError(t(ERR_FILE_PATH_MISSING))

                path = Path(raw_path)
                if not path.is_absolute():
                    raise ValueError(t(ERR_FILE_PATH_NOT_ABSOLUTE))

                resolved_path = path.resolve()
                if not is_path_within_allowed_operation_dirs(resolved_path, allowed_dirs):
                    raise ValueError(t(ERR_TOOL_PATH_OUTSIDE_ALLOWED_OPERATION_DIRS))
                if _is_sensitive_path(resolved_path):
                    raise ValueError(t(ERR_FILE_SENSITIVE_NOT_ALLOWED))
                if resolved_path.suffix.lower() in blocked_extensions:
                    raise ValueError(t(ERR_FILE_EXTENSION_BLOCKED))
                if not resolved_path.exists() or not resolved_path.is_file():
                    raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))

                size = resolved_path.stat().st_size
                if size > max_single_file_size:
                    raise ValueError(t(ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED))
                if total_size + size > max_total_file_size:
                    raise ValueError(t(ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED))

                mime_type = item.get("mime_type") or mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
                display_name = item.get("display_name") or resolved_path.name
                token = await asyncio.to_thread(_encode_token, {"path": str(resolved_path), "uid": self.uid, "id": uuid.uuid4().hex})
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
                "allowed_operation_dirs": allowed_dirs,
            },
            ensure_ascii=False,
        )


SEND_FILE_TO_USER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_file_to_user",
        "description": "Send existing local files to the user. Provide one or more absolute file paths. All paths must be absolute. The tool validates paths and returns safe downloadable file metadata without exposing real server paths.",
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
