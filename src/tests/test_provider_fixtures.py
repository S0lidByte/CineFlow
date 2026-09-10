"""Tests for normalized provider fixtures and error classification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from program.contracts.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNetworkError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    classify_http_status,
    normalize_provider_error,
)

FIXTURES_DIR = (Path(__file__).parent / "fixtures").resolve()

FIXTURE_MAP = {
    "realdebrid_fixtures.json": FIXTURES_DIR / "realdebrid_fixtures.json",
    "zilean_fixtures.json": FIXTURES_DIR / "zilean_fixtures.json",
    "stremthru_fixtures.json": FIXTURES_DIR / "stremthru_fixtures.json",
    "subdl_fixtures.json": FIXTURES_DIR / "subdl_fixtures.json",
    "prowlarr_fixtures.json": FIXTURES_DIR / "prowlarr_fixtures.json",
}


def load_fixture(filename: str) -> dict:
    if filename not in FIXTURE_MAP:
        raise ValueError(f"Disallowed fixture filename: {filename}")
    path = FIXTURE_MAP[filename]
    return json.loads(path.read_text(encoding="utf-8"))


class TestProviderFixtures:
    def test_fixtures_directory_exists_and_contains_expected_files(self):
        assert FIXTURES_DIR.is_dir()
        expected_files = [
            "realdebrid_fixtures.json",
            "zilean_fixtures.json",
            "stremthru_fixtures.json",
            "subdl_fixtures.json",
            "prowlarr_fixtures.json",
        ]
        for name in expected_files:
            file_path = FIXTURES_DIR / name
            assert file_path.is_file(), f"Missing fixture file: {name}"

    def test_realdebrid_fixture_contracts(self):
        data = load_fixture("realdebrid_fixtures.json")
        assert "user_profile_success" in data
        assert data["user_profile_success"]["type"] == "premium"
        assert "instant_availability_success" in data
        assert "error_bad_token" in data
        assert data["error_bad_token"]["error_code"] == 8

        # Test error normalization from RealDebrid error payload
        auth_err = normalize_provider_error(
            provider_name="RealDebrid",
            status_code=401,
            message=data["error_bad_token"]["message"],
            raw_payload=data["error_bad_token"],
        )
        assert isinstance(auth_err, ProviderAuthError)
        assert auth_err.status_code == 401
        assert "RealDebrid" in str(auth_err)

    def test_zilean_fixture_contracts(self):
        data = load_fixture("zilean_fixtures.json")
        assert len(data["search_success"]) > 0
        assert (
            data["search_success"][0]["info_hash"]
            == "0123456789abcdef0123456789abcdef01234567"
        )

        # Rate limit classification
        rate_err = normalize_provider_error(
            provider_name="Zilean",
            status_code=429,
            message=data["error_rate_limited"]["detail"],
            retry_after=60.0,
            raw_payload=data["error_rate_limited"],
        )
        assert isinstance(rate_err, ProviderRateLimitError)
        assert rate_err.retry_after_seconds == 60.0
        assert rate_err.is_transient is True

        # 500 error classification
        srv_err = normalize_provider_error(
            provider_name="Zilean",
            status_code=500,
            message=data["error_internal_server"]["detail"],
            raw_payload=data["error_internal_server"],
        )
        assert isinstance(srv_err, ProviderUnavailableError)
        assert srv_err.is_transient is True

    def test_stremthru_fixture_contracts(self):
        data = load_fixture("stremthru_fixtures.json")
        items = data["streams_success"]["data"]["items"]
        assert len(items) == 1
        assert "Real-Debrid" in items[0]["name"] or "RD+" in items[0]["name"]

        # Gateway error classification
        gw_err = normalize_provider_error(
            provider_name="StremThru",
            status_code=502,
            message=data["error_upstream_gateway"]["message"],
            raw_payload=data["error_upstream_gateway"],
        )
        assert isinstance(gw_err, ProviderUnavailableError)
        assert gw_err.status_code == 502

    def test_subdl_fixture_contracts(self):
        data = load_fixture("subdl_fixtures.json")
        assert data["search_success"]["status"] is True
        assert "subtitles" in data["search_success"]
        subtitles = data["search_success"]["subtitles"]
        assert len(subtitles) == 1
        assert subtitles[0]["lang"] == "en"
        assert subtitles[0]["release_name"] == "Example.Movie.2024.1080p.WEB-DL"

        # Auth error classification
        auth_err = normalize_provider_error(
            provider_name="SubDL",
            status_code=401,
            message=data["error_invalid_api_key"]["error"],
        )
        assert isinstance(auth_err, ProviderAuthError)

    def test_prowlarr_fixture_contracts(self):
        data = load_fixture("prowlarr_fixtures.json")
        assert len(data["search_success"]) == 1
        assert data["search_success"][0]["indexer"] == "TorrentGalaxy"


class TestErrorClassificationEdgeCases:
    def test_classify_http_status_codes(self):
        assert isinstance(classify_http_status(401, "unauthorized"), ProviderAuthError)
        assert isinstance(classify_http_status(403, "forbidden"), ProviderAuthError)
        assert isinstance(
            classify_http_status(429, "rate limited", retry_after=15.0),
            ProviderRateLimitError,
        )
        assert isinstance(
            classify_http_status(402, "payment required"), ProviderQuotaExceededError
        )
        assert isinstance(
            classify_http_status(500, "internal error"), ProviderUnavailableError
        )
        assert isinstance(
            classify_http_status(502, "bad gateway"), ProviderUnavailableError
        )
        assert isinstance(
            classify_http_status(503, "service unavailable"), ProviderUnavailableError
        )
        assert isinstance(
            classify_http_status(504, "gateway timeout"), ProviderUnavailableError
        )

        # Non-standard 404 maps to base ProviderError
        not_found = classify_http_status(404, "not found")
        assert isinstance(not_found, ProviderError)
        assert not isinstance(not_found, (ProviderAuthError, ProviderUnavailableError))

    def test_normalize_provider_error_with_exceptions(self):
        # Network timeout exception
        class FakeTimeoutError(Exception):
            pass

        timeout_exc = FakeTimeoutError("Connection timed out on port 443")
        err = normalize_provider_error(
            provider_name="DebridLink", exception=timeout_exc
        )
        assert isinstance(err, ProviderNetworkError)
        assert err.is_transient is True
        assert "DebridLink" in str(err)

        # Connection error exception
        class FakeConnectError(Exception):
            pass

        connect_exc = FakeConnectError(
            "Failed to establish a new connection: [Errno 111] Connection refused"
        )
        err2 = normalize_provider_error(
            provider_name="AllDebrid", exception=connect_exc
        )
        assert isinstance(err2, ProviderNetworkError)
        assert err2.is_transient is True

    def test_normalize_existing_provider_error_preserves_type(self):
        existing = ProviderRateLimitError(
            "Limit hit", provider_name="Zilean", retry_after_seconds=45
        )
        normalized = normalize_provider_error(
            provider_name="Zilean", exception=existing
        )
        assert normalized is existing
        assert normalized.retry_after_seconds == 45

    def test_normalize_http_response_with_retry_after_and_unread_stream(self):
        class FakeResponseWithUnreadStream:
            status_code = 429
            headers = {"retry-after": "120"}

            @property
            def text(self):
                raise RuntimeError("Response stream has not been read")

        class FakeHTTPStatusError(Exception):
            def __init__(self, message: str, response: Any):
                super().__init__(message)
                self.response = response

        http_err = FakeHTTPStatusError(
            "429 Client Error: Too Many Requests", FakeResponseWithUnreadStream()
        )
        normalized = normalize_provider_error(
            provider_name="RealDebrid", exception=http_err
        )
        assert isinstance(normalized, ProviderRateLimitError)
        assert normalized.status_code == 429
        assert normalized.retry_after_seconds == 120.0
        assert normalized.raw_error is None

    def test_all_5xx_status_codes_classify_as_unavailable(self):
        for code in (500, 501, 502, 503, 504, 521, 599):
            err = classify_http_status(
                code, "Server Error", provider_name="TestProvider"
            )
            assert isinstance(err, ProviderUnavailableError)
            assert err.is_transient is True
            assert err.status_code == code
