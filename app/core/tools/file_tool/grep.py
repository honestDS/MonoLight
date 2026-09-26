import fnmatch
import re
from pathlib import Path

from app.core.constants import ERR_FILE_NOT_FOUND, ERR_FILE_TOOL_GREP_OPTIONS_INVALID, ERR_FILE_TOOL_PATTERN_REQUIRED, ERR_FILE_TOOL_REGEX_INVALID
from app.core.i18n import t

from .text import format_numbered_lines, line_number_at, normalize_line_endings, read_text_source


def _matches_patterns(path: Path, root: Path, patterns: list[str] | None) -> bool:
    if not patterns:
        return True
    relative = path.relative_to(root).as_posix() if path != root else path.name
    return any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in patterns)


def _iter_files(target: Path, *, recursive: bool, include: list[str] | None, exclude: list[str] | None):
    if not target.exists():
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return

    iterator = target.rglob("*") if recursive else target.glob("*")
    for candidate in iterator:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if include and not _matches_patterns(candidate, target, include):
            continue
        if exclude and _matches_patterns(candidate, target, exclude):
            continue
        yield candidate


def grep_files(
    target: Path,
    *,
    pattern: str | None,
    recursive: bool,
    include: list[str] | None,
    exclude: list[str] | None,
    ignore_case: bool,
    context_lines: int,
    max_results: int,
) -> dict:
    if not isinstance(pattern, str) or not pattern:
        return {"status": "failed", "operation": "grep", "error": t(ERR_FILE_TOOL_PATTERN_REQUIRED)}
    if (
        not isinstance(recursive, bool)
        or not isinstance(ignore_case, bool)
        or isinstance(context_lines, bool)
        or not isinstance(context_lines, int)
        or not 0 <= context_lines <= 20
        or isinstance(max_results, bool)
        or not isinstance(max_results, int)
        or not 1 <= max_results <= 1000
        or (include is not None and (not isinstance(include, list) or not all(isinstance(item, str) and item for item in include)))
        or (exclude is not None and (not isinstance(exclude, list) or not all(isinstance(item, str) and item for item in exclude)))
    ):
        return {"status": "failed", "operation": "grep", "error": t(ERR_FILE_TOOL_GREP_OPTIONS_INVALID)}

    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return {"status": "failed", "operation": "grep", "error": t(ERR_FILE_TOOL_REGEX_INVALID, error=str(exc))}

    matches: list[dict] = []
    files_scanned = 0
    matched_files: set[str] = set()
    truncated = False

    for candidate in _iter_files(target, recursive=recursive, include=include, exclude=exclude):
        try:
            source = read_text_source(candidate)
        except UnicodeDecodeError:
            continue
        files_scanned += 1
        text = normalize_line_endings(source.text)
        lines = text.splitlines()
        for match in regex.finditer(text):
            start_line = line_number_at(text, match.start())
            end_line = line_number_at(text, max(match.start(), match.end() - 1))
            context_start = max(1, start_line - context_lines)
            context_end = min(len(lines), end_line + context_lines)
            matches.append(
                {
                    "path": str(candidate),
                    "start_line": start_line,
                    "end_line": end_line,
                    "context_start_line": context_start,
                    "context_end_line": context_end,
                    "content": format_numbered_lines(lines, context_start, context_end),
                }
            )
            matched_files.add(str(candidate))
            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break

    return {
        "status": "success",
        "operation": "grep",
        "path": str(target),
        "files_scanned": files_scanned,
        "matched_file_count": len(matched_files),
        "match_count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }
