from pathlib import Path

from app.core.constants import ERR_FILE_TOOL_CONTENT_REQUIRED
from app.core.i18n import t

from .text import write_text


def write_file(target: Path, *, content: str | None) -> dict:
    if not isinstance(content, str):
        return {"status": "failed", "operation": "write", "error": t(ERR_FILE_TOOL_CONTENT_REQUIRED)}
    write_text(target, content)
    return {
        "status": "success",
        "operation": "write",
        "path": str(target),
        "bytes_written": len(content.encode("utf-8")),
    }
