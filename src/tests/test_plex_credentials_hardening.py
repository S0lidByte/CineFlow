"""Tests for Plex credential hardening and URL token sanitization."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from program.apis.plex_api import PlexAPI
from program.utils.url_sanitizer import (
    SENSITIVE_URL_QUERY_PARAMS,
    sanitize_url_for_logs,
)


def test_sensitive_query_params_includes_x_plex_token():
    assert "x-plex-token" in SENSITIVE_URL_QUERY_PARAMS


@pytest.mark.parametrize(
    ("url", "expected_sanitized"),
    [
        (
            "https://plex.example.com/library/metadata/12345?X-Plex-Token=secret_token_123&includeGuids=1",
            "https://plex.example.com/library/metadata/12345?X-Plex-Token=%5Bredacted%5D&includeGuids=1",
        ),
        (
            "https://plex.example.com/library/metadata/12345?x-plex-token=secret_token_123",
            "https://plex.example.com/library/metadata/12345?x-plex-token=%5Bredacted%5D",
        ),
        (
            "https://plex.example.com/library/metadata/12345?X-PLEX-TOKEN=secret_token_123&other=val",
            "https://plex.example.com/library/metadata/12345?X-PLEX-TOKEN=%5Bredacted%5D&other=val",
        ),
        (
            "http://user:mypassword@example.com/path?token=tok&apikey=key&safe=1",
            "http://user:[redacted]@example.com/path?token=%5Bredacted%5D&apikey=%5Bredacted%5D&safe=1",
        ),
    ],
)
def test_sanitize_url_for_logs_redacts_plex_token_variants(
    url: str, expected_sanitized: str
):
    sanitized = sanitize_url_for_logs(url)
    assert sanitized == expected_sanitized
    assert "secret_token_123" not in sanitized


def test_ratingkey_to_imdbid_sends_token_in_headers_and_not_in_url():
    plex = PlexAPI.__new__(PlexAPI)
    plex.BASE_URL = "http://plex.local:32400"
    plex.token = "plex_secret_token_xyz"
    mock_session = MagicMock()
    plex.session = mock_session

    fake_response = MagicMock()
    fake_response.ok = True
    fake_response.json.return_value = {
        "MediaContainer": {
            "Metadata": [
                {
                    "Guid": [
                        {"id": "imdb://tt1234567"},
                        {"id": "tmdb://999"},
                    ]
                }
            ]
        }
    }
    mock_session.get.return_value = fake_response

    imdb_id = plex.ratingkey_to_imdbid("42")

    assert imdb_id == "tt1234567"
    assert mock_session.get.call_count == 1
    call_args, call_kwargs = mock_session.get.call_args

    called_url = call_args[0]
    called_headers = call_kwargs.get("headers", {})

    # 1. Plex token is absent from URL
    assert "X-Plex-Token" not in called_url
    assert "plex_secret_token_xyz" not in called_url
    assert (
        called_url
        == "http://plex.local:32400/library/metadata/42?includeGuids=1&includeFields=guid,title,year&includeElements=Guid"
    )

    # 2. Token sent in headers
    assert called_headers.get("X-Plex-Token") == "plex_secret_token_xyz"
    assert called_headers.get("Accept") == "application/json"


def test_ratingkey_to_imdbid_returns_none_on_http_or_parse_failure():
    plex = PlexAPI.__new__(PlexAPI)
    plex.BASE_URL = "http://plex.local:32400"
    plex.token = "plex_secret_token_xyz"
    mock_session = MagicMock()
    plex.session = mock_session

    # Case 1: HTTP error
    fake_response_fail = MagicMock()
    fake_response_fail.ok = False
    mock_session.get.return_value = fake_response_fail
    assert plex.ratingkey_to_imdbid("42") is None

    # Case 2: Malformed JSON
    fake_response_bad_json = MagicMock()
    fake_response_bad_json.ok = True
    fake_response_bad_json.json.side_effect = ValueError("Invalid JSON")
    mock_session.get.return_value = fake_response_bad_json
    assert plex.ratingkey_to_imdbid("42") is None

    # Case 3: Empty metadata
    fake_response_empty = MagicMock()
    fake_response_empty.ok = True
    fake_response_empty.json.return_value = {"MediaContainer": {"Metadata": []}}
    mock_session.get.return_value = fake_response_empty
    assert plex.ratingkey_to_imdbid("42") is None
