from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.core.constants import (
    ERR_FILE_TOOL_PATCH_HUNK_AMBIGUOUS,
    ERR_FILE_TOOL_PATCH_HUNK_NOT_FOUND,
    ERR_FILE_TOOL_PATCH_INVALID,
    ERR_FILE_TOOL_PATCH_REQUIRED,
)
from app.core.i18n import t

from .text import convert_line_endings, format_numbered_lines, normalize_line_endings, read_text_source, write_text


@dataclass(frozen=True)
class PatchLine:
    kind: str
    text: str


@dataclass(frozen=True)
class PatchHunk:
    lines: tuple[PatchLine, ...]


class PatchFormatReason(StrEnum):
    HUNK_HEADER_REQUIRED = "hunk_header_required"
    LINE_PREFIX_INVALID = "line_prefix_invalid"
    HUNK_REQUIRED = "hunk_required"
    EMPTY_HUNK = "empty_hunk"
    HUNK_NO_CHANGES = "hunk_no_changes"
    HUNK_CONTEXT_REQUIRED = "hunk_context_required"


class PatchFormatError(ValueError):
    def __init__(self, reason: PatchFormatReason, line_number: int | None = None):
        super().__init__()
        self.reason = reason
        self.line_number = line_number


def parse_patch(patch: str) -> list[PatchHunk]:
    normalized = normalize_line_endings(patch)
    raw_lines = normalized.split("\n")
    hunks: list[PatchHunk] = []
    current: list[PatchLine] | None = None

    for line_number, raw_line in enumerate(raw_lines, start=1):
        if line_number == len(raw_lines) and raw_line == "":
            continue
        if raw_line.startswith("@@"):
            if current is not None:
                _append_hunk(hunks, current)
            current = []
            continue
        if current is None:
            if raw_line.strip():
                raise PatchFormatError(PatchFormatReason.HUNK_HEADER_REQUIRED, line_number)
            continue
        if not raw_line:
            current.append(PatchLine(" ", ""))
            continue
        prefix = raw_line[0]
        if prefix not in {" ", "-", "+"}:
            raise PatchFormatError(PatchFormatReason.LINE_PREFIX_INVALID, line_number)
        current.append(PatchLine(prefix, raw_line[1:]))

    if current is not None:
        _append_hunk(hunks, current)
    if not hunks:
        raise PatchFormatError(PatchFormatReason.HUNK_REQUIRED)
    return hunks


def _append_hunk(hunks: list[PatchHunk], lines: list[PatchLine]) -> None:
    if not lines:
        raise PatchFormatError(PatchFormatReason.EMPTY_HUNK)
    if not any(line.kind in {"-", "+"} for line in lines):
        raise PatchFormatError(PatchFormatReason.HUNK_NO_CHANGES)
    if not any(line.kind in {" ", "-"} for line in lines):
        raise PatchFormatError(PatchFormatReason.HUNK_CONTEXT_REQUIRED)
    hunks.append(PatchHunk(tuple(lines)))


def _old_lines(hunk: PatchHunk) -> list[str]:
    return [line.text for line in hunk.lines if line.kind in {" ", "-"}]


def _identity(value: str) -> str:
    return value


def _candidate_positions(source_lines: list[str], expected: list[str], mode: str) -> list[int]:
    if not expected or len(expected) > len(source_lines):
        return []
    if mode == "exact":
        normalize = _identity
    elif mode == "rstrip":
        normalize = str.rstrip
    else:
        normalize = str.strip

    normalized_expected = [normalize(line) for line in expected]
    matches: list[int] = []
    for index in range(0, len(source_lines) - len(expected) + 1):
        if [normalize(line) for line in source_lines[index : index + len(expected)]] == normalized_expected:
            matches.append(index)
    return matches


def _locate_hunk(source_lines: list[str], hunk: PatchHunk) -> tuple[int | None, bool, str | None]:
    expected = _old_lines(hunk)
    for mode in ("exact", "rstrip", "strip"):
        matches = _candidate_positions(source_lines, expected, mode)
        if len(matches) == 1:
            return matches[0], False, mode
        if len(matches) > 1:
            return None, True, mode
    return None, False, None


def _apply_hunk(source_lines: list[str], hunk: PatchHunk, start: int) -> tuple[list[str], int]:
    old_count = len(_old_lines(hunk))
    actual_old = source_lines[start : start + old_count]
    replacement: list[str] = []
    old_cursor = 0
    for line in hunk.lines:
        if line.kind == " ":
            replacement.append(actual_old[old_cursor])
            old_cursor += 1
        elif line.kind == "-":
            old_cursor += 1
        else:
            replacement.append(line.text)
    return source_lines[:start] + replacement + source_lines[start + old_count :], len(replacement)


def patch_file(target: Path, *, patch: str | None) -> dict:
    if not isinstance(patch, str) or not patch.strip():
        return {"status": "failed", "operation": "patch", "error": t(ERR_FILE_TOOL_PATCH_REQUIRED)}
    try:
        hunks = parse_patch(patch)
    except PatchFormatError as exc:
        result = {
            "status": "failed",
            "operation": "patch",
            "error": t(ERR_FILE_TOOL_PATCH_INVALID),
            "reason": exc.reason.value,
        }
        if exc.line_number is not None:
            result["line_number"] = exc.line_number
        return result

    source = read_text_source(target)
    normalized_source = normalize_line_endings(source.text)
    candidate_lines = normalized_source.split("\n")
    targets: list[dict] = []

    for index, hunk in enumerate(hunks, start=1):
        start, ambiguous, strategy = _locate_hunk(candidate_lines, hunk)
        if ambiguous:
            return {
                "status": "failed",
                "operation": "patch",
                "error": t(ERR_FILE_TOOL_PATCH_HUNK_AMBIGUOUS, index=index),
                "reason": "hunk_ambiguous",
                "hunk_index": index,
            }
        if start is None:
            return {
                "status": "failed",
                "operation": "patch",
                "error": t(ERR_FILE_TOOL_PATCH_HUNK_NOT_FOUND, index=index),
                "reason": "hunk_not_found",
                "hunk_index": index,
            }

        candidate_lines, replacement_count = _apply_hunk(candidate_lines, hunk, start)
        display_lines = candidate_lines[:-1] if candidate_lines and candidate_lines[-1] == "" else candidate_lines
        start_line = start + 1
        end_line = max(start_line, start + replacement_count)
        targets.append(
            {
                "hunk_index": index,
                "start_line": start_line,
                "end_line": end_line,
                "match_strategy": strategy,
                "content": format_numbered_lines(display_lines, start_line, min(end_line, len(display_lines))),
            }
        )

    normalized_result = "\n".join(candidate_lines)
    result = convert_line_endings(normalized_result, source.line_ending)
    write_text(target, result, has_bom=source.has_bom)
    return {
        "status": "success",
        "operation": "patch",
        "path": str(target),
        "hunks_applied": len(hunks),
        "targets": targets,
    }
