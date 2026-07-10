import json
from typing import Any


def build_assistant_files_content(text: Any, files: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "type": "assistant_files",
            "text": str(text or ""),
            "files": files,
        },
        ensure_ascii=False,
    )


def parse_assistant_files_content(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(content, str):
        return str(content or "").strip(), []
    try:
        parsed = json.loads(content)
    except Exception:
        return content.strip(), []
    if not isinstance(parsed, dict) or parsed.get("type") != "assistant_files":
        return content.strip(), []
    text = str(parsed.get("text") or "").strip()
    return text, []


def merge_assistant_files(*file_groups: Any) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for items in file_groups:
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("id") or item.get("path") or "").strip()
            if file_id and file_id in seen_ids:
                continue
            if file_id:
                seen_ids.add(file_id)
            files.append(item)
    return files
