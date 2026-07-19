import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

READ_TEXT_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_text_file",
        "description": "Read a UTF-8 text file. Relative paths are resolved from the supplied working directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "An absolute path or a path relative to the working directory.",
                },
            },
            "required": ["path"],
        },
    },
}


@dataclass(frozen=True, slots=True)
class TextFileReadResult:
    original_path: str
    absolute_path: str
    resolved_path: str
    exists: bool
    file_type: str
    size: int | None
    sha256: str | None
    status: str
    truncated: bool
    content: str | None = None
    bytes_read: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _path_parts(path: str | Path, working_directory: str | Path) -> tuple[str, Path, Path]:
    original_path = str(path)
    source_path = Path(path)
    if not source_path.is_absolute():
        source_path = Path(working_directory) / source_path
    absolute_path = Path(os.path.abspath(source_path))
    return original_path, absolute_path, absolute_path.resolve(strict=False)


def _error_result(
    *,
    original_path: str,
    absolute_path: Path,
    resolved_path: Path,
    exists: bool,
    file_type: str,
    size: int | None = None,
    status: str,
    error: str,
) -> TextFileReadResult:
    return TextFileReadResult(
        original_path=original_path,
        absolute_path=str(absolute_path),
        resolved_path=str(resolved_path),
        exists=exists,
        file_type=file_type,
        size=size,
        sha256=None,
        status=status,
        truncated=False,
        error=error,
    )


def read_text_file(path: str | Path, *, working_directory: str | Path, max_bytes: int) -> TextFileReadResult:
    """Read a regular UTF-8 file without applying a path allowlist.

    The returned digest covers the complete file, while ``content`` is limited
    to ``max_bytes``. This lets the audit record detect changes even when the
    model received truncated evidence.
    """
    original_path, absolute_path, resolved_path = _path_parts(path, working_directory)
    try:
        path_stat = absolute_path.lstat()
    except FileNotFoundError:
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=False,
            file_type="missing",
            status="missing",
            error="file does not exist",
        )
    except OSError:
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=False,
            file_type="unknown",
            status="unreadable",
            error="file metadata is unavailable",
        )

    if stat.S_ISLNK(path_stat.st_mode):
        file_type = "symlink"
    elif stat.S_ISREG(path_stat.st_mode):
        file_type = "regular_file"
    elif stat.S_ISDIR(path_stat.st_mode):
        file_type = "directory"
    else:
        file_type = "other"

    if file_type not in {"regular_file", "symlink"}:
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=True,
            file_type=file_type,
            status="not_regular",
            error="path is not a regular file",
        )

    try:
        target_stat = resolved_path.stat()
    except OSError:
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=True,
            file_type=file_type,
            status="unreadable",
            error="file target metadata is unavailable",
        )
    if not stat.S_ISREG(target_stat.st_mode):
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=True,
            file_type=file_type,
            status="not_regular",
            error="path target is not a regular file",
        )

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=True,
            file_type=file_type,
            status="limit_exceeded",
            error="file byte limit is exhausted",
        )

    digest = hashlib.sha256()
    content_parts: list[bytes] = []
    content_bytes = 0
    total_size = 0
    try:
        with absolute_path.open("rb") as file_handle:
            opened_stat = os.fstat(file_handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                return _error_result(
                    original_path=original_path,
                    absolute_path=absolute_path,
                    resolved_path=resolved_path,
                    exists=True,
                    file_type=file_type,
                    status="not_regular",
                    error="path is not a regular file",
                )
            while chunk := file_handle.read(1024 * 1024):
                digest.update(chunk)
                total_size += len(chunk)
                if content_bytes < max_bytes:
                    selected = chunk[: max_bytes - content_bytes]
                    content_parts.append(selected)
                    content_bytes += len(selected)
    except (OSError, UnicodeError):
        return _error_result(
            original_path=original_path,
            absolute_path=absolute_path,
            resolved_path=resolved_path,
            exists=True,
            file_type=file_type,
            status="unreadable",
            error="file could not be read",
        )

    selected_content = b"".join(content_parts)
    try:
        content = selected_content.decode("utf-8")
    except UnicodeDecodeError:
        return TextFileReadResult(
            original_path=original_path,
            absolute_path=str(absolute_path),
            resolved_path=str(resolved_path),
            exists=True,
            file_type=file_type,
            size=total_size,
            sha256=digest.hexdigest(),
            status="unreadable",
            truncated=total_size > max_bytes,
            bytes_read=content_bytes,
            error="file is not valid UTF-8 text",
        )

    return TextFileReadResult(
        original_path=original_path,
        absolute_path=str(absolute_path),
        resolved_path=str(resolved_path),
        exists=True,
        file_type=file_type,
        size=total_size,
        sha256=digest.hexdigest(),
        status="ok",
        truncated=total_size > max_bytes,
        content=content,
        bytes_read=content_bytes,
    )
