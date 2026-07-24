import hmac
from typing import Annotated, Any

from fastapi import HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from program.settings import settings_manager


def api_key_matches(provided: str | None) -> bool:
    """Constant-time compare of a provided key against the configured API key."""

    expected = settings_manager.settings.api_key or ""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def header_auth(
    header: Any = Security(
        APIKeyHeader(
            name="x-api-key",
            auto_error=False,
        ),
    ),
):
    return api_key_matches(header if isinstance(header, str) else None)


def bearer_auth(
    bearer: HTTPAuthorizationCredentials = Security(HTTPBearer(auto_error=False)),
):
    return bool(bearer and api_key_matches(bearer.credentials))


def query_auth(api_key: Annotated[str | None, Query()] = None):
    return api_key_matches(api_key)


def resolve_api_key(
    header: bool = Security(header_auth),
    bearer: bool = Security(bearer_auth),
):
    """HTTP routes: header or Bearer only (no query-string API key)."""

    if not (header or bearer):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )


def resolve_webhook_api_key(
    header: bool = Security(header_auth),
    bearer: bool = Security(bearer_auth),
    query: bool = Security(query_auth),
):
    """Webhook routes: allow query API key for Overseerr-style notification URLs."""

    if not (header or bearer or query):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )


def resolve_ws_api_key(api_key: Annotated[str | None, Query()] = None):
    """WebSocket routes may authenticate via query (browser WS limitation)."""

    if not api_key_matches(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
