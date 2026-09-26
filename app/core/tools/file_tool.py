import bisect
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.core.constants import (
    ERR_FILE_NOT_FOUND,
    ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS,
    ERR_FILE_TOOL_CONTENT_REQUIRED,
    ERR_FILE_TOOL_EXPECTED_REPLACEMENTS_INVALID,
    ERR_FILE_TOOL_EXPECTED_REPLACEMENTS_REQUIRED,
    ERR_FILE_TOOL_NOT_REGULAR,
    ERR_FILE_TOOL_OPERATION_INVALID,
    ERR_FILE_TOOL_PATTERN_REQUIRED,
    ERR_FILE_TOOL_READ_RANGE_INVALID,
    ERR_FILE_TOOL_READ_RANGE_REQUIRED,
    ERR_FILE_TOOL_REGEX_INVALID,
    ERR_FILE_TOOL_REPLACEMENT_COUNT_MISMATCH,
    ERR_FILE_TOOL_REPLACEMENT_REQUIRED,
)
from app.core.i18n import t
from app.core.paths import get_user_temp_dir
from app.core.utils.operation_directories import get_allowed_operation_dirs, normalize_allowed_operation_dirs

from .base import BaseExecutor

FILE_TOOL_OPERATIONS = ("read", "write", "replace", "find")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_file_tool_target_path(
    path: str,
    workspace_path: str | Path,
    allowed_operation_dirs: list[str] | None = None,
) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))

    workspace = Path(workspace_path).resolve(strict=False)
    candidate = Path(path)
    portable_absolute = PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
    if portable_absolute and not candidate.is_absolute():
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))
    if candidate.is_absolute():
        target = candidate.resolve(strict=False)
    else:
        target = (workspace / candidate).resolve(strict=False)

    roots = [workspace, *normalize_allowed_operation_dirs(allowed_operation_dirs)]
    if target == workspace or not any(_is_within(target, root) for root in roots):
        raise ValueError(t(ERR_FILE_PATH_OUTSIDE_ALLOWED_DIRS))
    return target


def _format_numbered_lines(lines: list[str], start_line: int, end_line: int) -> str:
    if not lines or start_line > end_line:
        return ""
    return "\n".join(f"{line_number}: {lines[line_number - 1]}" for line_number in range(start_line, end_line + 1))


def _line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(index + 1 for index, character in enumerate(text) if character == "\n")
    return starts


def _line_number(starts: list[int], position: int) -> int:
    return max(1, bisect.bisect_right(starts, max(position, 0)))


