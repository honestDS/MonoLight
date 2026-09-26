from dataclasses import dataclass
from pathlib import Path

from app.core.constants import ERR_FILE_NOT_FOUND, ERR_FILE_TOOL_NOT_REGULAR
from app.core.i18n import t

_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class TextSource:
    text: str
    has_bom: bool
    line_ending: str


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def convert_line_endings(text: str, line_ending: str) -> str:
    normalized = normalize_line_endings(text)
    return normalized if line_ending == "\n" else normalized.replace("\n", "\r\n")


def read_text_source(target: Path) -> TextSource:
    if not target.exists():
        raise FileNotFoundError(t(ERR_FILE_NOT_FOUND))
    if not target.is_file():
        raise ValueError(t(ERR_FILE_TOOL_NOT_REGULAR))
    raw = target.read_bytes()
    has_bom = raw.startswith(_UTF8_BOM)
    payload = raw[len(_UTF8_BOM) :] if has_bom else raw
    text = payload.decode("utf-8")
    return TextSource(text=text, has_bom=has_bom, line_ending=detect_line_ending(text))


def write_text(target: Path, text: str, *, has_bom: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not target.is_file():
        raise ValueError(t(ERR_FILE_TOOL_NOT_REGULAR))
    payload = text.encode("utf-8")
    target.write_bytes((_UTF8_BOM if has_bom else b"") + payload)


def format_numbered_lines(lines: list[str], start_line: int, end_line: int) -> str:
    if not lines or start_line > end_line:
        return ""
    return "\n".join(f"{line_number}: {lines[line_number - 1]}" for line_number in range(start_line, end_line + 1))


def line_number_at(text: str, position: int) -> int:
    return text.count("\n", 0, max(0, position)) + 1
