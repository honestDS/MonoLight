#!/usr/bin/env python3
"""Monolight built-in cross-platform SkillHub searcher, inspector, and installer.

Uses only the Python 3 standard library. Does not invoke the external skillhub CLI,
does not depend on external web_tool, and contains no hardcoded local absolute paths.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

API_BASE = "https://api.skillhub.cn"
WEB_BASE = "https://www.skillhub.cn"

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024  # 150 MB
MAX_ENTRIES = 2000
HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "monolight-skillhub-manual-installer/1.1 (Python-urllib)"

SCRIPT_EXTENSIONS = {
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".bat",
    ".cmd",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
}


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def is_within_directory(base: Path, target: Path) -> bool:
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(base_resolved)
        return True
    except ValueError:
        return False


def sanitize_slug(raw_name: str) -> str:
    cleaned = raw_name.strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", cleaned)
    cleaned = cleaned.strip(".-_")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"Invalid skill directory slug derived from: {raw_name!r}")
    return cleaned


def http_get_bytes(url: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Download exceeded safety limit of {max_bytes} bytes.")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(512).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        detail = f" ({body})" if body else ""
        raise RuntimeError(f"HTTP {exc.code} fetching {url}: {exc.reason}{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error fetching {url}: {exc.reason}") from exc


def http_get_json(url: str) -> dict[str, Any]:
    raw = http_get_bytes(url, max_bytes=10 * 1024 * 1024)
    return json.loads(raw.decode("utf-8"))


def infer_monolight_skills_dir() -> tuple[Path | None, str | None]:
    """Locate Monolight's data/skills root dynamically without hardcoded paths."""
    script_path = Path(__file__).resolve()
    # Expected layout inside Monolight: <MONOLIGHT_ROOT>/data/skills/<this-skill>/scripts/install_skill.py
    if len(script_path.parents) >= 3:
        candidate_root = script_path.parents[2]
        if candidate_root.name == "skills" and candidate_root.is_dir():
            if candidate_root.parent.name == "data":
                return candidate_root, "self:../../ (data/skills)"
            return candidate_root, "self:../.. (skills root)"

    # Fallback: walk up from current working directory looking for data/skills
    cwd = Path.cwd().resolve()
    for ancestor in (cwd, *cwd.parents):
        candidate = ancestor / "data" / "skills"
        if candidate.is_dir():
            try:
                rel = os.path.relpath(candidate, cwd).replace("\\", "/")
            except ValueError:
                rel = "data/skills"
            return candidate, f"cwd-ancestor:{rel}"

    return None, None


def resolve_skills_dir(explicit_dir: str | None) -> tuple[Path, str]:
    if explicit_dir:
        return Path(explicit_dir).expanduser().resolve(), "arg:--skills-dir"

    for env_key in ("MONOLIGHT_SKILLS_DIR", "AGENT_SKILLS_DIR", "SKILLS_DIR"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(val).expanduser().resolve(), f"env:{env_key}"

    inferred, reason = infer_monolight_skills_dir()
    if inferred is not None and reason is not None:
        return inferred, reason

    raise ValueError("Could not auto-locate Monolight's data/skills directory. Pass --skills-dir or set MONOLIGHT_SKILLS_DIR.")


def detect_candidate_dirs() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(path: Path, label: str) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "label": label,
                "path": key,
                "exists": path.is_dir(),
            }
        )

    inferred, reason = infer_monolight_skills_dir()
    if inferred is not None:
        add_candidate(inferred, f"monolight ({reason})")

    cwd = Path.cwd().resolve()
    for rel in ("data/skills", "skills", ".agents/skills"):
        add_candidate(cwd / rel, f"cwd:{rel}")

    home = Path.home()
    for rel in (
        ".agents/skills",
        ".claude/skills",
        ".codex/skills",
        ".cursor/skills",
        ".openclaw/skills",
    ):
        add_candidate(home / rel, f"home:{rel}")

    return candidates