class FileToolExecutor(BaseExecutor):
    requires_audit = True

    def __init__(self, project_root: str, uid: str = "default"):
        super().__init__(project_root, uid)
        self.user_temp_dir = get_user_temp_dir(self.project_root, uid)
        self.user_temp_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_target_path(self, path: str) -> Path:
        return resolve_file_tool_target_path(
            path,
            self.user_temp_dir,
            get_allowed_operation_dirs(self.cfg),
        )

    @staticmethod
    def _failed(operation: str, error: str, **details: Any) -> str:
        return json.dumps(
            {
                "status": "failed",
                "operation": operation,
                "error": error,
                **details,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _read_text_sync(target: Path) -> str:
        if not target.exists():
            raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
        if not target.is_file():
            raise ValueError(t(ERR_FILE_TOOL_NOT_REGULAR))
        return target.read_text(encoding="utf-8")

    @staticmethod
    def _read_lines_sync(target: Path, start_line: int, end_line: int) -> tuple[list[str], int]:
        if not target.exists():
            raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
        if not target.is_file():
            raise ValueError(t(ERR_FILE_TOOL_NOT_REGULAR))
        selected_lines: list[str] = []
        total_lines = 0
        with target.open("r", encoding="utf-8") as file_handle:
            for total_lines, raw_line in enumerate(file_handle, start=1):
                if start_line <= total_lines <= end_line:
                    selected_lines.append(raw_line.rstrip("\r\n"))
        return selected_lines, total_lines

    @staticmethod
    def _write_text_sync(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not target.is_file():
            raise ValueError(t(ERR_FILE_TOOL_NOT_REGULAR))
        target.write_text(content, encoding="utf-8")

    async def _read(self, target: Path, *, start_line: int | None, end_line: int | None) -> str:
        if start_line is None or end_line is None:
            return self._failed("read", t(ERR_FILE_TOOL_READ_RANGE_REQUIRED))
        if isinstance(start_line, bool) or isinstance(end_line, bool) or not isinstance(start_line, int) or not isinstance(end_line, int) or start_line < 1 or end_line < start_line:
            return self._failed("read", t(ERR_FILE_TOOL_READ_RANGE_INVALID))

        selected_lines, total_lines = await self.run_sync(self._read_lines_sync, target, start_line, end_line)
        actual_end = min(end_line, total_lines)
        content = "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(selected_lines, start=start_line))
        return json.dumps(
            {
                "status": "success",
                "operation": "read",
                "path": str(target),
                "start_line": start_line,
                "end_line": actual_end,
                "total_lines": total_lines,
                "content": content,
            },
            ensure_ascii=False,
        )

    async def _write(self, target: Path, *, content: str | None) -> str:
        if not isinstance(content, str):
            return self._failed("write", t(ERR_FILE_TOOL_CONTENT_REQUIRED))
        await self.run_sync(self._write_text_sync, target, content)
        return json.dumps(
            {
                "status": "success",
                "operation": "write",
                "path": str(target),
                "bytes_written": len(content.encode("utf-8")),
            },
            ensure_ascii=False,
        )

    async def _replace(
        self,
        target: Path,
        *,
        pattern: str | None,
        replacement: str | None,
        expected_replacements: int | None,
    ) -> str:
        if not isinstance(pattern, str) or not pattern:
            return self._failed("replace", t(ERR_FILE_TOOL_PATTERN_REQUIRED))
        if not isinstance(replacement, str):
            return self._failed("replace", t(ERR_FILE_TOOL_REPLACEMENT_REQUIRED))
        if expected_replacements is None:
            return self._failed("replace", t(ERR_FILE_TOOL_EXPECTED_REPLACEMENTS_REQUIRED))
        if isinstance(expected_replacements, bool) or not isinstance(expected_replacements, int) or expected_replacements < 1:
            return self._failed("replace", t(ERR_FILE_TOOL_EXPECTED_REPLACEMENTS_INVALID))

        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            return self._failed("replace", t(ERR_FILE_TOOL_REGEX_INVALID, error=str(exc)))

        text = await self.run_sync(self._read_text_sync, target)
        matches = list(regex.finditer(text))
        if len(matches) != expected_replacements:
            return self._failed(
                "replace",
                t(
                    ERR_FILE_TOOL_REPLACEMENT_COUNT_MISMATCH,
                    expected=expected_replacements,
                    actual=len(matches),
                ),
                expected_replacements=expected_replacements,
                matched=len(matches),
            )

        parts: list[str] = []
        replacement_spans: list[tuple[int, int]] = []
        cursor = 0
        output_length = 0
        try:
            for match in matches:
                prefix = text[cursor : match.start()]
                parts.append(prefix)
                output_length += len(prefix)
                expanded = match.expand(replacement)
                start_position = output_length
                parts.append(expanded)
                output_length += len(expanded)
                replacement_spans.append((start_position, output_length))
                cursor = match.end()
        except re.error as exc:
            return self._failed("replace", t(ERR_FILE_TOOL_REGEX_INVALID, error=str(exc)))
        parts.append(text[cursor:])
        replaced_text = "".join(parts)

        await self.run_sync(self._write_text_sync, target, replaced_text)
        starts = _line_starts(replaced_text)
        lines = replaced_text.splitlines()
        targets = []
        for start_position, end_position in replacement_spans:
            start_line = _line_number(starts, start_position)
            end_line = _line_number(starts, max(start_position, end_position - 1))
            targets.append(
                {
                    "start_line": start_line,
                    "end_line": end_line,
                    "content": _format_numbered_lines(lines, start_line, min(end_line, len(lines))),
                }
            )
        return json.dumps(
            {
                "status": "success",
                "operation": "replace",
                "path": str(target),
                "replaced": len(matches),
                "targets": targets,
            },
            ensure_ascii=False,
        )

    async def _find(self, target: Path, *, pattern: str | None) -> str:
        if not isinstance(pattern, str) or not pattern:
            return self._failed("find", t(ERR_FILE_TOOL_PATTERN_REQUIRED))
        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            return self._failed("find", t(ERR_FILE_TOOL_REGEX_INVALID, error=str(exc)))

        text = await self.run_sync(self._read_text_sync, target)
        starts = _line_starts(text)
        lines = text.splitlines()
        matches = []
        for match in regex.finditer(text):
            start_line = _line_number(starts, match.start())
            end_line = _line_number(starts, max(match.start(), match.end() - 1))
            context_start_line = max(1, start_line - 5)
            context_end_line = min(len(lines), end_line + 5)
            matches.append(
                {
                    "start_line": start_line,
                    "end_line": end_line,
                    "context_start_line": context_start_line,
                    "context_end_line": context_end_line,
                    "content": _format_numbered_lines(lines, context_start_line, context_end_line),
                }
            )
        return json.dumps(
            {
                "status": "success",
                "operation": "find",
                "path": str(target),
                "match_count": len(matches),
                "matches": matches,
            },
            ensure_ascii=False,
        )

    async def execute(
        self,
        operation: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        content: str | None = None,
        pattern: str | None = None,
        replacement: str | None = None,
        expected_replacements: int | None = None,
    ) -> str:
        if operation not in FILE_TOOL_OPERATIONS:
            return self._failed(str(operation), t(ERR_FILE_TOOL_OPERATION_INVALID, operation=operation))
        try:
            target = self._resolve_target_path(path)
            if operation == "read":
                return await self._read(target, start_line=start_line, end_line=end_line)
            if operation == "write":
                return await self._write(target, content=content)
            if operation == "replace":
                return await self._replace(
                    target,
                    pattern=pattern,
                    replacement=replacement,
                    expected_replacements=expected_replacements,
                )
            return await self._find(target, pattern=pattern)
        except UnicodeDecodeError:
            return self._failed(operation, t(ERR_FILE_TOOL_NOT_REGULAR))
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return self._failed(operation, str(exc))


FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_tool",
        "description": "Read, overwrite, regex-replace, or regex-find UTF-8 text files. Relative paths stay inside the user workspace; absolute paths must be inside an allowed operation directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(FILE_TOOL_OPERATIONS),
                    "description": "File operation: read, write, replace, or find.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative workspace path or absolute path inside an allowed operation directory.",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Required for read. Inclusive 1-based start line.",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Required for read. Inclusive 1-based end line and must be >= start_line.",
                },
                "content": {
                    "type": "string",
                    "description": "Required for write. Complete file content; write always fully overwrites the target.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Required for replace and find. Python regular expression evaluated with multiline anchors enabled.",
                },
                "replacement": {
                    "type": "string",
                    "description": "Required for replace. Python regular-expression replacement string; capture-group references are supported.",
                },
                "expected_replacements": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Required for replace. Exact number of matches that must exist before any write occurs.",
                },
            },
            "required": ["operation", "path"],
            "additionalProperties": False,
        },
    },
}
