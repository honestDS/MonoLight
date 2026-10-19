import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from app.core.constants import (
    ERR_SKILL_ID_INVALID,
    ERR_SKILL_NOT_DIRECTORY,
    ERR_SKILL_NOT_FOUND,
    ERR_SKILL_PAGE_INVALID,
    ERR_SKILL_PATH_INVALID,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_METADATA_MAX_BYTES,
    SKILL_NAME_MAX_CHARS,
    SKILL_PAGE_DEFAULT_LIMIT,
    SKILL_PAGE_MAX_LIMIT,
)
from app.core.i18n import t


def _raise_error(error_key: str) -> None:
    raise ValueError(t(error_key))


def _valid_skill_id(skill_id: object) -> bool:
    return isinstance(skill_id, str) and bool(skill_id.strip()) and not skill_id.startswith(".") and not any(char in skill_id for char in ("/", "\\", ":", "\0")) and not PurePosixPath(skill_id).is_absolute() and not PureWindowsPath(skill_id).is_absolute() and not PureWindowsPath(skill_id).drive


def _validated_path_parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.strip() or "\0" in path or ":" in path or PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute() or PureWindowsPath(path).root or PureWindowsPath(path).drive:
        _raise_error(ERR_SKILL_PATH_INVALID)

    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        _raise_error(ERR_SKILL_PATH_INVALID)
    return [part for part in parts if part not in ("", ".")]