def parse_skill_frontmatter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter '---'.")

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError("SKILL.md YAML frontmatter closing '---' not found.")

    fm_lines = lines[1:end_idx]
    metadata: dict[str, Any] = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not match:
            i += 1
            continue

        key = match.group(1)
        val_part = match.group(2).strip()

        if val_part in {"|", "|-", ">", ">-"}:
            block_lines: list[str] = []
            i += 1
            while i < len(fm_lines):
                next_line = fm_lines[i]
                if next_line.startswith((" ", "\t")) or not next_line.strip():
                    block_lines.append(next_line.strip())
                    i += 1
                else:
                    break
            metadata[key] = " ".join(x for x in block_lines if x).strip()
            continue

        if len(val_part) >= 2 and ((val_part.startswith('"') and val_part.endswith('"')) or (val_part.startswith("'") and val_part.endswith("'"))):
            if val_part.startswith('"'):
                try:
                    val_part = json.loads(val_part)
                except json.JSONDecodeError:
                    val_part = val_part[1:-1]
            else:
                val_part = val_part[1:-1].replace("''", "'")
        metadata[key] = val_part
        i += 1

    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter missing required non-empty 'name'.")
    if not description:
        raise ValueError("SKILL.md frontmatter missing required non-empty 'description'.")
    return metadata


def find_skill_document(root: Path) -> Path | None:
    for candidate in ("SKILL.md", "skill.md"):
        p = root / candidate
        if p.is_file():
            return p
    return None


def validate_zip_member_path(name: str) -> PurePosixPath:
    if "\x00" in name:
        raise ValueError(f"Unsafe zip entry with NUL byte: {name!r}")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"Unsafe zip entry with absolute path: {name!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"Unsafe zip entry with drive letter: {name!r}")
    posix = PurePosixPath(normalized)
    if ".." in posix.parts:
        raise ValueError(f"Unsafe zip entry with parent traversal '..': {name!r}")
    return posix


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> tuple[Path, list[str], str | None]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    total_uncompressed = 0
    entry_count = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError(f"Zip contains {len(infos)} entries, exceeding safety limit of {MAX_ENTRIES}.")

        for info in infos:
            entry_count += 1
            mode = info.external_attr >> 16
            # Reject Unix symlinks (S_IFLNK = 0o120000)
            if (mode & 0o170000) == 0o120000:
                raise ValueError(f"Unsafe symlink entry in zip: {info.filename!r}")

            if info.file_size < 0:
                raise ValueError(f"Invalid negative file size in zip: {info.filename!r}")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(f"Uncompressed size exceeds safety limit of {MAX_UNCOMPRESSED_BYTES} bytes.")

            rel_posix = validate_zip_member_path(info.filename)
            if not rel_posix.parts:
                continue

            target_path = dest_dir.joinpath(*rel_posix.parts)
            if not is_within_directory(dest_dir, target_path):
                raise ValueError(f"Zip-Slip path escape detected: {info.filename!r}")

            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)

    strip_prefix: str | None = None
    content_root = dest_dir
    if find_skill_document(dest_dir) is None:
        children = [p for p in dest_dir.iterdir() if not p.name.startswith("__MACOSX")]
        if len(children) == 1 and children[0].is_dir() and find_skill_document(children[0]) is not None:
            strip_prefix = children[0].name
            content_root = children[0]

    doc_path = find_skill_document(content_root)
    if doc_path is None:
        raise ValueError("Package does not contain SKILL.md (or skill.md) at root or single wrapper folder.")

    files: list[str] = []
    for p in sorted(content_root.rglob("*")):
        if p.is_file():
            files.append(p.relative_to(content_root).as_posix())

    return content_root, files, strip_prefix


def parse_skill_package_metadata(content_root: Path) -> tuple[str, dict[str, Any], list[str]]:
    doc_path = find_skill_document(content_root)
    if doc_path is None:
        raise ValueError("SKILL.md not found in extracted package.")
    text = doc_path.read_text(encoding="utf-8")
    metadata = parse_skill_frontmatter(text)
    files = [p.relative_to(content_root).as_posix() for p in sorted(content_root.rglob("*")) if p.is_file()]
    scripts = [f for f in files if Path(f).suffix.lower() in SCRIPT_EXTENSIONS]
    return doc_path.name, metadata, scripts


def unpack_text_field(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, str):
        trimmed = val.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, dict):
                    for k in ("answer", "description", "summary", "text", "content"):
                        if parsed.get(k) and isinstance(parsed[k], str):
                            return parsed[k].strip()
            except Exception:
                pass
        return trimmed
    return str(val).strip()


