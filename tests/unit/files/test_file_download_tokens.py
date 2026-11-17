import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.core.tools.send_file_to_user as token_module


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
