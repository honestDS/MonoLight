import base64
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app.core.tools.send_file_to_user as token_module
from app.core.crud.account.file_download_cleanup import FileDownloadCleanupJournal


@pytest.fixture
def isolated_token_store(tmp_path, monkeypatch):
    token_dir = tmp_path / "file_download_tokens"
    monkeypatch.setattr(token_module, "FILE_TOKEN_DIR", token_dir)
    return tmp_path, token_dir


def _write_file(directory, name="document.txt", content="file contents"):
    file = directory / name
    file.write_text(content, encoding="utf-8")
    return file


def _token_data(file, uid="owner", file_id="file-id"):
    return {"uid": uid, "path": str(file.resolve()), "id": file_id}


def _record_path(token, token_dir):
    _, binding_key, _ = token.split(".")
    return token_dir / f"{binding_key}.json"


def test_same_binding_reuses_persisted_token(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)
    data = _token_data(file)

    first_token = token_module._encode_token(data)
    second_token = token_module._encode_token(data)

    assert second_token == first_token


def test_different_bindings_get_different_tokens(isolated_token_store):
    tmp_path, _ = isolated_token_store
    first_file = _write_file(tmp_path, "first.txt")
    second_file = _write_file(tmp_path, "second.txt")

    tokens = {
        "base": token_module._encode_token(_token_data(first_file)),
        "uid": token_module._encode_token(_token_data(first_file, uid="another-owner")),
        "id": token_module._encode_token(_token_data(first_file, file_id="another-id")),
        "path": token_module._encode_token(_token_data(second_file)),
    }

    assert len(set(tokens.values())) == 4


def test_token_does_not_expose_path_or_username(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)

    token = token_module._encode_token(_token_data(file))

    assert str(file.resolve()) not in token
    assert "owner" not in token


def test_token_resolves_for_owner_and_admin_but_not_other_user(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)
    token = token_module._encode_token(_token_data(file))

    assert token_module.resolve_file_token(token, uid="owner") == file.resolve()
    with pytest.raises(ValueError):
        token_module.resolve_file_token(token, uid="other")
    assert token_module.resolve_file_token(token, uid="other", is_admin=True) == file.resolve()


def test_tampered_random_secret_is_rejected(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)
    token = token_module._encode_token(_token_data(file))
    version, binding_key, _ = token.split(".")

    with pytest.raises(ValueError):
        token_module.resolve_file_token(f"{version}.{binding_key}.tampered-secret", uid="owner")


def test_unknown_binding_is_rejected(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)
    token = token_module._encode_token(_token_data(file))
    version, _, secret = token.split(".")

    with pytest.raises(ValueError):
        token_module.resolve_file_token(f"{version}.{'0' * 32}.{secret}", uid="owner")


def test_unknown_token_version_is_rejected(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)
    token = token_module._encode_token(_token_data(file))
    _, binding_key, secret = token.split(".")

    with pytest.raises(ValueError):
        token_module.resolve_file_token(f"v9.{binding_key}.{secret}", uid="owner")


def test_path_traversal_record_with_changed_binding_is_rejected(isolated_token_store):
    tmp_path, token_dir = isolated_token_store
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    file = _write_file(nested_dir)
    other_file = _write_file(tmp_path, "other.txt")
    token = token_module._encode_token(_token_data(file))
    record_path = _record_path(token, token_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["data"]["path"] = str(nested_dir / ".." / other_file.name)
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(ValueError):
        token_module.resolve_file_token(token, uid="owner")


def test_non_ascii_secret_record_is_rejected(isolated_token_store):
    tmp_path, token_dir = isolated_token_store
    file = _write_file(tmp_path)
    token = token_module._encode_token(_token_data(file))
    record_path = _record_path(token, token_dir)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["secret"] = "非法secret"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError):
        token_module.resolve_file_token(token, uid="owner")


def test_deleted_file_cannot_be_resolved(isolated_token_store):
    tmp_path, _ = isolated_token_store
    file = _write_file(tmp_path)
    token = token_module._encode_token(_token_data(file))
    file.unlink()

    with pytest.raises(FileNotFoundError):
        token_module.resolve_file_token(token, uid="owner")


