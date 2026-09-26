from pathlib import Path

from app.core.constants import ERR_FILE_TOOL_READ_RANGE_INVALID
from app.core.i18n import t

from .text import format_numbered_lines, normalize_line_endings, read_text_source

DEFAULT_READ_LINES = 2000


def read_file(target: Path, *, start_line: int | None, end_line: int | None) -> dict:
    start = 1 if start_line is None else start_line
    requested_end = start + DEFAULT_READ_LINES - 1 if end_line is None else end_line
    if isinstance(start, bool) or not isinstance(start, int) or start < 1 or isinstance(requested_end, bool) or not isinstance(requested_end, int) or requested_end < start:
        return {"status": "failed", "operation": "read", "error": t(ERR_FILE_TOOL_READ_RANGE_INVALID)}

    source = read_text_source(target)
    lines = normalize_line_endings(source.text).splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        actual_end = 0
        content = ""
    elif start > total_lines:
        return {"status": "failed", "operation": "read", "error": t(ERR_FILE_TOOL_READ_RANGE_INVALID)}
    else:
        capped_end = min(requested_end, start + DEFAULT_READ_LINES - 1)
        actual_end = min(capped_end, total_lines)
        content = format_numbered_lines(lines, start, actual_end)

    truncated = actual_end < total_lines
    return {
        "status": "success",
        "operation": "read",
        "path": str(target),
        "start_line": start,
        "end_line": actual_end,
        "total_lines": total_lines,
        "truncated": truncated,
        "next_start_line": actual_end + 1 if truncated else None,
        "content": content,
    }
