"""Utility helpers for safe URL logging."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_URL_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "apikey",
        "api_key",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
    }
)


def sanitize_url_for_logs(url: str) -> str:
    """
    Return a URL safe for logs by redacting sensitive query values.

    Args:
        url: URL string possibly containing sensitive query parameters.
    """
    try:
        parsed = urlsplit(url)
        if not parsed.query:
            return url

        query = parse_qsl(parsed.query, keep_blank_values=True)
        sanitized = [
            (key, "[redacted]") if key.lower() in SENSITIVE_URL_QUERY_PARAMS else (key, value)
            for key, value in query
        ]

        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(sanitized, doseq=True),
                parsed.fragment,
            )
        )
    except Exception:
        return url

