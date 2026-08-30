import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from program.settings import settings_manager

ADMIN_ROLE = "platform:admin"
ACTOR_CONTEXT_MAX_AGE_SECONDS = 300
ServiceCredentialKind = Literal["legacy", "bff"]


@dataclass(frozen=True, slots=True)
class ServiceAuthentication:
    """Authenticated backend caller type; only BFF callers may assert actors."""

    kind: ServiceCredentialKind


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Actor identity and capabilities asserted by the authenticated BFF."""

    actor_id: str
    roles: frozenset[str]
    client_id: str
    is_legacy_api_key: bool = False


def api_key_matches(provided: str | None) -> bool:
    """Constant-time compare against the legacy direct-client API key."""

    expected = settings_manager.settings.api_key or ""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _bff_api_key_matches(provided: str | None) -> bool:
    """Constant-time compare against the BFF-only backend credential."""

    expected = os.getenv("BFF_API_KEY", "")
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


def _authenticate_service(provided: str | None) -> ServiceAuthentication | None:
    if _bff_api_key_matches(provided):
        return ServiceAuthentication(kind="bff")
    if api_key_matches(provided):
        return ServiceAuthentication(kind="legacy")
    return None


def header_auth(
    header: Any = Security(
        APIKeyHeader(
            name="x-api-key",
            auto_error=False,
        ),
    ),
) -> ServiceAuthentication | None:
    return _authenticate_service(header if isinstance(header, str) else None)


def bearer_auth(
    bearer: HTTPAuthorizationCredentials = Security(HTTPBearer(auto_error=False)),
) -> ServiceAuthentication | None:
    return _authenticate_service(bearer.credentials if bearer else None)


def query_auth(api_key: Annotated[str | None, Query()] = None) -> bool:
    """Webhook and WebSocket compatibility remains limited to the legacy key."""

    return api_key_matches(api_key)


def resolve_api_key(
    header: ServiceAuthentication | None = Security(header_auth),
    bearer: ServiceAuthentication | None = Security(bearer_auth),
) -> ServiceAuthentication:
    """HTTP routes accept an authenticated BFF or legacy direct client credential."""

    authenticated = header or bearer
    if not authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
    return authenticated


def _parse_actor_roles(raw_roles: str | None) -> frozenset[str]:
    """Parse exact, comma-separated actor capabilities without substring matching."""

    return frozenset(
        role.strip() for role in (raw_roles or "").split(",") if role.strip()
    )


def _actor_signature_payload(
    actor_id: str,
    actor_roles: str,
    actor_client: str,
    actor_timestamp: str,
) -> bytes:
    return json.dumps(
        {
            "actor_id": actor_id,
            "actor_roles": actor_roles,
            "actor_client": actor_client,
            "actor_timestamp": actor_timestamp,
        },
        separators=(",", ":"),
    ).encode()


def _actor_context_signature_is_valid(
    actor_id: str,
    actor_roles: str,
    actor_client: str,
    actor_timestamp: str,
    actor_signature: str | None,
) -> bool:
    secret = os.getenv("ACTOR_CONTEXT_SECRET", "")
    if not secret or not actor_signature:
        return False

    try:
        timestamp = int(actor_timestamp)
    except (TypeError, ValueError):
        return False

    if abs(time.time() - timestamp) > ACTOR_CONTEXT_MAX_AGE_SECONDS:
        return False

    expected = hmac.new(
        secret.encode(),
        _actor_signature_payload(actor_id, actor_roles, actor_client, actor_timestamp),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(actor_signature, expected)


def resolve_actor_context(
    service: Annotated[ServiceAuthentication, Depends(resolve_api_key)],
    actor_id: Annotated[str | None, Header(alias="x-actor-id")] = None,
    actor_roles: Annotated[str | None, Header(alias="x-actor-roles")] = None,
    actor_client: Annotated[str | None, Header(alias="x-actor-client")] = None,
    actor_timestamp: Annotated[str | None, Header(alias="x-actor-timestamp")] = None,
    actor_signature: Annotated[str | None, Header(alias="x-actor-signature")] = None,
) -> ActorContext:
    """Resolve a signed BFF actor claim or a legacy direct API-key caller.

    A legacy direct client may retain the temporary administrator compatibility context,
    but it can never submit actor headers. Actor claims require both the dedicated BFF
    service key and an unexpired HMAC signature constructed by the trusted BFF.
    """

    actor_values = (actor_id, actor_roles, actor_client, actor_timestamp, actor_signature)
    has_any_actor_header = any(value is not None for value in actor_values)
    if not has_any_actor_header:
        if service.kind == "legacy":
            return ActorContext(
                actor_id="legacy-api-key",
                roles=frozenset({ADMIN_ROLE}),
                client_id="legacy-api-client",
                is_legacy_api_key=True,
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="BFF requests require a signed actor context",
        )

    if service.kind != "bff":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Actor context requires the BFF service credential",
        )

    if any(value is None for value in actor_values):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incomplete actor context",
        )

    if not _actor_context_signature_is_valid(
        actor_id, actor_roles, actor_client, actor_timestamp, actor_signature
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid actor context signature",
        )

    return ActorContext(
        actor_id=actor_id,
        roles=_parse_actor_roles(actor_roles),
        client_id=actor_client,
    )


def require_role(*required_roles: str) -> Callable[..., ActorContext]:
    """Require any listed capability; platform administrators always satisfy checks."""

    required = frozenset(role.strip() for role in required_roles if role.strip())
    if not required:
        raise ValueError("At least one required role must be provided")

    def dependency(
        actor: Annotated[ActorContext, Depends(resolve_actor_context)],
    ) -> ActorContext:
        if ADMIN_ROLE not in actor.roles and actor.roles.isdisjoint(required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient actor permissions",
            )
        return actor

    return dependency


def resolve_webhook_api_key(
    header: ServiceAuthentication | None = Security(header_auth),
    bearer: ServiceAuthentication | None = Security(bearer_auth),
    query: bool = Security(query_auth),
):
    """Webhook routes: allow the legacy key in query strings for compatibility."""

    if not (header or bearer or query):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )


def resolve_ws_api_key(api_key: Annotated[str | None, Query()] = None):
    """WebSocket routes may authenticate via legacy query key (browser limitation)."""

    if not api_key_matches(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
        )
