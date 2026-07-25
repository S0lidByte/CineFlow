"""Trakt Client ID resolution for trakt-api-key header."""

from __future__ import annotations

import os
from unittest.mock import patch

from program.apis.trakt_api import TraktAPI
from program.settings.models import TraktModel, TraktOauthModel


def test_resolve_client_id_prefers_settings_api_key():
    settings = TraktModel(
        api_key="settings-client-id",
        oauth=TraktOauthModel(oauth_client_id="oauth-client-id"),
    )
    with patch.dict(os.environ, {"TRAKT_API_CLIENT_ID": "env-client-id"}):
        assert TraktAPI.resolve_client_id(settings) == "settings-client-id"


def test_resolve_client_id_falls_back_to_oauth_then_env():
    settings = TraktModel(
        api_key="",
        oauth=TraktOauthModel(oauth_client_id="oauth-client-id"),
    )
    with patch.dict(os.environ, {"TRAKT_API_CLIENT_ID": "env-client-id"}):
        assert TraktAPI.resolve_client_id(settings) == "oauth-client-id"

    settings = TraktModel(api_key="", oauth=TraktOauthModel(oauth_client_id=""))
    with patch.dict(os.environ, {"TRAKT_API_CLIENT_ID": "env-client-id"}):
        assert TraktAPI.resolve_client_id(settings) == "env-client-id"


def test_trakt_api_uses_resolved_client_id_in_headers():
    settings = TraktModel(api_key="ui-client-id")
    with patch.dict(os.environ, {"TRAKT_API_CLIENT_ID": "env-client-id"}):
        api = TraktAPI(settings)
    assert api.headers["trakt-api-key"] == "ui-client-id"
    assert api.client_id == "ui-client-id"


def test_oauth_authorize_url_uses_website_host():
    settings = TraktModel(
        oauth=TraktOauthModel(
            oauth_client_id="cid",
            oauth_client_secret="secret",
            oauth_redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        )
    )
    api = TraktAPI(settings)
    url = api.build_oauth_url()
    assert url.startswith("https://trakt.tv/oauth/authorize?")
    assert "client_id=cid" in url


def test_oauth_token_exchange_posts_json_body_not_form():
    """Trakt rejects form-urlencoded bodies advertised as application/json."""
    from unittest.mock import MagicMock

    settings = TraktModel(
        api_key="mismatched-api-key",
        oauth=TraktOauthModel(
            oauth_client_id="cid",
            oauth_client_secret="secret",
            oauth_redirect_uri="http://localhost:3000/api/trakt/oauth/callback",
            access_token="stale-bearer",
        ),
    )
    api = TraktAPI(settings)
    api.headers["Authorization"] = "Bearer stale-bearer"

    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
    }
    api.session.post = MagicMock(return_value=mock_response)

    with patch("program.apis.trakt_api.settings_manager.save"):
        assert api.handle_oauth_callback("ignored-api-key", "auth-code") is True

    api.session.post.assert_called_once()
    _args, kwargs = api.session.post.call_args
    assert kwargs.get("json") == {
        "code": "auth-code",
        "client_id": "cid",
        "client_secret": "secret",
        "redirect_uri": "http://localhost:3000/api/trakt/oauth/callback",
        "grant_type": "authorization_code",
    }
    assert "data" not in kwargs or kwargs.get("data") is None
    assert kwargs["headers"]["trakt-api-key"] == "cid"
    assert "Authorization" not in kwargs["headers"]
    assert settings.oauth.access_token == "new-access"
    assert settings.oauth.refresh_token == "new-refresh"


def test_oauth_token_exchange_logs_failure_body():
    from unittest.mock import MagicMock

    settings = TraktModel(
        oauth=TraktOauthModel(
            oauth_client_id="cid",
            oauth_client_secret="secret",
            oauth_redirect_uri="http://localhost:3000/api/trakt/oauth/callback",
        ),
    )
    api = TraktAPI(settings)
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 412
    mock_response.text = "use application/json content type"
    api.session.post = MagicMock(return_value=mock_response)

    assert api.handle_oauth_callback("cid", "auth-code") is False