def normalize_skill_search_item(raw: dict[str, Any], *, max_desc_chars: int = 160) -> dict[str, Any]:
    ns_obj = raw.get("namespace") if isinstance(raw.get("namespace"), dict) else {}
    ns_handle = ns_obj.get("handle") or ns_obj.get("displayName") or raw.get("ownerName") or ""
    slug = raw.get("slug") or ns_obj.get("publicSlug") or ""
    canonical = ns_obj.get("canonicalName") or (f"@{ns_handle}/{slug}" if ns_handle and slug else slug)
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    req_key_raw = labels.get("requires_api_key")
    requires_api_key: bool | None
    if isinstance(req_key_raw, bool):
        requires_api_key = req_key_raw
    elif isinstance(req_key_raw, str):
        requires_api_key = req_key_raw.strip().lower() == "true"
    else:
        requires_api_key = None

    pricing_type = labels.get("pricing_type") or None
    channel_pricing = raw.get("channelPricing") if isinstance(raw.get("channelPricing"), dict) else None
    price_info = None
    if channel_pricing:
        p = channel_pricing.get("price")
        curr = channel_pricing.get("currency") or "CNY"
        if p is not None:
            price_info = f"{p} {curr}"
    elif pricing_type and pricing_type not in ("free", "none"):
        price_info = str(pricing_type)

    display_name = raw.get("displayName") or raw.get("name") or slug
    raw_desc = (
        unpack_text_field(raw.get("description_zh"))
        or unpack_text_field(raw.get("description"))
        or unpack_text_field(raw.get("summary_zh"))
        or unpack_text_field(raw.get("summary"))
        or ""
    )
    # Collapse multiple whitespace
    raw_desc = re.sub(r"\s+", " ", raw_desc).strip()
    if max_desc_chars > 0 and len(raw_desc) > max_desc_chars:
        desc = raw_desc[:max_desc_chars].rstrip() + "..."
    else:
        desc = raw_desc

    item: dict[str, Any] = {
        "name": display_name,
        "slug": slug,
        "canonical_name": canonical,
        "version": raw.get("version"),
        "category": raw.get("category"),
        "source": raw.get("source"),
        "downloads": raw.get("downloads"),
        "stars": raw.get("stars"),
        "score": raw.get("score"),
        "verified": bool(raw.get("verified")),
        "requires_api_key": requires_api_key,
        "description": desc,
    }
    if price_info:
        item["pricing"] = price_info
    return item


def search_skillhub(
    keyword: str,
    *,
    page: int = 1,
    limit: int = 10,
    category: str | None = None,
    sort_by: str | None = None,
    order: str = "desc",
    source: str | None = None,
    max_desc_chars: int = 160,
) -> dict[str, Any]:
    params: dict[str, str] = {
        "page": str(page),
        "pageSize": str(limit),
    }
    if keyword.strip():
        params["keyword"] = keyword.strip()
    if category:
        params["category"] = category
    if sort_by:
        params["sortBy"] = sort_by
    if order:
        params["order"] = order
    if source and source != "all":
        params["source"] = source

    url = f"{API_BASE}/api/skills?{urllib.parse.urlencode(params)}"
    resp = http_get_json(url)
    if resp.get("code") not in (0, None):
        raise RuntimeError(f"SkillHub search API error: {resp.get('message') or resp}")
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    raw_skills = data.get("skills") or []
    items = [normalize_skill_search_item(it, max_desc_chars=max_desc_chars) for it in raw_skills if isinstance(it, dict)]
    return {
        "total": data.get("total", len(items)),
        "items": items,
    }


def fetch_skill_security_report(
    slug: str | None,
    namespace: str | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "checked": False,
        "has_suspicious_report": False,
        "version": None,
        "display_name": None,
        "requires_api_key": None,
        "engines": {},
        "warnings": [],
    }
    if not slug:
        return report

    qs = f"?{urllib.parse.urlencode({'namespace': namespace})}" if namespace else ""
    url = f"{API_BASE}/api/v1/skills/{urllib.parse.quote(slug)}{qs}"
    try:
        detail = http_get_json(url)
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        return report

    report["checked"] = True
    skill_obj = detail.get("skill") if isinstance(detail.get("skill"), dict) else {}
    latest_ver = detail.get("latestVersion") if isinstance(detail.get("latestVersion"), dict) else {}
    tags = skill_obj.get("tags") if isinstance(skill_obj.get("tags"), dict) else {}
    labels = skill_obj.get("labels") if isinstance(skill_obj.get("labels"), dict) else {}

    report["version"] = latest_ver.get("version") or tags.get("latest")
    report["display_name"] = skill_obj.get("displayName") or skill_obj.get("name")
    req_raw = labels.get("requires_api_key")
    if isinstance(req_raw, bool):
        report["requires_api_key"] = req_raw
    elif isinstance(req_raw, str):
        report["requires_api_key"] = req_raw.strip().lower() == "true"

    sec_reports = detail.get("securityReports") if isinstance(detail.get("securityReports"), dict) else {}
    for engine_name, engine_data in sec_reports.items():
        if not isinstance(engine_data, dict):
            continue
        status = str(engine_data.get("status") or "").strip().lower()
        status_text = str(engine_data.get("statusText") or "").strip()
        report_url = engine_data.get("reportUrl")
        report["engines"][engine_name] = {
            "status": status,
            "status_text": status_text,
            "report_url": report_url,
        }
        if status and status not in {"benign", "safe", "clean", "pass"}:
            report["has_suspicious_report"] = True
            report["warnings"].append(f"Security engine '{engine_name}' flagged status '{status}' ({status_text}).")

    return report


