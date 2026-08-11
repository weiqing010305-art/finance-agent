from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


SENSITIVE_QUERY_KEYS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "client_secret",
    "credential", "id_token", "key", "password", "passwd", "refresh_token", "secret",
    "session_token", "sig", "signature", "token",
    "x-amz-credential", "x-amz-security-token", "x-amz-signature",
    "x-goog-credential", "x-goog-signature",
}

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?[^\s,;}\]]+"
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|token|password|passwd|secret|"
    r"credential|auth)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^&\s,;}\]]+)"
)
_BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;}\]]+")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def redact_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return value
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    query = [
        (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunparse(
        parsed._replace(netloc=f"{hostname}{port}", query=urlencode(query), fragment="")
    )


def redact_text(value: str) -> str:
    result = _URL.sub(lambda match: redact_url(match.group(0)), value)
    result = _AUTHORIZATION.sub("Authorization=[REDACTED]", result)
    result = _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", result)
    result = _BEARER.sub("Bearer [REDACTED]", result)
    result = _SK_KEY.sub("[REDACTED]", result)
    return result
