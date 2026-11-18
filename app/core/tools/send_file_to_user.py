import asyncio
import base64
import errno
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import stat
import tempfile
import uuid
from collections.abc import Collection, Mapping
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
    SEND_FILE_TO_USER_TOOL_NAME,
)
from app.core.crud.account.file_download_cleanup import FileDownloadCleanupJournal
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


def get_file_download_cleanup_journal() -> FileDownloadCleanupJournal:
    return FileDownloadCleanupJournal(FILE_TOKEN_DIR)


def _sign_payload(payload: str) -> str:
    key = _get_encryption_key()
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _token_payload_error() -> ValueError:
    return ValueError(t(ERR_FILE_TOKEN_PAYLOAD_INVALID))


def _is_token_secret(secret: Any) -> bool:
    return isinstance(secret, str) and bool(secret) and secret.isascii() and all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in secret)


def _validate_token_data(data: Any, *, require_id: bool) -> dict[str, str]:
    if not isinstance(data, dict):
        raise _token_payload_error()
    if require_id and set(data) != _TOKEN_DATA_KEYS:
        raise _token_payload_error()

    uid = data.get("uid")
    if not isinstance(uid, str) or not uid.strip():
        raise _token_payload_error()

    raw_path = data.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise _token_payload_error()
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            raise _token_payload_error()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _token_payload_error() from exc

    normalized_data = {"uid": uid, "path": raw_path}
    if require_id:
        file_id = data.get("id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise _token_payload_error()
        normalized_data["id"] = file_id
    return normalized_data


def _normalize_token_data(data: Any, *, require_id: bool) -> tuple[dict[str, str], os.stat_result]:
    normalized_data = _validate_token_data(data, require_id=require_id)
    try:
        path = Path(normalized_data["path"]).resolve(strict=False)
    except (RuntimeError, ValueError) as exc:
        raise _token_payload_error() from exc

    if _is_sensitive_path(path):
        raise ValueError(t(ERR_FILE_SENSITIVE_NOT_ALLOWED))

    try:
        path_stat = path.stat()
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND)) from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))

    normalized_data["path"] = str(path)
    return normalized_data, path_stat


def _binding_key(data: dict[str, str]) -> str:
    normalized_payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid.uuid5(uuid.NAMESPACE_URL, normalized_payload).hex


def _is_token_binding_key(binding_key: Any) -> bool:
    return isinstance(binding_key, str) and len(binding_key) == 32 and all(character in "0123456789abcdef" for character in binding_key) and binding_key[12] == "5" and binding_key[16] in "89ab"


def _token_record_path(binding_key: str) -> Path:
    if not _is_token_binding_key(binding_key):
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))

    try:
        token_dir_path = Path(FILE_TOKEN_DIR).resolve(strict=False)
        record_path = token_dir_path / f"{binding_key}.json"
    except (RuntimeError, ValueError) as exc:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID)) from exc
    if record_path.is_symlink():
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))
    return record_path


def _read_token_record_from_path(record_path: Path, binding_key: str) -> tuple[dict[str, str], str, os.stat_result]:
    with record_path.open("r", encoding="utf-8") as record_file:
        record_stat = os.fstat(record_file.fileno())
        record = json.load(record_file)
    if not isinstance(record, dict) or set(record) != _TOKEN_RECORD_KEYS:
        raise _token_payload_error()
    stored_secret = record.get("secret")
    if not _is_token_secret(stored_secret):
        raise _token_payload_error()

    data = _validate_token_data(record.get("data"), require_id=True)
    if _binding_key(data) != binding_key:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))
    return data, stored_secret, record_stat


def _read_token_record(binding_key: str) -> tuple[dict[str, str], str, os.stat_result]:
    record_path = _token_record_path(binding_key)
    try:
        return _read_token_record_from_path(record_path, binding_key)
    except FileNotFoundError as exc:
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND)) from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _token_payload_error() from exc


def _source_file_missing(path: Path) -> bool:
    try:
        path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    return False


def _source_file_is_same(path: Path, initial_stat: os.stat_result) -> bool:
    try:
        current_stat = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(current_stat.st_mode) and os.path.samestat(initial_stat, current_stat)


def _raise_if_source_file_invalid(path: Path, initial_stat: os.stat_result) -> None:
    if not _source_file_is_same(path, initial_stat):
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))


def _remove_new_token_if_source_invalid(
    source_path: Path,
    initial_source_stat: os.stat_result,
    temp_path: Path,
    record_path: Path,
) -> None:
    if _source_file_is_same(source_path, initial_source_stat):
        return

    try:
        same_record = os.path.samestat(temp_path.lstat(), record_path.lstat())
    except (FileNotFoundError, NotADirectoryError):
        same_record = False

    if same_record:
        record_path.unlink(missing_ok=True)
    raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))


def _record_snapshot_matches(record_path: Path, initial_stat: os.stat_result) -> bool:
    current_stat = record_path.lstat()
    return os.path.samestat(initial_stat, current_stat) and initial_stat.st_mtime_ns == current_stat.st_mtime_ns and initial_stat.st_size == current_stat.st_size


def _should_delete_token(
    data: dict[str, str],
    modified_at_ns: int,
    *,
    uid: str | None,
    existing_uids: Collection[str] | None,
    removal_cutoffs: Mapping[Path, int] | None,
    removed_under: Mapping[Path, int],
    missing_under: Collection[Path],
    records_before: float | None,
) -> bool:
    source_path = Path(data["path"])
    path_cutoff = removal_cutoffs.get(source_path) if removal_cutoffs is not None else None
    if path_cutoff is not None and modified_at_ns <= path_cutoff:
        return True
    for root, cutoff in removed_under.items():
        if source_path.is_relative_to(root) and modified_at_ns <= cutoff:
            return True
    if records_before is not None and modified_at_ns / 1_000_000_000 >= records_before:
        return False
    if uid is not None and data["uid"] == uid:
        return True
    if existing_uids is not None and data["uid"] not in existing_uids:
        return True
    for root in missing_under:
        if not source_path.is_relative_to(root):
            continue
        if _source_file_missing(source_path):
            return True
    return False


