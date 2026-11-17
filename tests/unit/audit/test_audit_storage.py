import json
import os

import pytest

from app.core.audit.storage import cleanup_audit_storage, validate_audit_file_path, write_audit_json, write_audit_json_and_associate
from app.core.paths import get_audit_file_path, get_user_audit_dir


@pytest.mark.asyncio
async def test_write_audit_json_uses_atomic_user_file(tmp_path):
    payload = {"tools": [{"name": "execute_shell", "arguments": {"command": "echo 测试"}}], "status": "pending"}

    stored_path = await write_audit_json(uid="u1", audit_record_id=12, payload=payload, audit_root=tmp_path)

    assert stored_path == (tmp_path / "temp_u1" / "audit_12.json").resolve()
    assert json.loads(stored_path.read_text(encoding="utf-8")) == payload
    assert not list(stored_path.parent.glob("*.tmp"))
    assert stored_path.is_absolute()


def test_validate_audit_file_path_rejects_other_user_directory(tmp_path):
    other_user_file = tmp_path / "temp_u2" / "audit_1.json"
    other_user_file.parent.mkdir()
    other_user_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="对应用户目录"):
        validate_audit_file_path(other_user_file.resolve(), uid="u1", audit_record_id=1, audit_root=tmp_path, require_exists=True)


def test_validate_audit_file_path_rejects_invalid_file_name(tmp_path):
    invalid_file = tmp_path / "temp_u1" / "audit_report.json"
    invalid_file.parent.mkdir()
    invalid_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="文件名无效"):
        validate_audit_file_path(invalid_file.resolve(), uid="u1", audit_root=tmp_path, require_exists=True)


@pytest.mark.asyncio
async def test_write_audit_json_associates_validated_absolute_path(tmp_path):
    associated_paths = []

    async def associate_path(path):
        associated_paths.append(path)

    stored_path = await write_audit_json_and_associate(
        uid="u1",
        audit_record_id=7,
        payload={"status": "passed"},
        associate_path=associate_path,
        audit_root=tmp_path,
    )

    assert associated_paths == [str(stored_path)]
    assert stored_path.is_absolute()
    assert stored_path.exists()


@pytest.mark.asyncio
async def test_write_audit_json_rejects_symlink_user_directory(tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    user_dir = tmp_path / "temp_u1"
    try:
        os.symlink(outside_dir, user_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    with pytest.raises(ValueError, match="不能是链接"):
        await write_audit_json(uid="u1", audit_record_id=1, payload={"status": "pending"}, audit_root=tmp_path)

    assert not (outside_dir / "audit_1.json").exists()


def test_cleanup_audit_storage_deletes_only_expired_files_without_database_paths(tmp_path):
    user_dir = get_user_audit_dir("u1", audit_root=tmp_path)
    user_dir.mkdir()
    expired_file = user_dir / "audit_1.json"
    fresh_file = user_dir / "audit_2.json"
    stale_temp_file = user_dir / ".audit_3.deadbeef.tmp"
    expired_file.write_text("{}", encoding="utf-8")
    fresh_file.write_text("{}", encoding="utf-8")
    stale_temp_file.write_text("{}", encoding="utf-8")
    now = 2_000_000_000.0
    expired_time = now - 91 * 86400
    fresh_time = now - 10 * 86400
    os.utime(expired_file, (expired_time, expired_time))
    os.utime(stale_temp_file, (expired_time, expired_time))
    os.utime(fresh_file, (fresh_time, fresh_time))

    result = cleanup_audit_storage(retention_days=90, audit_root=tmp_path, now_timestamp=now)

    assert not expired_file.exists()
    assert not stale_temp_file.exists()
    assert fresh_file.exists()
    assert sorted(result.deleted_files) == sorted([str(expired_file.resolve()), str(stale_temp_file.resolve())])
    assert result.missing_referenced_files == []


def test_cleanup_audit_storage_reports_missing_references_and_removes_orphans(tmp_path):
    user_dir = get_user_audit_dir("u1", audit_root=tmp_path)
    user_dir.mkdir()
    referenced_file = user_dir / "audit_1.json"
    orphan_file = user_dir / "audit_2.json"
    missing_file = get_audit_file_path("u1", 3, audit_root=tmp_path)
    referenced_file.write_text("{}", encoding="utf-8")
    orphan_file.write_text("{}", encoding="utf-8")

    result = cleanup_audit_storage(
        retention_days=90,
        audit_root=tmp_path,
        referenced_paths={str(referenced_file.resolve()), str(missing_file.resolve())},
    )

    assert referenced_file.exists()
    assert not orphan_file.exists()
    assert result.missing_referenced_files == [str(missing_file.resolve())]


@pytest.mark.parametrize("uid", ["", ".", "..", "../u1", "u1/sub", "u1\\sub"])
def test_get_user_audit_dir_rejects_invalid_uid(tmp_path, uid):
    with pytest.raises(ValueError):
        get_user_audit_dir(uid, audit_root=tmp_path)
