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
