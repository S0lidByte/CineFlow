"""Plex → Trakt history POST (P2): mapping, idempotency, webhook sync path."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from program.utils.plex_webhook import (
    build_trakt_history_payload,
    history_idempotency_key,
    parse_provider_ids,
    plex_media_kind,
    sanitize_plex_guids,
)
from routers.secure import webhooks as webhooks_mod
from routers.secure.webhooks import verify_plex_webhook_secret


def _request_with(
    *,
    headers: dict[str, str] | None = None,
    query_string: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/webhook/plex",
        "raw_path": b"/api/v1/webhook/plex",
        "query_string": query_string,
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    return Request(scope)


def test_sanitize_plex_guids_keeps_provider_ids_only():
    metadata = {
        "type": "movie",
        "guid": "plex://movie/5d776b9eadsomeid",
        "Guid": [
            {"id": "imdb://tt0111161"},
            {"id": "tmdb://278"},
            {"id": "tvdb://123"},
            {"id": "plex://movie/ignored"},
            {"id": "IMDB://tt009"},
        ],
    }
    assert sanitize_plex_guids(metadata) == [
        "imdb://tt009",
        "imdb://tt0111161",
        "tmdb://278",
        "tvdb://123",
    ]


def test_parse_provider_ids_and_media_kind():
    assert parse_provider_ids(["imdb://tt1", "tmdb://42", "tvdb://9"]) == {
        "imdb": "tt1",
        "tmdb": 42,
        "tvdb": 9,
    }
    assert plex_media_kind({"type": "movie"}) == "movie"
    assert plex_media_kind({"type": "episode", "librarySectionType": "show"}) == (
        "episode"
    )
    assert plex_media_kind({"type": "show"}) == "unknown"


def test_build_trakt_history_payload_movie_drops_tvdb():
    metadata = {"type": "movie", "ratingKey": "1"}
    guids = ["imdb://tt0111161", "tmdb://278", "tvdb://999"]
    body = build_trakt_history_payload(
        metadata, guids, watched_at="2024-01-02T03:04:05.000Z"
    )
    assert body == {
        "movies": [
            {
                "watched_at": "2024-01-02T03:04:05.000Z",
                "ids": {"imdb": "tt0111161", "tmdb": 278},
            }
        ]
    }


def test_build_trakt_history_payload_episode():
    metadata = {"type": "episode", "ratingKey": "99", "parentIndex": 1, "index": 2}
    guids = ["tvdb://349232", "imdb://tt0959621"]
    body = build_trakt_history_payload(
        metadata, guids, watched_at="2024-01-02T03:04:05.000Z"
    )
    assert body == {
        "episodes": [
            {
                "watched_at": "2024-01-02T03:04:05.000Z",
                "ids": {"tvdb": 349232, "imdb": "tt0959621"},
            }
        ]
    }


def test_build_trakt_history_payload_rejects_unknown():
    assert build_trakt_history_payload({"type": "show"}, ["imdb://tt1"]) is None
    assert build_trakt_history_payload({"type": "movie"}, []) is None


def test_history_idempotency_key_stable():
    meta = {"type": "movie", "ratingKey": "42"}
    guids = ["imdb://tt1", "tmdb://2"]
    assert history_idempotency_key(
        media_kind="movie", guids=guids, metadata=meta
    ) == history_idempotency_key(media_kind="movie", guids=guids, metadata=meta)


def test_plex_webhook_secret_optional_when_unset(monkeypatch):
    monkeypatch.setattr(
        "routers.secure.webhooks.settings_manager",
        MagicMock(
            settings=MagicMock(
                content=MagicMock(plex_webhook=MagicMock(webhook_secret=""))
            )
        ),
    )
    verify_plex_webhook_secret(_request_with())


def test_plex_webhook_secret_accepts_header_or_query(monkeypatch):
    monkeypatch.setattr(
        "routers.secure.webhooks.settings_manager",
        MagicMock(
            settings=MagicMock(
                content=MagicMock(
                    plex_webhook=MagicMock(webhook_secret="plex-secret")
                )
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        verify_plex_webhook_secret(_request_with())
    assert exc.value.status_code == 401

    verify_plex_webhook_secret(
        _request_with(headers={"x-webhook-secret": "plex-secret"})
    )
    verify_plex_webhook_secret(
        _request_with(query_string=b"webhook_secret=plex-secret")
    )


def test_sync_plex_scrobble_success_and_idempotent():
    from kink import di

    from program.apis.trakt_api import TraktAPI

    webhooks_mod._HISTORY_IDEMPOTENCY.clear()
    trakt = MagicMock()
    trakt.oauth_connected.return_value = True
    trakt.add_items_to_watched_history.return_value = True
    previous = di[TraktAPI] if TraktAPI in di else None
    di[TraktAPI] = trakt
    try:
        metadata = {"type": "movie", "ratingKey": "7"}
        guids = ["imdb://tt0111161"]
        msg = webhooks_mod._sync_plex_scrobble_to_trakt(metadata=metadata, guids=guids)
        assert msg == "synced to Trakt"
        assert trakt.add_items_to_watched_history.call_count == 1

        msg2 = webhooks_mod._sync_plex_scrobble_to_trakt(metadata=metadata, guids=guids)
        assert msg2.startswith("idempotent")
        assert trakt.add_items_to_watched_history.call_count == 1
    finally:
        if previous is not None:
            di[TraktAPI] = previous
        else:
            del di[TraktAPI]


def test_sync_skips_when_oauth_disconnected():
    from kink import di

    from program.apis.trakt_api import TraktAPI

    webhooks_mod._HISTORY_IDEMPOTENCY.clear()
    trakt = MagicMock()
    trakt.oauth_connected.return_value = False
    previous = di[TraktAPI] if TraktAPI in di else None
    di[TraktAPI] = trakt
    try:
        msg = webhooks_mod._sync_plex_scrobble_to_trakt(
            metadata={"type": "movie", "ratingKey": "8"},
            guids=["imdb://tt1"],
        )
        assert "OAuth" in msg
        trakt.add_items_to_watched_history.assert_not_called()
    finally:
        if previous is not None:
            di[TraktAPI] = previous
        else:
            del di[TraktAPI]


def test_add_items_to_watched_history_posts_json(monkeypatch):
    from program.apis.trakt_api import TraktAPI
    from program.settings.models import TraktModel, TraktOauthModel

    settings = TraktModel(
        oauth=TraktOauthModel(
            oauth_client_id="cid",
            oauth_client_secret="sec",
            oauth_redirect_uri="http://localhost/cb",
            access_token="atok",
            refresh_token="rtok",
        )
    )
    api = TraktAPI(settings)
    api._last_history_post_at = 0.0

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 201
    post = MagicMock(return_value=mock_resp)
    monkeypatch.setattr(api.session, "post", post)

    ok = api.add_items_to_watched_history(
        {"movies": [{"ids": {"imdb": "tt1"}, "watched_at": "2024-01-01T00:00:00.000Z"}]}
    )
    assert ok is True
    assert post.call_count == 1
    args, kwargs = post.call_args
    assert args[0].endswith("/sync/history")
    assert "movies" in kwargs["json"]
