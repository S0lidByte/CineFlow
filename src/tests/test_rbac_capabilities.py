"""Regression tests for trusted actor context and capability authorization."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import auth

TEST_SECRET = "test-actor-context-secret-12345"


@pytest.fixture(autouse=True)
def configured_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth.settings_manager,
        "settings",
        MagicMock(api_key="r" * 32),
    )
    monkeypatch.setenv("ACTOR_CONTEXT_SECRET", TEST_SECRET)


def test_legacy_key_remains_legacy_when_actor_secret_exists_without_bff_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BFF_API_KEY", raising=False)

    service = auth._authenticate_service("r" * 32)

    assert service == auth.ServiceAuthentication(kind="legacy")


def _sign(
    actor_id: str,
    actor_roles: str,
    actor_client: str,
    timestamp: str,
    secret: str = TEST_SECRET,
) -> str:
    payload = json.dumps(
        {
            "actor_id": actor_id,
            "actor_roles": actor_roles,
            "actor_client": actor_client,
            "actor_timestamp": timestamp,
        },
        separators=(",", ":"),
    ).encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _actor(
    roles: str | None,
    *,
    actor_id: str = "user-123",
    client_id: str = "client-456",
    timestamp: str | None = None,
    signature: str | None = None,
    service: auth.ServiceAuthentication | None = None,
) -> auth.ActorContext:
    ts = timestamp or str(int(time.time()))
    sig = signature or _sign(actor_id, roles or "", client_id, ts)
    svc = service or auth.ServiceAuthentication(kind="bff")
    return auth.resolve_actor_context(
        svc,
        actor_id=actor_id,
        actor_roles=roles,
        actor_client=client_id,
        actor_timestamp=ts,
        actor_signature=sig,
    )


@pytest.mark.parametrize(
    ("roles", "required_role"),
    [
        ("media:request,library:read", "media:request"),
        ("playback:operator", "playback:operator"),
        ("settings:write", "settings:write"),
        ("library:read", "library:read"),
        ("platform:admin", "settings:write"),
    ],
)
def test_capability_guard_allows_exact_capability_or_platform_admin(
    roles: str, required_role: str
) -> None:
    actor = _actor(roles)

    result = auth.require_role(required_role)(actor)

    assert result is actor


@pytest.mark.parametrize(
    ("roles", "required_role"),
    [
        ("media:request", "playback:operator"),
        ("library:read", "media:request"),
        ("settings:write", "platform:admin"),
        ("playback:operators", "playback:operator"),
    ],
)
def test_capability_guard_denies_missing_or_substring_capability(
    roles: str, required_role: str
) -> None:
    with pytest.raises(HTTPException) as exc:
        auth.require_role(required_role)(_actor(roles))

    assert exc.value.status_code == 403
    assert exc.value.detail == "Insufficient actor permissions"


def test_actor_role_parser_trims_and_discards_empty_values() -> None:
    assert auth._parse_actor_roles(" media:request, , playback:operator ") == {
        "media:request",
        "playback:operator",
    }


def test_bare_legacy_key_receives_temporary_legacy_administrator_context() -> None:
    service = auth.ServiceAuthentication(kind="legacy")
    actor = auth.resolve_actor_context(service)

    assert actor.actor_id == "legacy-api-key"
    assert actor.client_id == "legacy-api-client"
    assert actor.roles == frozenset({auth.ADMIN_ROLE})
    assert actor.is_legacy_api_key is True
    assert auth.require_role("platform:admin")(actor) is actor


def test_direct_legacy_caller_cannot_assert_actor_headers() -> None:
    service = auth.ServiceAuthentication(kind="legacy")
    ts = str(int(time.time()))
    sig = _sign("user-123", "platform:admin", "untrusted-client", ts)

    with pytest.raises(HTTPException) as exc:
        auth.resolve_actor_context(
            service,
            actor_id="user-123",
            actor_roles="platform:admin",
            actor_client="untrusted-client",
            actor_timestamp=ts,
            actor_signature=sig,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Actor context requires the BFF service credential"


def test_untrusted_client_with_forged_signature_is_rejected() -> None:
    service = auth.ServiceAuthentication(kind="bff")
    ts = str(int(time.time()))

    with pytest.raises(HTTPException) as exc:
        auth.resolve_actor_context(
            service,
            actor_id="user-123",
            actor_roles="platform:admin",
            actor_client="cineflow-web-bff",
            actor_timestamp=ts,
            actor_signature="bad-forged-signature-hex",
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid actor context signature"


def test_expired_actor_signature_is_rejected() -> None:
    service = auth.ServiceAuthentication(kind="bff")
    ts = str(int(time.time()) - 301)  # expired by > 300s
    sig = _sign("user-123", "library:read", "cineflow-web-bff", ts)

    with pytest.raises(HTTPException) as exc:
        auth.resolve_actor_context(
            service,
            actor_id="user-123",
            actor_roles="library:read",
            actor_client="cineflow-web-bff",
            actor_timestamp=ts,
            actor_signature=sig,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid actor context signature"


@pytest.mark.parametrize(
    ("actor_id", "actor_roles", "actor_client", "actor_timestamp", "actor_signature"),
    [
        ("user-123", None, None, None, None),
        (None, "media:request", None, None, None),
        ("user-123", "media:request", "client-456", None, None),
        ("user-123", "media:request", "client-456", "1740000000", None),
    ],
)
def test_partial_actor_context_fails_closed(
    actor_id: str | None,
    actor_roles: str | None,
    actor_client: str | None,
    actor_timestamp: str | None,
    actor_signature: str | None,
) -> None:
    service = auth.ServiceAuthentication(kind="bff")
    with pytest.raises(HTTPException) as exc:
        auth.resolve_actor_context(
            service,
            actor_id=actor_id,
            actor_roles=actor_roles,
            actor_client=actor_client,
            actor_timestamp=actor_timestamp,
            actor_signature=actor_signature,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Incomplete actor context"


def test_authenticated_actor_without_roles_is_not_legacy_admin() -> None:
    actor = _actor("")

    assert actor.roles == frozenset()
    assert actor.is_legacy_api_key is False

    with pytest.raises(HTTPException) as exc:
        auth.require_role("media:request")(actor)

    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "route_name",
    [
        '"/pause",',
        '"/unpause",',
    ],
)
def test_pause_and_unpause_routes_require_playback_operator(route_name: str) -> None:
    """Assert both routes retain their guard without importing handler dependencies."""

    items_source = Path(__file__).parents[1].joinpath("routers", "secure", "items.py")
    source = items_source.read_text(encoding="utf-8")
    route_start = source.index(route_name)
    route_end = source.index("async def", route_start)

    assert 'dependencies=[Depends(require_role("playback:operator"))]' in source[
        route_start:route_end
    ]


def test_playback_operator_allows_pause_and_unpause_authorization() -> None:
    actor = _actor("playback:operator")

    assert auth.require_role("playback:operator")(actor) is actor


def test_regular_user_cannot_pause_or_unpause() -> None:
    with pytest.raises(HTTPException) as exc:
        auth.require_role("playback:operator")(_actor("media:request,library:read"))

    assert exc.value.status_code == 403


def test_invalid_service_api_key_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        auth.resolve_api_key(header=None, bearer=None)

    assert exc.value.status_code == 401
