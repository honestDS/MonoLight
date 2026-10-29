import os
from types import SimpleNamespace

import certifi
import pytest

from app import https_certificates
from app.adapters.weixin_openclaw.client import WeixinOpenClawClient
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig


def _clear_certificate_environment(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


def _disable_dotenv_loading(monkeypatch):
    monkeypatch.setattr(https_certificates, "load_dotenv", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SSL_CERT_FILE", "configured-certificate.pem"),
        ("SSL_CERT_DIR", "configured-certificate-directory"),
    ],
)
def test_initialize_https_certificates_preserves_explicit_environment(monkeypatch, variable, value):
    _clear_certificate_environment(monkeypatch)
    _disable_dotenv_loading(monkeypatch)
    monkeypatch.setenv(variable, value)
    before = {name: os.environ.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")}

    https_certificates.initialize_https_certificates()

    after = {name: os.environ.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
    assert after == before


def test_initialize_https_certificates_preserves_dotenv_environment(monkeypatch, tmp_path):
    _clear_certificate_environment(monkeypatch)
    application_directory = tmp_path / "app"
    application_directory.mkdir()
    (tmp_path / ".env").write_text("SSL_CERT_DIR=dotenv-certificate-directory\n", encoding="utf-8")
    monkeypatch.setattr(https_certificates, "__file__", str(application_directory / "https_certificates.py"))

    try:
        https_certificates.initialize_https_certificates()

        assert os.environ.get("SSL_CERT_DIR") == "dotenv-certificate-directory"
        assert os.environ.get("SSL_CERT_FILE") is None
    finally:
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("SSL_CERT_DIR", None)


def test_initialize_https_certificates_does_not_inject_file_when_default_context_has_ca(monkeypatch):
    _clear_certificate_environment(monkeypatch)
    _disable_dotenv_loading(monkeypatch)

    def fake_create_default_context(*, cafile=None):
        return SimpleNamespace(cert_store_stats=lambda: {"x509_ca": int(cafile is None)})

    monkeypatch.setattr(https_certificates.ssl, "create_default_context", fake_create_default_context)

    https_certificates.initialize_https_certificates()

    assert os.environ.get("SSL_CERT_FILE") is None
    assert os.environ.get("SSL_CERT_DIR") is None


def test_initialize_https_certificates_does_not_inject_file_when_capath_has_hashed_certificate(monkeypatch, tmp_path):
    _clear_certificate_environment(monkeypatch)
    _disable_dotenv_loading(monkeypatch)
    capath = tmp_path / "certificates"
    capath.mkdir()
    hashed_certificate = capath / "a1b2c3d4.0"
    hashed_certificate.touch()
    monkeypatch.setattr(https_certificates.ssl, "get_default_verify_paths", lambda: SimpleNamespace(capath=str(capath)))

    def fake_create_default_context(*, cafile=None):
        return SimpleNamespace(cert_store_stats=lambda: {"x509_ca": int(cafile == str(hashed_certificate))})

    monkeypatch.setattr(https_certificates.ssl, "create_default_context", fake_create_default_context)

    https_certificates.initialize_https_certificates()

    assert os.environ.get("SSL_CERT_FILE") is None
    assert os.environ.get("SSL_CERT_DIR") is None


def test_initialize_https_certificates_uses_first_valid_system_ca_file(monkeypatch):
    _clear_certificate_environment(monkeypatch)
    _disable_dotenv_loading(monkeypatch)
    valid_system_ca_file = "/etc/ssl/certs/ca-certificates.crt"
    monkeypatch.setattr(https_certificates.ssl, "get_default_verify_paths", lambda: SimpleNamespace(capath=None))

    def fake_create_default_context(*, cafile=None):
        if cafile == valid_system_ca_file:
            return SimpleNamespace(cert_store_stats=lambda: {"x509_ca": 1})
        raise OSError

    monkeypatch.setattr(https_certificates.ssl, "create_default_context", fake_create_default_context)

    try:
        https_certificates.initialize_https_certificates()

        assert os.environ.get("SSL_CERT_FILE") == valid_system_ca_file
    finally:
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("SSL_CERT_DIR", None)


def test_initialize_https_certificates_uses_certifi_when_no_system_ca_file_is_valid(monkeypatch):
    _clear_certificate_environment(monkeypatch)
    _disable_dotenv_loading(monkeypatch)
    monkeypatch.setattr(https_certificates.ssl, "get_default_verify_paths", lambda: SimpleNamespace(capath=None))
    monkeypatch.setattr(
        https_certificates.ssl,
        "create_default_context",
        lambda **_kwargs: SimpleNamespace(cert_store_stats=lambda: {"x509_ca": 0}),
    )

    try:
        https_certificates.initialize_https_certificates()

        assert os.environ.get("SSL_CERT_FILE") == certifi.where()
    finally:
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("SSL_CERT_DIR", None)


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def text(self):
        return "{}"


class _FakeSession:
    closed = False

    def __init__(self):
        self.request_calls = []

    def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
        return _FakeResponse()


@pytest.mark.asyncio
async def test_weixin_openclaw_request_json_keeps_aiohttp_tls_defaults():
    client = WeixinOpenClawClient(
        WeixinOpenClawConfig(
            token="test-token",
            base_url="https://example.test",
            api_timeout_ms=1_000,
        )
    )
    session = _FakeSession()
    client.session = session

    assert await client.request_json("POST", "ilink/bot/test", payload={}) == {}

    assert len(session.request_calls) == 1
    method, url, request_kwargs = session.request_calls[0]
    assert method == "POST"
    assert url == "https://example.test/ilink/bot/test"
    assert "ssl" not in request_kwargs