def test_cleanup_replays_interrupted_removal_without_deleting_recreated_file_token(monkeypatch, isolated_token_store):
    tmp_path, token_dir = isolated_token_store
    source = _write_file(tmp_path, content="old contents")
    old_token = token_module._encode_token(_token_data(source, file_id="old-id"))
    old_record_path = _record_path(old_token, token_dir)
    source_path = source.resolve()
    original_unlink = Path.unlink

    def unlink_source_then_interrupt(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if path == source_path:
            raise RuntimeError("simulated cleanup interruption")
        return result

    journal = FileDownloadCleanupJournal(token_dir)
    with monkeypatch.context() as hook:
        hook.setattr(Path, "unlink", unlink_source_then_interrupt)
        with pytest.raises(RuntimeError, match="simulated cleanup interruption"):
            journal.unlink_file(source)

    assert not source.exists()
    assert old_record_path.is_file()
    with pytest.raises(FileNotFoundError):
        token_module.resolve_file_token(old_token, uid="owner")

    _write_file(tmp_path, name=source.name, content="new contents")
    with pytest.raises(FileNotFoundError):
        token_module.resolve_file_token(old_token, uid="owner")

    new_token = token_module._encode_token(_token_data(source, file_id="new-id"))
    new_record_path = _record_path(new_token, token_dir)
    old_mtime_ns = 946_684_800_000_000_000
    new_mtime_ns = time.time_ns() + 1_000_000_000
    os.utime(old_record_path, ns=(old_mtime_ns, old_mtime_ns))
    os.utime(new_record_path, ns=(new_mtime_ns, new_mtime_ns))

    assert old_record_path.is_file()
    assert new_record_path.is_file()
    resolved_source = token_module.resolve_file_token(new_token, uid="owner")
    assert resolved_source == source.resolve()
    assert resolved_source.read_text(encoding="utf-8") == "new contents"

    assert token_module.cleanup_file_download_tokens(replay_removals=True) == 1
    assert not old_record_path.exists()
    assert new_record_path.is_file()
    assert token_module.cleanup_file_download_tokens(replay_removals=True) == 0
    assert new_record_path.is_file()


@pytest.mark.parametrize(
    "partial_failure",
    [False, True],
    ids=["post-delete-runtime-error", "partial-delete-permission-error"],
)
def test_cleanup_replays_interrupted_tree_removal_without_deleting_recreated_file_tokens(
    monkeypatch,
    isolated_token_store,
    partial_failure,
):
    tmp_path, token_dir = isolated_token_store
    deleted_tree = tmp_path / "temp_deleted"
    sibling_tree = tmp_path / "sibling"
    boundary_tree = tmp_path / "temp_deleted-extra"
    replacement_tree = tmp_path / "replacement"
    for directory in (deleted_tree, sibling_tree, boundary_tree, replacement_tree):
        directory.mkdir()

    deleted_file = _write_file(deleted_tree, name="original.txt", content="old contents")
    if partial_failure:
        remaining_file = _write_file(deleted_tree, name="remaining.txt", content="remaining contents")
        remaining_directory = deleted_tree / "remaining"
        remaining_directory.mkdir()
        remaining_nested_file = _write_file(remaining_directory, name="nested.txt", content="nested contents")
    sibling_file = _write_file(sibling_tree, name="original.txt", content="sibling contents")
    boundary_file = _write_file(boundary_tree, name="original.txt", content="boundary contents")
    _write_file(replacement_tree, name="original.txt", content="new contents")
    assert not os.path.samestat(deleted_tree.stat(), replacement_tree.stat())

    old_token = token_module._encode_token(_token_data(deleted_file, uid="peer", file_id="old-id"))
    sibling_token = token_module._encode_token(_token_data(sibling_file, uid="peer", file_id="sibling-id"))
    boundary_token = token_module._encode_token(_token_data(boundary_file, uid="peer", file_id="boundary-id"))
    old_record_path = _record_path(old_token, token_dir)
    sibling_record_path = _record_path(sibling_token, token_dir)
    boundary_record_path = _record_path(boundary_token, token_dir)
    original_rmtree = shutil.rmtree

    def _rmtree_then_interrupt(path, *args, **kwargs):
        if partial_failure and Path(path) == deleted_tree:
            deleted_file.unlink()
            raise PermissionError("simulated partial tree cleanup failure")
        result = original_rmtree(path, *args, **kwargs)
        if Path(path) == deleted_tree:
            raise RuntimeError("simulated tree cleanup interruption")
        return result

    journal = FileDownloadCleanupJournal(token_dir)
    expected_exception = PermissionError if partial_failure else RuntimeError
    expected_message = "simulated partial tree cleanup failure" if partial_failure else "simulated tree cleanup interruption"
    with monkeypatch.context() as hook:
        hook.setattr(shutil, "rmtree", _rmtree_then_interrupt)
        with pytest.raises(expected_exception, match=expected_message):
            journal.remove_tree(deleted_tree)

    if not partial_failure:
        assert not deleted_tree.exists()
    assert replacement_tree.is_dir()
    assert old_record_path.is_file()

    if partial_failure:
        assert deleted_tree.is_dir()
        assert not deleted_file.exists()
        assert remaining_file.is_file()
        assert remaining_directory.is_dir()
        assert remaining_nested_file.is_file()
        _write_file(deleted_tree, name=deleted_file.name, content="intermediate replacement")
        assert old_record_path.is_file()
        with pytest.raises(FileNotFoundError):
            token_module.resolve_file_token(old_token, uid="peer")
        shutil.rmtree(deleted_tree)

    replacement_tree.rename(deleted_tree)
    new_file = deleted_tree / "original.txt"
    new_token = token_module._encode_token(_token_data(new_file, uid="peer", file_id="new-id"))
    new_record_path = _record_path(new_token, token_dir)
    old_mtime_ns = 946_684_800_000_000_000
    new_mtime_ns = time.time_ns() + 1_000_000_000
    os.utime(old_record_path, ns=(old_mtime_ns, old_mtime_ns))
    os.utime(new_record_path, ns=(new_mtime_ns, new_mtime_ns))

    assert old_record_path.is_file()
    with pytest.raises(FileNotFoundError):
        token_module.resolve_file_token(old_token, uid="peer")
    assert token_module.resolve_file_token(new_token, uid="peer") == new_file.resolve()
    assert new_file.read_text(encoding="utf-8") == "new contents"
    assert token_module.resolve_file_token(sibling_token, uid="peer") == sibling_file.resolve()
    assert sibling_file.read_text(encoding="utf-8") == "sibling contents"
    assert token_module.resolve_file_token(boundary_token, uid="peer") == boundary_file.resolve()
    assert boundary_file.read_text(encoding="utf-8") == "boundary contents"

    assert token_module.cleanup_file_download_tokens(replay_removals=True) == 1
    assert not old_record_path.exists()
    assert new_record_path.is_file()
    assert sibling_record_path.is_file()
    assert boundary_record_path.is_file()
    assert token_module.cleanup_file_download_tokens(replay_removals=True) == 0


def test_source_deleted_after_record_link_does_not_leave_new_token(monkeypatch, isolated_token_store):
    tmp_path, token_dir = isolated_token_store
    file = _write_file(tmp_path)
    data = _token_data(file)
    original_link = token_module.os.link

    def link_then_delete_source(source, destination, *args, **kwargs):
        original_link(source, destination, *args, **kwargs)
        file.unlink()

    monkeypatch.setattr(token_module.os, "link", link_then_delete_source)

    with pytest.raises(FileNotFoundError):
        token_module._encode_token(data)

    assert list(token_dir.glob("*.json")) == []
    assert list(token_dir.glob("*.tmp")) == []


def test_sensitive_file_cannot_get_a_token(isolated_token_store):
    tmp_path, _ = isolated_token_store
    sensitive_file = _write_file(tmp_path, ".env")

    with pytest.raises(ValueError):
        token_module._encode_token(_token_data(sensitive_file))


def test_token_is_readable_from_persisted_store_on_later_call(isolated_token_store):
    tmp_path, token_dir = isolated_token_store
    file = _write_file(tmp_path)
    data = _token_data(file)

    token = token_module._encode_token(data)
    assert _record_path(token, token_dir).is_file()

    assert token_module.resolve_file_token(token, uid="owner") == file.resolve()


def test_concurrent_generation_is_stable_and_cleans_temp_files(isolated_token_store):
    tmp_path, token_dir = isolated_token_store
    file = _write_file(tmp_path)
    data = _token_data(file)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: token_module._encode_token(data), range(32)))

    assert len(set(tokens)) == 1
    assert list(token_dir.glob("*.tmp")) == []
    assert len(list(token_dir.glob("*.json"))) == 1


def test_legacy_v1_token_keeps_owner_and_admin_access(monkeypatch, isolated_token_store):
    tmp_path, _ = isolated_token_store
    monkeypatch.setattr(token_module, "_get_encryption_key", lambda: b"fixed-v1-test-key")
    file = _write_file(tmp_path)
    payload = base64.urlsafe_b64encode(json.dumps({"uid": "owner", "path": str(file.resolve())}, separators=(",", ":")).encode("utf-8")).decode("ascii")
    token = f"v1.{payload}.{token_module._sign_payload(payload)}"

    assert token_module.resolve_file_token(token, uid="owner") == file.resolve()
    assert token_module.resolve_file_token(token, uid="other", is_admin=True) == file.resolve()
    with pytest.raises(ValueError):
        token_module.resolve_file_token(token, uid="other")
