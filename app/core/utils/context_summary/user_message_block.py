import base64
import re
from dataclasses import dataclass

USER_MESSAGE_BLOCK_PATTERN = re.compile(
    r"(?:\n\n)?<covered_user_message message_id=\"(?P<message_id>[1-9]\d*)\" encoding=\"base64-utf8\">\n"
    r"(?P<payload>[A-Za-z0-9+/]*={0,2})\n"
    r"</covered_user_message>\s*\Z"
)


@dataclass(frozen=True)
class CoveredUserMessage:
    message_id: int
    content: str


def split_covered_user_message(summary: str | None) -> tuple[str | None, CoveredUserMessage | None]:
    if not summary:
        return summary, None
    match = USER_MESSAGE_BLOCK_PATTERN.search(summary)
    if match is None:
        return summary, None

    try:
        decoded = base64.b64decode(match.group("payload"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return summary, None

    model_summary = summary[: match.start()].rstrip()
    return (
        model_summary or None,
        CoveredUserMessage(
            message_id=int(match.group("message_id")),
            content=decoded,
        ),
    )


def append_covered_user_message(
    model_summary: str | None,
    *,
    message_id: int | None,
    content: str | None,
) -> str | None:
    clean_summary, _ = split_covered_user_message(model_summary)
    if message_id is None or content is None:
        return clean_summary

    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    block = f'<covered_user_message message_id="{message_id}" encoding="base64-utf8">\n{payload}\n</covered_user_message>'
    if not clean_summary:
        return block
    return f"{clean_summary}\n\n{block}"
