"""HTTP proxy configuration helpers for channels."""

import string
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import aiohttp

from app.core.constants import ERR_CHANNEL_HTTP_PROXY_INVALID
from app.core.i18n import t

_UNRESERVED_CREDENTIAL_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-._~")


def _invalid_http_proxy() -> ValueError:
    return ValueError(t(ERR_CHANNEL_HTTP_PROXY_INVALID))


def _normalize_credential(value: str) -> str:
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if index + 2 >= len(value) or any(digit not in string.hexdigits for digit in value[index + 1 : index + 3]):
                raise _invalid_http_proxy()
            index += 3
            continue
        if character not in _UNRESERVED_CREDENTIAL_CHARACTERS:
            raise _invalid_http_proxy()
        index += 1

    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _invalid_http_proxy() from exc

    if not decoded:
        raise _invalid_http_proxy()
    return quote(decoded, safe="-._~")


def normalize_http_proxy(value: object) -> str | None:
    """Validate and canonicalize an HTTP proxy URL."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid_http_proxy()
    if value == "":
        return None
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        raise _invalid_http_proxy()

    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "http":
            raise _invalid_http_proxy()
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise _invalid_http_proxy()
        if not parsed.netloc or not parsed.hostname:
            raise _invalid_http_proxy()

        port = parsed.port
        if port is None or not 1 <= port <= 65535:
            raise _invalid_http_proxy()

        if parsed.username is None and parsed.password is None:
            userinfo = ""
        elif parsed.username and parsed.password:
            userinfo = f"{_normalize_credential(parsed.username)}:{_normalize_credential(parsed.password)}@"
        else:
            raise _invalid_http_proxy()

        hostname = parsed.hostname.lower()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        return urlunsplit(("http", f"{userinfo}{hostname}:{port}", "", "", ""))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ValueError) and str(exc) == t(ERR_CHANNEL_HTTP_PROXY_INVALID):
            raise
        raise _invalid_http_proxy() from exc


def build_aiohttp_proxy_kwargs(value: str | None) -> dict:
    """Build aiohttp proxy keyword arguments from a normalized proxy URL."""
    normalized = normalize_http_proxy(value)
    if normalized is None:
        return {}

    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    proxy = urlunsplit(("http", f"{hostname}:{parsed.port}", "", "", ""))
    kwargs = {"proxy": proxy}
    if parsed.username is not None and parsed.password is not None:
        kwargs["proxy_headers"] = {"Proxy-Authorization": aiohttp.encode_basic_auth(unquote(parsed.username), unquote(parsed.password))}
    return kwargs


def get_channel_http_proxy(channel: object) -> str | None:
    """Read and normalize a channel's optional HTTP proxy setting."""
    if isinstance(channel, dict):
        value = channel.get("http_proxy")
    else:
        value = getattr(channel, "http_proxy", None)
    return normalize_http_proxy(value)