def parse_source(raw_source: str) -> dict[str, str | None]:
    raw = raw_source.strip()
    if not raw:
        raise ValueError("Source must not be empty.")

    local_candidate = Path(raw).expanduser()
    if local_candidate.is_file() and local_candidate.suffix.lower() == ".zip":
        return {
            "kind": "local_zip",
            "namespace": None,
            "slug": sanitize_slug(local_candidate.stem),
            "page_url": None,
            "download_url": None,
            "local_path": str(local_candidate.resolve()),
        }

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        if "/api/v1/download" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            slug = (qs.get("slug") or [None])[0]
            ns = (qs.get("namespace") or [None])[0]
            return {
                "kind": "download_url",
                "namespace": ns,
                "slug": slug,
                "page_url": f"{WEB_BASE}/skills/{ns}/{slug}" if ns and slug else None,
                "download_url": raw,
                "local_path": None,
            }

        m_ns = re.match(r"^/skills/([^/]+)/([^/?#]+)", parsed.path)
        if m_ns:
            ns, slug = urllib.parse.unquote(m_ns.group(1)), urllib.parse.unquote(m_ns.group(2))
            dl_qs = urllib.parse.urlencode({"slug": slug, "namespace": ns})
            return {
                "kind": "skillhub_url",
                "namespace": ns,
                "slug": slug,
                "page_url": f"{WEB_BASE}/skills/{ns}/{slug}",
                "download_url": f"{API_BASE}/api/v1/download?{dl_qs}",
                "local_path": None,
            }

        m_slug = re.match(r"^/skills/([^/?#]+)", parsed.path)
        if m_slug:
            slug = urllib.parse.unquote(m_slug.group(1))
            return resolve_bare_slug(slug)

        raise ValueError(f"Unsupported URL format: {raw}")

    m_at = re.match(r"^@?([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$", raw)
    if m_at:
        ns, slug = m_at.group(1), m_at.group(2)
        dl_qs = urllib.parse.urlencode({"slug": slug, "namespace": ns})
        return {
            "kind": "skillhub_slug",
            "namespace": ns,
            "slug": slug,
            "page_url": f"{WEB_BASE}/skills/{ns}/{slug}",
            "download_url": f"{API_BASE}/api/v1/download?{dl_qs}",
            "local_path": None,
        }

    if re.match(r"^[A-Za-z0-9._-]+$", raw):
        return resolve_bare_slug(raw)

    raise ValueError(f"Unrecognized source {raw!r}. Provide @namespace/slug, slug, SkillHub URL, Zip URL, or local .zip path.")


def resolve_bare_slug(slug: str) -> dict[str, str | None]:
    res = search_skillhub(slug, page=1, limit=20)
    exact = [it for it in res["items"] if str(it.get("slug") or "").lower() == slug.lower()]
    if len(exact) == 1:
        hit = exact[0]
        ns = hit.get("namespace") or None
        real_slug = hit.get("slug") or slug
        params = {"slug": real_slug}
        if ns:
            params["namespace"] = ns
        return {
            "kind": "skillhub_slug",
            "namespace": ns,
            "slug": real_slug,
            "page_url": hit.get("page_url"),
            "download_url": f"{API_BASE}/api/v1/download?{urllib.parse.urlencode(params)}",
            "local_path": None,
        }
    if len(exact) > 1:
        candidates = ", ".join(it.get("canonical_name") or str(it.get("slug")) for it in exact[:10])
        raise ValueError(f"Multiple skills match slug {slug!r}: {candidates}. Please specify @namespace/slug.")

    dl_qs = urllib.parse.urlencode({"slug": slug})
    return {
        "kind": "skillhub_slug",
        "namespace": None,
        "slug": slug,
        "page_url": None,
        "download_url": f"{API_BASE}/api/v1/download?{dl_qs}",
        "local_path": None,
    }


