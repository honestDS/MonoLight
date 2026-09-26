from pathlib import Path

from app.core.constants import (
    ERR_FILE_TOOL_EDIT_IDENTICAL,
    ERR_FILE_TOOL_EDIT_MULTIPLE_MATCHES,
    ERR_FILE_TOOL_EDIT_NOT_FOUND,
    ERR_FILE_TOOL_NEW_TEXT_REQUIRED,
    ERR_FILE_TOOL_OLD_TEXT_REQUIRED,
)
from app.core.i18n import t

from .text import convert_line_endings, format_numbered_lines, line_number_at, normalize_line_endings, read_text_source, write_text


def _literal_matches(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        index = text.find(needle, cursor)
        if index < 0:
            return positions
        positions.append(index)
        cursor = index + max(1, len(needle))


def _apply_literal_replacements(text: str, old_text: str, new_text: str, positions: list[int]) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    output_length = 0
    for position in positions:
        prefix = text[cursor:position]
        parts.append(prefix)
        output_length += len(prefix)
        start = output_length
        parts.append(new_text)
        output_length += len(new_text)
        spans.append((start, output_length))
        cursor = position + len(old_text)
    parts.append(text[cursor:])
    return "".join(parts), spans


def edit_file(
    target: Path,
    *,
    old_text: str | None,
    new_text: str | None,
    replace_all: bool,
) -> dict:
    if not isinstance(old_text, str) or not old_text:
        return {"status": "failed", "operation": "edit", "error": t(ERR_FILE_TOOL_OLD_TEXT_REQUIRED)}
    if not isinstance(new_text, str):
        return {"status": "failed", "operation": "edit", "error": t(ERR_FILE_TOOL_NEW_TEXT_REQUIRED)}
    if not isinstance(replace_all, bool):
        return {"status": "failed", "operation": "edit", "error": t(ERR_FILE_TOOL_EDIT_MULTIPLE_MATCHES, actual=0)}
    if old_text == new_text:
        return {"status": "failed", "operation": "edit", "error": t(ERR_FILE_TOOL_EDIT_IDENTICAL)}

    source = read_text_source(target)
    old = convert_line_endings(old_text, source.line_ending)
    new = convert_line_endings(new_text, source.line_ending)
    matches = _literal_matches(source.text, old)
    if not matches:
        return {
            "status": "failed",
            "operation": "edit",
            "error": t(ERR_FILE_TOOL_EDIT_NOT_FOUND),
            "reason": "old_text_not_found",
            "matched": 0,
        }
    if len(matches) > 1 and not replace_all:
        return {
            "status": "failed",
            "operation": "edit",
            "error": t(ERR_FILE_TOOL_EDIT_MULTIPLE_MATCHES, actual=len(matches)),
            "reason": "multiple_matches",
            "matched": len(matches),
        }

    selected = matches if replace_all else matches[:1]
    replaced_text, spans = _apply_literal_replacements(source.text, old, new, selected)
    write_text(target, replaced_text, has_bom=source.has_bom)

    normalized_result = normalize_line_endings(replaced_text)
    lines = normalized_result.splitlines()
    targets: list[dict] = []
    for start_position, end_position in spans:
        start_line = line_number_at(replaced_text, start_position)
        end_line = line_number_at(replaced_text, max(start_position, end_position - 1))
        targets.append(
            {
                "start_line": start_line,
                "end_line": end_line,
                "content": format_numbered_lines(lines, start_line, min(end_line, len(lines))),
            }
        )
    return {
        "status": "success",
        "operation": "edit",
        "path": str(target),
        "matched": len(matches),
        "replaced": len(selected),
        "targets": targets,
    }
