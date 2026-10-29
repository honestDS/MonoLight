import os
import re
import ssl
from pathlib import Path

from dotenv import load_dotenv


def _has_ca_certificates(cafile: str | None = None) -> bool:
    try:
        return ssl.create_default_context(cafile=cafile).cert_store_stats()["x509_ca"] > 0
    except OSError:
        return False


def initialize_https_certificates() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    if "SSL_CERT_FILE" in os.environ or "SSL_CERT_DIR" in os.environ:
        return

    if _has_ca_certificates():
        return

    capath = ssl.get_default_verify_paths().capath
    if capath:
        for certificate in Path(capath).glob("????????.*"):
            if re.fullmatch(r"[0-9a-fA-F]{8}\.[0-9]+", certificate.name):
                if _has_ca_certificates(str(certificate)):
                    return

    ca_files = (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/ca-bundle.pem",
        "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
        "/etc/ssl/cert.pem",
    )
    for cafile in ca_files:
        if _has_ca_certificates(cafile):
            os.environ["SSL_CERT_FILE"] = cafile
            return

    import certifi

    os.environ["SSL_CERT_FILE"] = certifi.where()
