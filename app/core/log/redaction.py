import json
import re

from app.core.constants import LOG_REDACTION_PLACEHOLDER

_CREDENTIAL_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "apikey",
        "token",
        "accesstoken",
        "refreshtoken",
        "secret",
        "clientsecret",
        "secretkey",
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "privatekey",
        "credentials",
    }
)
_BUSINESS_KEYS = frozenset(
    {
        "content",
        "knowledgecontent",
        "query",
        "knowledgequery",
        "knowledgekey",
        "changeevidence",
        "error",
        "source",
    }
)
_SENSITIVE_KEYS = _CREDENTIAL_KEYS | _BUSINESS_KEYS
_SENSITIVE_KEY_PATTERN = "|".join("[-_]*".join(re.escape(character) for character in key) for key in sorted(_SENSITIVE_KEYS, key=len, reverse=True))
_CREDENTIAL_KEY_PATTERN = "|".join("[-_]*".join(re.escape(character) for character in key) for key in sorted(_CREDENTIAL_KEYS, key=len, reverse=True))
_KEY_VALUE_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?P<prefix>(?P<quote>[\"']?)(?P<key>{_SENSITIVE_KEY_PATTERN}|[A-Za-z0-9_-]*?(?:{_CREDENTIAL_KEY_PATTERN}))(?P=quote)[ \t]*[:=][ \t]*)"
    rf"(?P<value>{re.escape(LOG_REDACTION_PLACEHOLDER)}|\"(?:\\.|[^\"\\])*\"?|\'(?:\\.|[^\'\\])*\'?|[^\r\n,;&}}\]]+?(?=[ \t]+(?:[\"']?[A-Za-z][A-Za-z0-9_-]*[\"']?)[ \t]*[:=]|[,;&\r\n}}\]]|$))?",
    re.IGNORECASE,
)
_HEADER_RE = re.compile(r"(?im)^(?P<prefix>[ \t]*(?:cookie|set-cookie|authorization|proxy-authorization)[ \t]*:[ \t]*).*$")
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)
_URL_PASSWORD_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/@\s:?#]+:)([^@/\s?#]*)(@)")
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
    re.IGNORECASE,
)


def _redact_key_value_match(text: str, match: re.Match, decoder: json.JSONDecoder) -> tuple[str, int]:
    value = match.group("value") or ""
    if value == LOG_REDACTION_PLACEHOLDER:
        return match.group(0), match.end()

    value_start = match.start("value") if match.group("value") is not None else match.end()
    value_end = match.end()
    quoted = value.startswith(('"', "'"))
    if value_start < len(text) and text[value_start] in '{["':
        try:
            parsed, decoded_end = decoder.raw_decode(text, value_start)
        except (json.JSONDecodeError, RecursionError):
            pass
        else:
            if text[value_start] == '"' and isinstance(parsed, str):
                value_end = decoded_end
                quoted = True
            elif text[value_start] in "[{" and isinstance(parsed, (dict, list)):
                value_end = decoded_end
                quoted = False

    replacement = f'{match.group("prefix")}"{LOG_REDACTION_PLACEHOLDER}"' if quoted else f"{match.group('prefix')}{LOG_REDACTION_PLACEHOLDER}"
    return replacement, value_end


def _redact_embedded_json(text: str) -> str:
    text = _HEADER_RE.sub(lambda match: match.group("prefix") + LOG_REDACTION_PLACEHOLDER, text)
    parts: list[str] = []
    cursor = 0
    search_from = 0
    decoder = json.JSONDecoder()

    while search_from < len(text):
        json_start = min(
            (position for position in (text.find("{", search_from), text.find("[", search_from)) if position >= 0),
            default=-1,
        )
        match = _KEY_VALUE_RE.search(text, search_from)
        if match is not None and (json_start < 0 or match.start() < json_start):
            parts.append(_redact_plain_text(text[cursor : match.start()]))
            replacement, end = _redact_key_value_match(text, match, decoder)
            parts.append(replacement)
            cursor = end
            search_from = end
            continue

        if json_start < 0:
            break

        try:
            value, end = decoder.raw_decode(text, json_start)
        except (json.JSONDecodeError, RecursionError):
            search_from = json_start + 1
            continue

        parts.append(_redact_plain_text(text[cursor:json_start]))
        redacted = _redact_json_value(value)
        if redacted == value:
            parts.append(text[json_start:end])
        else:
            parts.append(json.dumps(redacted, ensure_ascii=False, separators=(",", ":")))
        cursor = end
        search_from = end

    parts.append(_redact_plain_text(text[cursor:]))
    return "".join(parts)


def _redact_plain_text(text: str) -> str:
    text = _PEM_PRIVATE_KEY_RE.sub(LOG_REDACTION_PLACEHOLDER, text)
    text = _AUTH_SCHEME_RE.sub(lambda match: f"{match.group(1)} {LOG_REDACTION_PLACEHOLDER}", text)
    text = _URL_PASSWORD_RE.sub(lambda match: f"{match.group(1)}{LOG_REDACTION_PLACEHOLDER}{match.group(3)}", text)
    return text


def _redact_json_value(value):
    if isinstance(value, dict):
        return {key: LOG_REDACTION_PLACEHOLDER if isinstance(key, str) and _is_sensitive_key(key) else _redact_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        try:
            nested = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return _redact_embedded_json(value)
        if isinstance(nested, (dict, list)):
            redacted = _redact_json_value(nested)
            if redacted != nested:
                return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))
        return _redact_embedded_json(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_KEYS or any(normalized.endswith(credential) for credential in _CREDENTIAL_KEYS)


def redact_log_text(value: str) -> str:
    if not value:
        return value

    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return _redact_embedded_json(value)

    if isinstance(parsed, (dict, list)):
        redacted = _redact_json_value(parsed)
        if redacted == parsed:
            return value
        return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))

    if isinstance(parsed, str):
        redacted = _redact_json_value(parsed)
        if redacted != parsed:
            return json.dumps(redacted, ensure_ascii=False)
    return value
