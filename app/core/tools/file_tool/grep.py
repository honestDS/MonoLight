import fnmatch
import json
import re
import sys
from array import array
from bisect import bisect_right
from itertools import accumulate, chain
from pathlib import Path

from app.core.constants import (
    ERR_FILE_NOT_FOUND,
    ERR_FILE_TOOL_GREP_OPTIONS_INVALID,
    ERR_FILE_TOOL_PATTERN_REQUIRED,
    ERR_FILE_TOOL_REGEX_INVALID,
    FILE_TOOL_GREP_JSON_CHUNK_CHARS,
    FILE_TOOL_GREP_MAX_OUTPUT_BYTES,
    MSG_FILE_TOOL_GREP_CONTENT_OMITTED,
)
from app.core.i18n import t

from .text import format_numbered_lines, normalize_line_endings, read_text_source


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _numbered_lines_min_chars(line_starts: array, start_line: int, end_line: int) -> int:
    if start_line > end_line:
        return 0
    line_count = end_line - start_line + 1
    span_chars = line_starts[end_line] - line_starts[start_line - 1] - 1
    return span_chars + line_count * len(f"{start_line}: ")


def _text_json_bytes(
    text: str,
    start: int,
    end: int,
    budget: int,
    chunk_sizes: dict[int, int],
) -> int | None:
    if end - start > budget:
        return None

    total = 0
    position = start
    while position < end:
        next_end = min(
            end,
            ((position // FILE_TOOL_GREP_JSON_CHUNK_CHARS) + 1) * FILE_TOOL_GREP_JSON_CHUNK_CHARS,
        )
        is_full_chunk = next_end - position == FILE_TOOL_GREP_JSON_CHUNK_CHARS
        chunk_bytes = chunk_sizes.get(position) if is_full_chunk else None
        if chunk_bytes is None:
            chunk_bytes = _json_size(text[position:next_end]) - 2
            if is_full_chunk:
                chunk_sizes[position] = chunk_bytes
        total += chunk_bytes
        if total > budget:
            return None
        position = next_end
    return total


def _numbered_lines_json_bytes(
    text: str,
    line_starts: array,
    start_line: int,
    end_line: int,
    budget: int,
    chunk_sizes: dict[int, int],
) -> int | None:
    if _numbered_lines_min_chars(line_starts, start_line, end_line) > budget:
        return None

    total = 0
    for line_number in range(start_line, end_line + 1):
        prefix_bytes = len(f"{line_number}: ")
        separator_bytes = 2 if line_number > start_line else 0  # JSON escapes "\n" to two bytes.
        line_budget = budget - total - prefix_bytes - separator_bytes
        if line_budget < 0:
            return None
        line_start = line_starts[line_number - 1]
        line_end = line_starts[line_number] - 1
        line_bytes = _text_json_bytes(text, line_start, line_end, line_budget, chunk_sizes)
        if line_bytes is None:
            return None
        total += prefix_bytes + separator_bytes + line_bytes
    return total


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
    before_chars: int | None = None,
    after_chars: int | None = None,
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
        or any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0) for value in (before_chars, after_chars))
        or (include is not None and (not isinstance(include, list) or not all(isinstance(item, str) and item for item in include)))
        or (exclude is not None and (not isinstance(exclude, list) or not all(isinstance(item, str) and item for item in exclude)))
    ):
        return {"status": "failed", "operation": "grep", "error": t(ERR_FILE_TOOL_GREP_OPTIONS_INVALID)}

    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return {"status": "failed", "operation": "grep", "error": t(ERR_FILE_TOOL_REGEX_INVALID, error=str(exc))}

    # Reserve maximum count fields and the omission instruction so incremental match accounting stays within the final dump budget.
    result = {
        "status": "success",
        "operation": "grep",
        "path": str(target),
        "instruction": t(MSG_FILE_TOOL_GREP_CONTENT_OMITTED),
        "files_scanned": sys.maxsize,
        "matched_file_count": max_results,
        "match_count": max_results,
        "truncated": False,
        "matches": [],
    }
    remaining_bytes = FILE_TOOL_GREP_MAX_OUTPUT_BYTES - _json_size(result)
    matches: list[dict] = []
    files_scanned = 0
    matched_files: set[str] = set()
    truncated = False
    snippet_mode = before_chars is not None or after_chars is not None

    for candidate in _iter_files(target, recursive=recursive, include=include, exclude=exclude):
        try:
            source = read_text_source(candidate)
        except UnicodeDecodeError:
            continue
        files_scanned += 1
        text = normalize_line_endings(source.text)
        match_iterator = regex.finditer(text)
        first_match = next(match_iterator, None)
        if first_match is None:
            continue
        lines = text.splitlines()
        line_starts = array("Q", accumulate((len(line) + 1 for line in lines), initial=0))
        candidate_path = str(candidate)
        chunk_sizes: dict[int, int] = {}
        for match in chain((first_match,), match_iterator):
            match_start = match.start()
            match_end = match.end()
            start_line = bisect_right(line_starts, match_start)
            end_position = max(match_start, match_end - 1)
            end_line = bisect_right(line_starts, end_position)
            if snippet_mode:
                snippet_start = max(0, match_start - (before_chars or 0))
                snippet_end = min(len(text), match_end + (after_chars or 0))
                context_start = bisect_right(line_starts, snippet_start)
                snippet_end_position = max(snippet_start, snippet_end - 1)
                context_end = bisect_right(line_starts, snippet_end_position)
            else:
                context_start = max(1, start_line - context_lines)
                context_end = min(len(lines), end_line + context_lines)
            start_column = match_start - line_starts[start_line - 1] + 1
            end_column = end_position - line_starts[end_line - 1] + 1

            match_payload = {
                "path": candidate_path,
                "start_line": start_line,
                "end_line": end_line,
                "start_column": start_column,
                "end_column": end_column,
                "context_start_line": context_start,
                "context_end_line": context_end,
                "content": "",
                "content_omitted": True,
            }
            match_size = _json_size(match_payload)
            match_cost = match_size + (2 if matches else 0)
            if match_cost > remaining_bytes:
                truncated = True
                break

            matches.append(match_payload)
            remaining_bytes -= match_cost
            matched_files.add(candidate_path)

            content_budget = remaining_bytes - 1  # "false" is one byte longer than "true".
            if snippet_mode:
                content_bytes = _text_json_bytes(text, snippet_start, snippet_end, content_budget, chunk_sizes)
            else:
                content_bytes = _numbered_lines_json_bytes(
                    text,
                    line_starts,
                    context_start,
                    context_end,
                    content_budget,
                    chunk_sizes,
                )
            if content_bytes is not None:
                if snippet_mode:
                    match_payload["content"] = text[snippet_start:snippet_end]
                else:
                    match_payload["content"] = format_numbered_lines(lines, context_start, context_end)
                match_payload["content_omitted"] = False
                remaining_bytes -= content_bytes + 1

            if len(matches) >= max_results:
                truncated = True
                break
        if truncated:
            break

    result.update(
        {
            "files_scanned": files_scanned,
            "matched_file_count": len(matched_files),
            "match_count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }
    )
    if not any(match["content_omitted"] for match in matches):
        result.pop("instruction")
    return result
