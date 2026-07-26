"""Trakt OAuth refresh_token exchange and 401 one-shot retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from program.apis.trakt_api import TraktAPI
from program.settings.models import TraktModel, TraktOauthModel


def _oauth_settings(**token_overrides: str) -> TraktModel:
    oauth = TraktOauthModel(
        oauth_client_id="cid",
        oauth_client_secret="secret",
        oauth_redirect_uri="http://localhost:3000/api/trakt/oauth/callback",
        access_token=token_overrides.get("access_token", "old-access"),
        refresh_token=token_overrides.get("refresh_token", "old-refresh"),
    )
    return TraktModel(oauth=oauth)


def test_refresh_oauth_tokens_posts_json_and_soft_persists():
    api = TraktAPI(_oauth_settings())
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
    }
    api.session.post = MagicMock(return_value=mock_response)

    with patch("program.apis.trakt_api.settings_manager.save") as save:
        assert api.refresh_oauth_tokens() is True

    api.session.post.assert_called_once()
    _args, kwargs = api.session.post.call_args
    assert kwargs.get("json") == {
        "refresh_token": "old-refresh",
        "client_id": "cid",
        "client_secret": "secret",
        "redirect_uri": "http://localhost:3000/api/trakt/oauth/callback",
        "grant_type": "refresh_token",
    }
    assert "data" not in kwargs or kwargs.get("data") is None
    assert "Authorization" not in kwargs["headers"]
    assert api.settings.oauth.access_token == "new-access"
    assert api.settings.oauth.refresh_token == "new-refresh"
    assert api.headers["Authorization"] == "Bearer new-access"
    assert api.session.headers["Authorization"] == "Bearer new-access"
    save.assert_called_once()


def test_refresh_oauth_tokens_clears_on_failure():
    api = TraktAPI(_oauth_settings())
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 401
    mock_response.text = "invalid_grant"
    api.session.post = MagicMock(return_value=mock_response)

    with patch("program.apis.trakt_api.settings_manager.save"):
        assert api.refresh_oauth_tokens() is False

    assert api.settings.oauth.access_token == ""
    assert api.settings.oauth.refresh_token == ""
    assert "Authorization" not in api.headers
    assert "Authorization" not in api.session.headers


def test_refresh_oauth_tokens_clears_on_malformed_success_body():
    api = TraktAPI(_oauth_settings())
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {"access_token": "only-access"}
    mock_response.text = '{"access_token":"only-access"}'
    api.session.post = MagicMock(return_value=mock_response)

    with patch("program.apis.trakt_api.settings_manager.save"):
        assert api.refresh_oauth_tokens() is False

    assert api.settings.oauth.access_token == ""
    assert api.settings.oauth.refresh_token == ""


def test_fetch_data_refreshes_once_on_401_then_succeeds():
    from pydantic import BaseModel

    class _MovieRow(BaseModel):
        movie: dict[str, object]

    api = TraktAPI(_oauth_settings())

    class _Resp:
        def __init__(
            self,
            *,
            ok: bool,
            status_code: int,
            payload: object | None = None,
            headers: dict[str, str] | None = None,
        ):
            self.ok = ok
            self.status_code = status_code
            self._payload = payload
            self.headers = headers or {}

        def json(self):
            return self._payload

    unauthorized = _Resp(ok=False, status_code=401)
    ok = _Resp(
        ok=True,
        status_code=200,
        payload=[{"movie": {"title": "X", "ids": {"trakt": 1}}}],
        headers={
            "X-Pagination-Page": "1",
            "X-Pagination-Page-Count": "1",
        },
    )

    api.session.get = MagicMock(side_effect=[unauthorized, ok])
    api.refresh_oauth_tokens = MagicMock(return_value=True)

    results = api._fetch_data(
        "users/me/watchlist/movies",
        model_validator=lambda item: _MovieRow.model_validate(item),
    )

    assert len(results) == 1
    assert results[0].movie["title"] == "X"
    assert api.session.get.call_count == 2
    api.refresh_oauth_tokens.assert_called_once()


def test_fetch_data_does_not_loop_when_refresh_fails():
    api = TraktAPI(_oauth_settings())

    unauthorized = MagicMock()
    unauthorized.ok = False
    unauthorized.status_code = 401
    unauthorized.text = "expired"

    api.session.get = MagicMock(return_value=unauthorized)
    api.refresh_oauth_tokens = MagicMock(return_value=False)

    results = api._fetch_data(
        "users/me/watchlist/movies",
        model_validator=lambda item: item,
    )

    assert results == []
    assert api.session.get.call_count == 1
    api.refresh_oauth_tokens.assert_called_once()


def test_handle_oauth_callback_updates_bearer_headers():
    """Callback soft-persist must also refresh in-memory Bearer (same instance)."""
    settings = _oauth_settings(access_token="stale", refresh_token="")
    api = TraktAPI(settings)
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
    }
    api.session.post = MagicMock(return_value=mock_response)

    with patch("program.apis.trakt_api.settings_manager.save"):
        assert api.handle_oauth_callback("cid", "auth-code") is True

    assert api.headers["Authorization"] == "Bearer fresh-access"
    assert api.session.headers["Authorization"] == "Bearer fresh-access"