def cleanup_file_download_tokens(
    *,
    uid: str | None = None,
    replay_removals: bool = False,
    missing_under: Collection[Path] = (),
    existing_uids: Collection[str] | None = None,
    records_before: float | None = None,
) -> int:
    journal = get_file_download_cleanup_journal()
    pending_ids: list[int] = []
    removal_cutoffs: dict[Path, int] = {}
    tree_cutoffs: dict[Path, int] = {}
    if replay_removals:
        pending_ids, removal_cutoffs, tree_cutoffs = journal.snapshot_pending()

    missing_under_paths = tuple(path for path in missing_under if path.is_absolute())
    if uid is None and not pending_ids and not missing_under_paths and existing_uids is None:
        return 0

    token_dir = Path(FILE_TOKEN_DIR)
    try:
        token_dir_stat = token_dir.lstat()
    except FileNotFoundError:
        return 0
    except NotADirectoryError as exc:
        raise OSError(errno.EINVAL, t(ERR_FILE_TOKEN_INVALID), token_dir) from exc

    if not stat.S_ISDIR(token_dir_stat.st_mode) or stat.S_ISLNK(token_dir_stat.st_mode) or getattr(token_dir_stat, "st_reparse_tag", 0):
        raise OSError(errno.EINVAL, t(ERR_FILE_TOKEN_INVALID), token_dir)

    deleted_count = 0
    first_error: OSError | None = None
    scan_succeeded = False
    try:
        with os.scandir(token_dir) as records:
            for record in records:
                try:
                    record_name = record.name
                    if len(record_name) != 37 or record_name[32:] != ".json" or not _is_token_binding_key(record_name[:32]):
                        continue

                    record_path = Path(record.path)
                    initial_record_stat = record_path.lstat()
                    if not stat.S_ISREG(initial_record_stat.st_mode) or getattr(initial_record_stat, "st_reparse_tag", 0):
                        continue

                    binding_key = record_name[:32]
                    data, _, _ = _read_token_record_from_path(record_path, binding_key)

                    if not _should_delete_token(
                        data,
                        initial_record_stat.st_mtime_ns,
                        uid=uid,
                        existing_uids=existing_uids,
                        removal_cutoffs=removal_cutoffs,
                        removed_under=tree_cutoffs,
                        missing_under=missing_under_paths,
                        records_before=records_before,
                    ):
                        continue

                    if not _record_snapshot_matches(record_path, initial_record_stat):
                        continue
                    record_path.unlink()
                    deleted_count += 1
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except (UnicodeError, ValueError):
                    continue
                except OSError as exc:
                    if first_error is None:
                        first_error = exc
        scan_succeeded = True

    except FileNotFoundError:
        pass
    except OSError as exc:
        if first_error is None:
            first_error = exc

    if first_error is not None:
        raise first_error
    if scan_succeeded and pending_ids:
        journal.acknowledge(pending_ids)
    return deleted_count


def _encode_token(data: dict[str, Any]) -> str:
    normalized_data, source_stat = _normalize_token_data(data, require_id=True)
    source_path = Path(normalized_data["path"])
    binding_key = _binding_key(normalized_data)
    record_path = _token_record_path(binding_key)
    journal = get_file_download_cleanup_journal()
    _raise_if_source_file_invalid(source_path, source_stat)
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

        with journal.publication_guard(source_path, source_stat):
            _raise_if_source_file_invalid(source_path, source_stat)
            try:
                os.link(temp_path, record_path)
            except FileExistsError:
                _, stored_secret, record_stat = _read_token_record(binding_key)
                if journal.revokes(source_path, record_stat.st_mtime_ns, source_stat):
                    raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
                _raise_if_source_file_invalid(source_path, source_stat)
                if not _record_snapshot_matches(record_path, record_stat):
                    raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
                return f"{TOKEN_VERSION}.{binding_key}.{stored_secret}"
            _remove_new_token_if_source_invalid(source_path, source_stat, temp_path, record_path)
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
        data = _validate_token_data(data, require_id=False)
        _check_token_owner(data, uid, is_admin)
        normalized_data, _ = _normalize_token_data(data, require_id=False)
        return Path(normalized_data["path"])

    if version != TOKEN_VERSION:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))

    if not _is_token_secret(signature):
        raise ValueError(t(ERR_FILE_TOKEN_SIGNATURE_INVALID))
    record_path = _token_record_path(payload)
    data, expected_secret, record_stat = _read_token_record(payload)
    if not secrets.compare_digest(signature, expected_secret):
        raise ValueError(t(ERR_FILE_TOKEN_SIGNATURE_INVALID))
    _check_token_owner(data, uid, is_admin)
    normalized_data, source_stat = _normalize_token_data(data, require_id=True)
    if normalized_data["path"] != data["path"]:
        raise ValueError(t(ERR_FILE_TOKEN_INVALID))
    normalized_path = Path(normalized_data["path"])
    journal = get_file_download_cleanup_journal()
    if journal.revokes(normalized_path, record_stat.st_mtime_ns, source_stat) or not _record_snapshot_matches(record_path, record_stat):
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
    return normalized_path


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
        "name": SEND_FILE_TO_USER_TOOL_NAME,
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