def _validate_page(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SKILL_PAGE_MAX_LIMIT:
        _raise_error(ERR_SKILL_PAGE_INVALID)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_root(skills_root: Path) -> Path | None:
    try:
        return skills_root.resolve(strict=True)
    except FileNotFoundError:
        return None


def _resolve_package(root: Path, skill_id: str) -> Path:
    try:
        package = (root / skill_id).resolve(strict=True)
    except FileNotFoundError:
        _raise_error(ERR_SKILL_NOT_FOUND)
    except RuntimeError:
        _raise_error(ERR_SKILL_PATH_INVALID)

    if package == root or not _within(package, root):
        _raise_error(ERR_SKILL_PATH_INVALID)
    if not package.is_dir():
        _raise_error(ERR_SKILL_NOT_DIRECTORY)
    return package


def _resolve_target(package: Path, parts: list[str]) -> Path:
    try:
        target = package.joinpath(*parts).resolve(strict=True)
    except FileNotFoundError:
        _raise_error(ERR_SKILL_NOT_FOUND)
    except RuntimeError:
        _raise_error(ERR_SKILL_PATH_INVALID)

    if not _within(target, package):
        _raise_error(ERR_SKILL_PATH_INVALID)
    return target


def _recover_metadata_name(header: bytes) -> str | None:
    candidate = None
    for line in header.splitlines():
        if line.startswith(b"name:"):
            if candidate is not None:
                return None
            candidate = line
    if candidate is None:
        return None

    try:
        parsed = yaml.safe_load(candidate.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        return None
    name = parsed.get("name") if isinstance(parsed, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _metadata(package: Path, skill_id: str) -> dict:
    fallback = {
        "id": skill_id,
        "name": skill_id,
        "description": "",
        "path": str(package),
        "document_path": None,
        "metadata_status": "missing",
    }
    document = package / "SKILL.md"
    try:
        document = document.resolve(strict=True)
    except FileNotFoundError:
        return fallback
    except (OSError, RuntimeError):
        fallback["metadata_status"] = "invalid"
        return fallback

    if not _within(document, package):
        fallback["metadata_status"] = "invalid"
        return fallback

    try:
        if not stat.S_ISREG(document.stat().st_mode):
            fallback["metadata_status"] = "invalid"
            return fallback
        with document.open("rb") as file:
            raw = file.read(SKILL_METADATA_MAX_BYTES)
    except FileNotFoundError:
        return fallback
    except OSError:
        fallback["metadata_status"] = "invalid"
        return fallback

    fallback["document_path"] = str(document)
    lines = raw.splitlines(keepends=True)
    if not lines:
        return fallback

    first_line = lines[0].rstrip(b"\r\n")
    if first_line.startswith(b"\xef\xbb\xbf"):
        first_line = first_line[3:]
    if first_line != b"---":
        return fallback

    closing_line = next((index for index, line in enumerate(lines[1:], start=1) if line.rstrip(b"\r\n") in (b"---", b"...")), None)
    header_bytes = b"".join(lines[1:closing_line]) if closing_line is not None else b"".join(lines[1:])
    if closing_line is None:
        recovered_name = _recover_metadata_name(header_bytes)
        if recovered_name:
            fallback["name"] = recovered_name[:SKILL_NAME_MAX_CHARS]
        fallback["metadata_status"] = "invalid"
        return fallback

    try:
        header = header_bytes.decode("utf-8-sig")
        metadata = yaml.safe_load(header)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        recovered_name = _recover_metadata_name(header_bytes)
        if recovered_name:
            fallback["name"] = recovered_name[:SKILL_NAME_MAX_CHARS]
        fallback["metadata_status"] = "invalid"
        return fallback

    name = metadata.get("name") if isinstance(metadata, dict) else None
    description = metadata.get("description") if isinstance(metadata, dict) else None
    if isinstance(name, str) and name.strip():
        fallback["name"] = name.strip()[:SKILL_NAME_MAX_CHARS]
        fallback["metadata_status"] = "valid"
    else:
        fallback["metadata_status"] = "invalid"
    if isinstance(description, str):
        fallback["description"] = description.strip()[:SKILL_DESCRIPTION_MAX_CHARS]
    return fallback


def list_skills(
    skills_root: Path,
    *,
    offset: int = 0,
    limit: int = SKILL_PAGE_DEFAULT_LIMIT,
) -> dict:
    _validate_page(offset, limit)
    root = _resolve_root(skills_root)
    if root is None:
        return {
            "status": "success",
            "operation": "list",
            "root": str(skills_root.resolve()),
            "skills": [],
            "offset": offset,
            "total": 0,
            "truncated": False,
            "next_offset": None,
        }

    packages = []
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.startswith(".") or not _valid_skill_id(entry.name):
                continue
            try:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                package = Path(entry.path).resolve(strict=True)
                if package == root or not _within(package, root) or not package.is_dir():
                    continue
            except (OSError, RuntimeError):
                continue
            packages.append((entry.name, package))

    packages.sort(key=lambda item: item[0])
    total = len(packages)
    page = packages[offset : offset + limit]
    truncated = offset + len(page) < total
    return {
        "status": "success",
        "operation": "list",
        "root": str(root),
        "skills": [_metadata(package, skill_id) for skill_id, package in page],
        "offset": offset,
        "total": total,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
    }


def browse_skill(
    skills_root: Path,
    skill_id: str,
    *,
    path: str = ".",
    offset: int = 0,
    limit: int = SKILL_PAGE_DEFAULT_LIMIT,
) -> dict:
    if not _valid_skill_id(skill_id):
        _raise_error(ERR_SKILL_ID_INVALID)
    parts = _validated_path_parts(path)
    _validate_page(offset, limit)

    root = _resolve_root(skills_root)
    if root is None:
        _raise_error(ERR_SKILL_NOT_FOUND)
    package = _resolve_package(root, skill_id)
    target = _resolve_target(package, parts)
    if not target.is_dir():
        _raise_error(ERR_SKILL_NOT_DIRECTORY)

    def sorted_children(directory: Path) -> list[tuple[str, str]]:
        with os.scandir(directory) as children:
            return sorted(((child.name, child.path) for child in children), key=lambda item: item[0])

    max_items = offset + limit + 1
    entries = []
    stack = [(sorted_children(target), 0, parts)]
    while stack and len(entries) < max_items:
        children, index, relative_parts = stack[-1]
        if index >= len(children):
            stack.pop()
            continue
        name, child_entry_path = children[index]
        stack[-1] = (children, index + 1, relative_parts)

        try:
            child_entry = Path(child_entry_path)
            child_path = child_entry.resolve(strict=True)
            if not _within(child_path, package):
                continue
            mode = child_path.stat().st_mode
            link_info = child_entry.lstat()
            is_link = stat.S_ISLNK(link_info.st_mode) or bool(getattr(link_info, "st_reparse_tag", 0))
            if stat.S_ISDIR(mode):
                entry_type = "directory"
            elif stat.S_ISREG(mode):
                entry_type = "file"
            else:
                continue
        except (OSError, RuntimeError):
            continue

        child_parts = [*relative_parts, name]
        entries.append(
            {
                "name": name,
                "relative_path": PurePosixPath(*child_parts).as_posix(),
                "path": str(child_path),
                "type": entry_type,
            }
        )
        if entry_type == "directory" and not is_link and len(entries) < max_items:
            try:
                stack.append((sorted_children(child_path), 0, child_parts))
            except OSError:
                pass

    page = entries[offset : offset + limit]
    truncated = len(entries) > offset + limit
    return {
        "status": "success",
        "operation": "browse",
        "skill_id": skill_id,
        "path": str(target),
        "entries": page,
        "offset": offset,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
    }