def resolve_zip_for_source(
    source_info: dict[str, str | None],
    temp_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    if source_info["local_path"]:
        return Path(source_info["local_path"]), {
            "checked": False,
            "has_suspicious_report": False,
            "version": None,
            "display_name": None,
            "requires_api_key": None,
            "engines": {},
            "warnings": [],
            "note": "Local zip archive; remote SkillHub security report check skipped.",
        }

    security_report = fetch_skill_security_report(
        slug=source_info.get("slug"),
        namespace=source_info.get("namespace"),
    )

    download_url = source_info["download_url"]
    if not download_url:
        raise ValueError("No download URL available for source.")

    zip_bytes = http_get_bytes(download_url)
    zip_path = temp_dir / "package.zip"
    zip_path.write_bytes(zip_bytes)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Downloaded content from {download_url} is not a valid zip file.")

    return zip_path, security_report


def cmd_categories() -> int:
    url = f"{API_BASE}/api/v1/categories"
    data = http_get_json(url)
    items = data.get("items") or []
    print(
        json.dumps(
            {
                "status": "success",
                "command": "categories",
                "count": data.get("count", len(items)),
                "items": items,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    max_desc = -1 if args.full_desc else args.max_desc_chars
    res = search_skillhub(
        args.keyword,
        page=args.page,
        limit=args.limit,
        category=args.category,
        sort_by=args.sort_by,
        order=args.order,
        source=args.source,
        max_desc_chars=max_desc,
    )
    print(
        json.dumps(
            {
                "status": "success",
                "command": "search",
                "query": args.keyword,
                "page": args.page,
                "limit": args.limit,
                "total": res["total"],
                "items": res["items"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_detect_dirs() -> int:
    inferred, reason = infer_monolight_skills_dir()
    print(
        json.dumps(
            {
                "status": "success",
                "command": "detect-dirs",
                "resolved_skills_dir": str(inferred) if inferred else None,
                "resolved_by": reason,
                "candidates": detect_candidate_dirs(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_list_installed(args: argparse.Namespace) -> int:
    skills_dir, resolved_by = resolve_skills_dir(args.skills_dir)
    installed: list[dict[str, Any]] = []
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            doc = find_skill_document(entry)
            meta: dict[str, Any] = {}
            valid = False
            error = None
            if doc is not None:
                try:
                    meta = parse_skill_frontmatter(doc.read_text(encoding="utf-8"))
                    valid = True
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
            else:
                error = "SKILL.md not found"
            installed.append(
                {
                    "id": entry.name,
                    "name": meta.get("name") or entry.name,
                    "description": meta.get("description") or "",
                    "path": str(entry),
                    "document_path": str(doc) if doc else None,
                    "metadata_valid": valid,
                    "error": error,
                }
            )

    print(
        json.dumps(
            {
                "status": "success",
                "command": "list-installed",
                "skills_dir": str(skills_dir),
                "resolved_by": resolved_by,
                "count": len(installed),
                "skills": installed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    source_info = parse_source(args.source)
    with tempfile.TemporaryDirectory(prefix="skillhub-inspect-") as tmp:
        tmp_path = Path(tmp)
        zip_path, security_report = resolve_zip_for_source(source_info, tmp_path)
        extract_dir = tmp_path / "extracted"
        content_root, files, strip_prefix = safe_extract_zip(zip_path, extract_dir)
        doc_name, metadata, scripts = parse_skill_package_metadata(content_root)

        suggested_slug = sanitize_slug(source_info.get("slug") or str(metadata["name"]))
        total_bytes = sum((content_root / f).stat().st_size for f in files if (content_root / f).is_file())

        print(
            json.dumps(
                {
                    "status": "success",
                    "command": "inspect",
                    "source": source_info,
                    "suggested_slug": suggested_slug,
                    "security_report": security_report,
                    "package": {
                        "total_files": len(files),
                        "total_uncompressed_bytes": total_bytes,
                        "strip_prefix": strip_prefix,
                        "document_file": doc_name,
                        "metadata": metadata,
                        "files": files,
                        "scripts": scripts,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    source_info = parse_source(args.source)
    skills_dir, resolved_by = resolve_skills_dir(args.skills_dir)
    skills_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="skillhub-install-") as tmp:
        tmp_path = Path(tmp)
        zip_path, security_report = resolve_zip_for_source(source_info, tmp_path)
        extract_dir = tmp_path / "extracted"
        content_root, files, _strip_prefix = safe_extract_zip(zip_path, extract_dir)
        doc_name, metadata, scripts = parse_skill_package_metadata(content_root)

        raw_slug = args.slug or source_info.get("slug") or str(metadata["name"])
        slug = sanitize_slug(raw_slug)
        target_dir = skills_dir / slug
        if not is_within_directory(skills_dir, target_dir):
            raise ValueError(f"Unsafe target directory: {target_dir}")

        if target_dir.exists():
            if not args.force:
                raise FileExistsError(f"Skill directory already exists: {target_dir}. Re-run with --force to replace.")

        staging_dir = Path(tempfile.mkdtemp(prefix=f".staging-{slug}-", dir=str(skills_dir)))
        backup_dir: Path | None = None
        try:
            shutil.copytree(content_root, staging_dir, dirs_exist_ok=True)
            if target_dir.exists():
                backup_dir = Path(tempfile.mkdtemp(prefix=f".backup-{slug}-", dir=str(skills_dir)))
                backup_dir.rmdir()
                target_dir.rename(backup_dir)
            staging_dir.rename(target_dir)
        except Exception:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            if backup_dir is not None and backup_dir.exists() and not target_dir.exists():
                backup_dir.rename(target_dir)
            raise
        else:
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)

    try:
        rel_install = str(Path("data") / "skills" / slug if skills_dir.name == "skills" and skills_dir.parent.name == "data" else target_dir).replace("\\", "/")
    except Exception:  # noqa: BLE001
        rel_install = f"data/skills/{slug}"

    print(
        json.dumps(
            {
                "status": "success",
                "command": "install",
                "resolved_skills_dir_by": resolved_by,
                "relative_install_path": rel_install,
                "installed_dir": str(target_dir),
                "slug": slug,
                "document_path": str(target_dir / doc_name),
                "total_files": len(files),
                "metadata": metadata,
                "security_report": security_report,
                "scripts": scripts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monolight built-in SkillHub search, inspect, and no-CLI installer.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search skills on SkillHub.")
    p_search.add_argument("keyword", help="Search keyword (Chinese or English).")
    p_search.add_argument("--category", help="Filter by category key.")
    p_search.add_argument(
        "--sort",
        dest="sort_by",
        help="Sort field, e.g. downloads, stars, score, newest.",
    )
    p_search.add_argument("--order", default="desc", choices=["asc", "desc"])
    p_search.add_argument("--source", help="Filter by source, e.g. all, official, community.")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--max-desc-chars", type=int, default=160, help="Max characters for description summary (default: 160).")
    p_search.add_argument("--full-desc", action="store_true", help="Output full description without truncating.")

    sub.add_parser("categories", help="List SkillHub categories.")

    p_list = sub.add_parser(
        "list-installed",
        help="List skills currently installed in Monolight's data/skills directory.",
    )
    p_list.add_argument("--skills-dir", help="Override target skills root directory.")

    sub.add_parser(
        "detect-dirs",
        help="List Monolight default skills directory and fallback candidate paths.",
    )

    p_inspect = sub.add_parser(
        "inspect",
        help="Fetch official security report and validate Zip package in a temp directory.",
    )
    p_inspect.add_argument(
        "source",
        help="SkillHub URL, @namespace/slug, slug, direct Zip URL, or local .zip path.",
    )

    p_install = sub.add_parser(
        "install",
        help="Validate and install a skill package into Monolight's data/skills directory.",
    )
    p_install.add_argument(
        "source",
        help="SkillHub URL, @namespace/slug, slug, direct Zip URL, or local .zip path.",
    )
    p_install.add_argument(
        "--skills-dir",
        help="Override target skills root directory (defaults to Monolight data/skills).",
    )
    p_install.add_argument(
        "--slug",
        help="Override installed folder name (defaults to SKILL.md name or URL slug).",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing target skill directory safely via staged backup.",
    )
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "categories":
            return cmd_categories()
        if args.command == "search":
            return cmd_search(args)
        if args.command == "detect-dirs":
            return cmd_detect_dirs()
        if args.command == "list-installed":
            return cmd_list_installed(args)
        if args.command == "inspect":
            return cmd_inspect(args)
        if args.command == "install":
            return cmd_install(args)
        parser.print_help()
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
