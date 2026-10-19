import json
import os
from pathlib import Path

import pytest

from app.core import paths
from app.core.constants import (
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_METADATA_MAX_BYTES,
    SKILL_NAME_MAX_CHARS,
    SKILL_PAGE_MAX_LIMIT,
    SKILLS_DIRECTORY,
)
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.skills import browse_skill, list_skills
from app.core.tools.skill import SkillExecutor


@pytest.fixture(autouse=True)
def isolate_shared_skills_root(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / SKILLS_DIRECTORY)


def _executor(project_root, *, uid="u1"):
    return SkillExecutor(project_root=str(project_root), uid=uid)


def _write_skill(skills_root, skill_id, contents=None):
    package = skills_root / skill_id
    package.mkdir(parents=True)
    if contents is not None:
        document = package / "SKILL.md"
        if isinstance(contents, bytes):
            document.write_bytes(contents)
        else:
            document.write_text(contents, encoding="utf-8")
    return package


def _create_symlink_or_skip(target, link, *, target_is_directory=False):
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")


def test_list_missing_root_returns_empty_without_creating_it(tmp_path):
    skills_root = tmp_path / "missing-skills"

    result = list_skills(skills_root)

    assert result["status"] == "success"
    assert result["skills"] == []
    assert result["total"] == 0
    assert result["truncated"] is False
    assert result["next_offset"] is None
    assert not skills_root.exists()


