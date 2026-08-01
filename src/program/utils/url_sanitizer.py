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
    Return a URL safe for logs by redacting sensitive query values and
    any user:password credentials embedded in the URL authority.

    Args:
        url: URL string possibly containing sensitive query parameters or
             basic-auth credentials in the form user:password@host.
    """
    try:
        parsed = urlsplit(url)

        # Redact password embedded in netloc authority (FIX-06).
        netloc = parsed.netloc
        if parsed.password:
            netloc = netloc.replace(f":{parsed.password}@", ":[redacted]@")

        if not parsed.query:
            if netloc == parsed.netloc:
                return url
            return urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )

        query = parse_qsl(parsed.query, keep_blank_values=True)
        sanitized = [
            (key, "[redacted]") if key.lower() in SENSITIVE_URL_QUERY_PARAMS else (key, value)
            for key, value in query
        ]

        return urlunsplit(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                urlencode(sanitized, doseq=True),
                parsed.fragment,
            )
        )
    except Exception:
        return url