def test_list_reflects_package_and_metadata_changes_on_each_call(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    package = _write_skill(skills_root, "dynamic", "---\nname: First\ndescription: old\n---\n")

    first = list_skills(skills_root)["skills"][0]
    (package / "SKILL.md").write_text("---\nname: Updated\ndescription: new\n---\n", encoding="utf-8")
    updated = list_skills(skills_root)["skills"][0]
    (package / "SKILL.md").unlink()
    package.rmdir()
    deleted = list_skills(skills_root)

    assert (first["name"], first["description"]) == ("First", "old")
    assert (updated["name"], updated["description"]) == ("Updated", "new")
    assert deleted["skills"] == []


@pytest.mark.asyncio
async def test_skill_list_is_shared_across_executor_uids(tmp_path):
    skills_root = tmp_path / SKILLS_DIRECTORY
    _write_skill(skills_root, "shared", "---\nname: Shared\n---\n")
    first_user = json.loads(await _executor(tmp_path, uid="user-a").execute(operation="list"))
    second_user = json.loads(await _executor(tmp_path, uid="user-b").execute(operation="list"))

    assert first_user == second_user
    assert first_user["status"] == "success"
    assert [skill["id"] for skill in first_user["skills"]] == ["shared"]


def test_list_parses_multiline_yaml_and_bom_crlf_without_using_directory_name(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(
        skills_root,
        "package-id",
        "\ufeff---\r\nname: Shown Name\r\ndescription: >-\r\n  first line\r\n  second line\r\nversion: 3\r\n---\r\nbody\r\n".encode(),
    )
    _write_skill(skills_root, "name-only", "---\nname: Name Only\n---\n")

    skills = {skill["id"]: skill for skill in list_skills(skills_root)["skills"]}

    assert skills["package-id"]["name"] == "Shown Name"
    assert skills["package-id"]["description"] == "first line second line"
    assert skills["name-only"]["name"] == "Name Only"
    assert skills["name-only"]["description"] == ""


@pytest.mark.parametrize(
    ("description_field", "expected_description"),
    [
        ("", ""),
        ("description: null", ""),
        ("description: 42", ""),
        ("description: {nested: value}", ""),
        ('description: ""', ""),
    ],
    ids=["missing", "null", "number", "object", "empty"],
)
def test_non_string_descriptions_do_not_replace_name_or_get_stringified(tmp_path, description_field, expected_description):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(
        skills_root,
        "typed-values",
        f"---\nname: Retained Name\n{description_field}\nauthor: 7\nlicense: [MIT]\n---\n",
    )

    skill = list_skills(skills_root)["skills"][0]

    assert skill["name"] == "Retained Name"
    assert skill["description"] == expected_description
    assert "author" not in skill
    assert "license" not in skill


def test_non_string_name_and_description_fall_back_to_directory_id(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(skills_root, "fallback-id", "---\nname: 42\ndescription: [one, two]\n---\n")

    skill = list_skills(skills_root)["skills"][0]

    assert skill["name"] == "fallback-id"
    assert skill["description"] == ""


def test_malformed_yaml_recovers_quoted_escaped_single_line_name(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(
        skills_root,
        "broken-yaml",
        '---\nname: "Recovered \\"quoted\\" name"\ndescription: [unfinished\n---\n',
    )

    skill = list_skills(skills_root)["skills"][0]

    assert skill["name"] == 'Recovered "quoted" name'
    assert skill["description"] == ""


def test_unclosed_frontmatter_keeps_recoverable_name(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(skills_root, "unclosed", "---\nname: 'Still Recoverable'\ndescription: unfinished\n")

    skill = list_skills(skills_root)["skills"][0]

    assert skill["name"] == "Still Recoverable"
    assert skill["description"] == ""


def test_unrecoverable_or_missing_metadata_falls_back_to_directory_id(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(skills_root, "no-name", "---\ndescription: [unfinished\n---\n")
    _write_skill(skills_root, "no-frontmatter", "A document without metadata\nname: Not metadata\n")
    _write_skill(skills_root, "no-document")

    skills = {skill["id"]: skill for skill in list_skills(skills_root)["skills"]}

    for skill_id in ("no-name", "no-frontmatter", "no-document"):
        assert skills[skill_id]["name"] == skill_id
        assert skills[skill_id]["description"] == ""


def test_large_body_does_not_change_a_complete_frontmatter_header(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    contents = "---\nname: Header Name\ndescription: Header description\n---\n" + "body " * SKILL_METADATA_MAX_BYTES
    _write_skill(skills_root, "large-body", contents)

    skill = list_skills(skills_root)["skills"][0]

    assert skill["name"] == "Header Name"
    assert skill["description"] == "Header description"


def test_metadata_header_and_field_lengths_are_bounded_in_list_output(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    long_name = "n" * (SKILL_NAME_MAX_CHARS + 20)
    long_description = "d" * (SKILL_DESCRIPTION_MAX_CHARS + 20)
    _write_skill(skills_root, "long-fields", f"---\nname: {long_name}\ndescription: {long_description}\n---\n")
    _write_skill(
        skills_root,
        "long-header",
        "---\nname: Header Survives\npadding: " + "x" * SKILL_METADATA_MAX_BYTES + "\n---\n",
    )

    skills = {skill["id"]: skill for skill in list_skills(skills_root)["skills"]}

    assert skills["long-fields"]["name"] == long_name[:SKILL_NAME_MAX_CHARS]
    assert skills["long-fields"]["description"] == long_description[:SKILL_DESCRIPTION_MAX_CHARS]
    assert len(skills["long-fields"]["name"]) <= SKILL_NAME_MAX_CHARS
    assert len(skills["long-fields"]["description"]) <= SKILL_DESCRIPTION_MAX_CHARS
    assert skills["long-header"]["name"] == "Header Survives"
    assert skills["long-header"]["description"] == ""


def test_deep_malformed_yaml_in_one_package_does_not_block_other_packages(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    nested_value = "[" * 1200 + "unfinished"
    _write_skill(skills_root, "a-deeply-nested", f"---\nname: Nested\nvalue: {nested_value}\n---\n")
    _write_skill(skills_root, "z-healthy", "---\nname: Healthy\n---\n")

    result = list_skills(skills_root)

    assert result["status"] == "success"
    assert [skill["id"] for skill in result["skills"]] == ["a-deeply-nested", "z-healthy"]
    assert result["skills"][1]["name"] == "Healthy"


def test_list_sorts_and_paginates_through_last_and_out_of_range_pages(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    for skill_id in ("echo", "alpha", "delta", "bravo", "charlie"):
        _write_skill(skills_root, skill_id)

    first = list_skills(skills_root, limit=2)
    second = list_skills(skills_root, offset=first["next_offset"], limit=2)
    last = list_skills(skills_root, offset=4, limit=2)
    beyond = list_skills(skills_root, offset=20, limit=2)

    assert [skill["id"] for skill in first["skills"]] == ["alpha", "bravo"]
    assert first["truncated"] is True
    assert first["next_offset"] == 2
    assert [skill["id"] for skill in second["skills"]] == ["charlie", "delta"]
    assert [skill["id"] for skill in last["skills"]] == ["echo"]
    assert last["truncated"] is False
    assert last["next_offset"] is None
    assert beyond["skills"] == []
    assert beyond["truncated"] is False
    assert beyond["next_offset"] is None


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(True, 1), (-1, 1), (0, True), (0, 0), (0, SKILL_PAGE_MAX_LIMIT + 1)],
    ids=["boolean-offset", "negative-offset", "boolean-limit", "zero-limit", "over-limit"],
)
def test_list_rejects_invalid_pagination(tmp_path, offset, limit):
    with pytest.raises(ValueError):
        list_skills(tmp_path / "skills", offset=offset, limit=limit)


def test_list_ignores_hidden_directories_and_regular_files(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _write_skill(skills_root, ".hidden", "---\nname: Hidden\n---\n")
    _write_skill(skills_root, "visible")
    (skills_root / "ordinary-file").write_text("not a skill directory", encoding="utf-8")

    result = list_skills(skills_root)

    assert [skill["id"] for skill in result["skills"]] == ["visible"]


def test_browse_returns_recursive_relative_entries_and_absolute_real_paths(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "tree")
    (package / "alpha" / "nested").mkdir(parents=True)
    (package / "alpha" / "a.txt").write_text("a", encoding="utf-8")
    (package / "alpha" / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    (package / "omega.txt").write_text("omega", encoding="utf-8")

    result = browse_skill(skills_root, "tree")

    assert result["status"] == "success"
    assert [entry["relative_path"] for entry in result["entries"]] == [
        "alpha",
        "alpha/a.txt",
        "alpha/nested",
        "alpha/nested/deep.txt",
        "omega.txt",
    ]
    for entry in result["entries"]:
        assert Path(entry["path"]).is_absolute()
        assert Path(entry["path"]) == (package / entry["relative_path"]).resolve()


def test_browse_pagination_continues_across_nested_and_root_entries_without_gaps(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "paged")
    (package / "alpha" / "nested").mkdir(parents=True)
    (package / "alpha" / "a.txt").write_text("a", encoding="utf-8")
    (package / "alpha" / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    (package / "omega.txt").write_text("omega", encoding="utf-8")

    first = browse_skill(skills_root, "paged", limit=3)
    second = browse_skill(skills_root, "paged", offset=first["next_offset"], limit=3)
    all_entries = first["entries"] + second["entries"]

    assert first["truncated"] is True
    assert first["next_offset"] == 3
    assert second["truncated"] is False
    assert second["next_offset"] is None
    assert [entry["relative_path"] for entry in all_entries] == [
        "alpha",
        "alpha/a.txt",
        "alpha/nested",
        "alpha/nested/deep.txt",
        "omega.txt",
    ]


def test_browse_can_start_at_a_subdirectory_and_empty_directories_succeed(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "subtree")
    empty_dir = package / "empty"
    nested_dir = package / "parent" / "child"
    empty_dir.mkdir()
    nested_dir.mkdir(parents=True)
    (nested_dir / "leaf.txt").write_text("leaf", encoding="utf-8")

    nested = browse_skill(skills_root, "subtree", path="parent/child")
    empty = browse_skill(skills_root, "subtree", path="empty")

    assert nested["path"] == str(nested_dir.resolve())
    assert [entry["relative_path"] for entry in nested["entries"]] == ["parent/child/leaf.txt"]
    assert empty["status"] == "success"
    assert empty["entries"] == []
    assert empty["truncated"] is False


def test_browse_output_excludes_document_contents_and_does_not_execute_scripts(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "read-only", "---\nname: Private Name\ndescription: Private Description\n---\nsecret document body")
    marker = package / "script-was-run.txt"
    script = package / "effect.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n", encoding="utf-8")

    result = browse_skill(skills_root, "read-only")
    serialized = json.dumps(result)

    assert "secret document body" not in serialized
    assert "Private Name" not in serialized
    assert "Private Description" not in serialized
    assert all(set(entry) == {"name", "path", "relative_path", "type"} for entry in result["entries"])
    assert not marker.exists()


def test_browse_rejects_invalid_skill_ids(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    invalid_ids = [
        ".hidden",
        "..",
        "../outside",
        str(tmp_path),
        r"C:\outside",
        r"\\server\share\skill",
        r"nested\skill",
        "drive:skill",
        "bad\0id",
    ]

    for skill_id in invalid_ids:
        with pytest.raises(ValueError):
            browse_skill(skills_root, skill_id)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "nested/../../outside",
        "/rooted",
        r"\rooted",
        r"C:\outside",
        "C:outside",
        r"\\server\share\outside",
        "drive:name",
        "bad\0path",
    ],
    ids=["parent", "nested-parent", "posix-root", "windows-root", "drive-root", "drive-relative", "unc", "colon", "nul"],
)
def test_browse_rejects_paths_outside_the_skill(tmp_path, path):
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "safe")

    with pytest.raises(ValueError):
        browse_skill(skills_root, "safe", path=path)


def test_browse_reports_missing_packages_and_file_targets(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "package")
    (skills_root / "plain-file").write_text("not a package", encoding="utf-8")
    (package / "plain-file").write_text("not a directory", encoding="utf-8")

    for skill_id, path in (("missing", "."), ("plain-file", "."), ("package", "plain-file")):
        with pytest.raises(ValueError):
            browse_skill(skills_root, skill_id, path=path)


def test_package_symlinks_outside_root_or_to_root_are_not_listed_or_browsed(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    external_package = tmp_path / "external-package"
    external_package.mkdir()
    _create_symlink_or_skip(external_package, skills_root / "external-link", target_is_directory=True)
    _create_symlink_or_skip(skills_root, skills_root / "root-link", target_is_directory=True)

    result = list_skills(skills_root)

    assert result["skills"] == []
    assert result["total"] == 0
    for skill_id in ("external-link", "root-link"):
        with pytest.raises(ValueError):
            browse_skill(skills_root, skill_id)


def test_browse_does_not_expose_package_entries_linked_outside_package(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "linked")
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "secret.txt"
    external_file.write_text("outside", encoding="utf-8")
    (package / "visible.txt").write_text("inside", encoding="utf-8")
    _create_symlink_or_skip(external_file, package / "external-file.txt")
    _create_symlink_or_skip(external_dir, package / "external-dir", target_is_directory=True)

    result = browse_skill(skills_root, "linked")

    assert [entry["relative_path"] for entry in result["entries"]] == ["visible.txt"]


def test_browse_shows_safe_internal_directory_links_without_following_them(tmp_path):
    skills_root = tmp_path / "skills"
    package = _write_skill(skills_root, "internal-links")
    actual_dir = package / "actual"
    actual_dir.mkdir()
    (actual_dir / "leaf.txt").write_text("leaf", encoding="utf-8")
    _create_symlink_or_skip(actual_dir, package / "safe-link", target_is_directory=True)
    _create_symlink_or_skip(actual_dir, actual_dir / "cycle", target_is_directory=True)

    result = browse_skill(skills_root, "internal-links")
    relative_paths = [entry["relative_path"] for entry in result["entries"]]
    safe_link = next(entry for entry in result["entries"] if entry["relative_path"] == "safe-link")

    assert relative_paths == ["actual", "actual/cycle", "actual/leaf.txt", "safe-link"]
    assert safe_link["type"] == "directory"
    assert Path(safe_link["path"]) == actual_dir.resolve()
    assert "safe-link/leaf.txt" not in relative_paths
    assert "actual/cycle/leaf.txt" not in relative_paths


@pytest.mark.asyncio
async def test_skill_executor_supports_list_and_browse_only(tmp_path):
    skills_root = tmp_path / SKILLS_DIRECTORY
    package = _write_skill(skills_root, "tool-package", "---\nname: Tool Package\n---\n")
    (package / "inside.txt").write_text("content", encoding="utf-8")
    executor = _executor(tmp_path)

    listed = json.loads(await executor.execute(operation="list"))
    browsed = json.loads(await executor.execute(operation="browse", skill_id="tool-package"))

    assert listed["status"] == "success"
    assert [skill["id"] for skill in listed["skills"]] == ["tool-package"]
    assert browsed["status"] == "success"
    assert {entry["relative_path"] for entry in browsed["entries"]} == {"SKILL.md", "inside.txt"}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "run"], ids=["read", "run"])
async def test_skill_executor_rejects_non_browse_operations_without_creating_files(tmp_path, operation):
    skills_root = tmp_path / SKILLS_DIRECTORY
    result = json.loads(await _executor(tmp_path).execute(operation=operation))

    assert result["status"] == "failed"
    assert result["operation"] == operation
    assert result["error"]
    assert not skills_root.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_skill_executor_reports_missing_browse_id_and_localized_pagination_errors(tmp_path):
    executor = _executor(tmp_path)
    localized_errors = {}

    for locale in ("zh", "en"):
        token = set_current_locale(locale)
        try:
            missing_id = json.loads(await executor.execute(operation="browse"))
            bad_page = json.loads(await executor.execute(operation="browse", skill_id="missing", offset=-1))
        finally:
            reset_current_locale(token)

        assert missing_id["status"] == "failed"
        assert bad_page["status"] == "failed"
        localized_errors[locale] = (missing_id["error"], bad_page["error"])

    assert localized_errors["zh"] == (
        "技能标识无效，必须使用列表返回的单个目录名",
        "技能分页参数无效",
    )
    assert localized_errors["en"] == (
        "Invalid skill identifier; use a single directory name returned by the list operation",
        "Invalid pagination",
    )
