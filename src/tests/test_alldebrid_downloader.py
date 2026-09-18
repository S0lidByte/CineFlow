"""Comprehensive unit test suite for AllDebrid v4.1 downloader, API contracts, error envelopes, rate limiting, PIN auth, and tree flattening."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from program.contracts.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from program.media.item import ProcessedItemType
from program.services.downloaders.alldebrid import (
    AllDebridAPI,
    AllDebridDirectory,
    AllDebridDownloader,
    AllDebridErrorCode,
    AllDebridErrorDetail,
    AllDebridErrorResponse,
    AllDebridFile,
    AllDebridMagnet,
    AllDebridMagnetFilesResponse,
    AllDebridMagnetStatusCode,
    AllDebridMagnetStatusResponse,
    AllDebridPinCheckResponse,
    AllDebridPinGetResponse,
    AllDebridResponse,
    AllDebridSuccessResponse,
    AllDebridUserResponse,
)
from program.services.downloaders.models import (
    DebridFile,
    TorrentInfo,
    UnrestrictedLink,
    UserInfo,
)
from program.services.downloaders.shared import DebridVpnBlockedError

# ============================================================================
# Schema & Contract Envelope Tests
# ============================================================================


def test_alldebrid_error_codes_enum() -> None:
    assert AllDebridErrorCode.AUTH_BAD_APIKEY.value == "AUTH_BAD_APIKEY"
    assert AllDebridErrorCode.RATE_LIMIT_EXCEEDED.value == "RATE_LIMIT_EXCEEDED"
    assert AllDebridErrorCode.PIN_EXPIRED.value == "PIN_EXPIRED"
    assert AllDebridErrorCode.PIN_INVALID.value == "PIN_INVALID"
    assert AllDebridErrorCode.MAGNET_NO_SERVER.value == "MAGNET_NO_SERVER"


def test_alldebrid_magnet_status_code_enum() -> None:
    assert AllDebridMagnetStatusCode.IN_QUEUE == 0
    assert AllDebridMagnetStatusCode.DOWNLOADING == 1
    assert AllDebridMagnetStatusCode.COMPRESSING == 2
    assert AllDebridMagnetStatusCode.UPLOADING == 3
    assert AllDebridMagnetStatusCode.READY == 4
    assert AllDebridMagnetStatusCode.UPLOAD_FAILED == 5
    assert AllDebridMagnetStatusCode.ERROR == 6
    assert AllDebridMagnetStatusCode.BAD_TORRENT == 7
    assert AllDebridMagnetStatusCode.NOT_FOUND == 8
    assert AllDebridMagnetStatusCode.DELETED == 9
    assert AllDebridMagnetStatusCode.SERVER_MAINTENANCE == 10
    assert AllDebridMagnetStatusCode.PAUSED == 11


def test_alldebrid_error_detail_tolerant_coercion() -> None:
    # 1. Plain string coercion
    detail1 = AllDebridErrorDetail.model_validate("AUTH_BAD_API_KEY")
    assert detail1.code == "AUTH_BAD_API_KEY"
    assert detail1.message == "AUTH_BAD_API_KEY"

    # 2. Dict with code and message
    detail2 = AllDebridErrorDetail.model_validate(
        {
            "code": "MUST_BE_PREMIUM",
            "message": "You must be premium to perform this action",
        }
    )
    assert detail2.code == "MUST_BE_PREMIUM"
    assert detail2.message == "You must be premium to perform this action"

    # 3. Dict with code only
    detail3 = AllDebridErrorDetail.model_validate({"code": "PIN_EXPIRED"})
    assert detail3.code == "PIN_EXPIRED"
    assert detail3.message == "PIN_EXPIRED"

    # 4. Dict with error subkey
    detail4 = AllDebridErrorDetail.model_validate(
        {"error": {"code": "RATE_LIMIT_EXCEEDED", "message": "Too many requests"}}
    )
    assert detail4.code == "RATE_LIMIT_EXCEEDED"
    assert detail4.message == "Too many requests"


def test_alldebrid_error_response_envelopes() -> None:
    # Top-level status="error" with dict error
    payload1 = {
        "status": "error",
        "error": {"code": "AUTH_BLOCKED", "message": "IP address is blocked"},
    }
    resp1 = AllDebridErrorResponse.model_validate(payload1)
    assert resp1.status == "error"
    assert resp1.error.code == "AUTH_BLOCKED"
    assert resp1.error.message == "IP address is blocked"

    # Top-level status="error" with string error
    payload2 = {"status": "error", "error": "FREE_TRIAL_LIMIT_REACHED"}
    resp2 = AllDebridErrorResponse.model_validate(payload2)
    assert resp2.status == "error"
    assert resp2.error.code == "FREE_TRIAL_LIMIT_REACHED"

    # Code and message directly in root
    payload3 = {
        "status": "error",
        "code": "MAGNET_INVALID_ID",
        "message": "Invalid magnet ID",
    }
    resp3 = AllDebridErrorResponse.model_validate(payload3)
    assert resp3.status == "error"
    assert resp3.error.code == "MAGNET_INVALID_ID"


def test_alldebrid_magnet_status_response_single_vs_list_coercion() -> None:
    # Single magnet object under data.magnets
    single_payload = {
        "status": "success",
        "data": {
            "magnets": {
                "id": 12345,
                "filename": "Ubuntu.24.04.iso",
                "size": 1048576000,
                "status": "Ready",
                "statusCode": 4,
                "downloaded": 1048576000,
                "uploaded": 0,
                "seeders": 42,
                "downloadSpeed": 0,
                "uploadSpeed": 0,
                "uploadDate": 1700000000,
                "completionDate": 1700000100,
                "links": ["https://cdn.alldebrid.com/dl/12345/ubuntu.iso"],
                "files": [
                    {
                        "n": "Ubuntu.24.04.iso",
                        "s": 1048576000,
                        "l": "https://cdn.alldebrid.com/dl/12345/ubuntu.iso",
                    }
                ],
            }
        },
    }
    model_single = AllDebridSuccessResponse[
        AllDebridMagnetStatusResponse
    ].model_validate(single_payload)
    assert len(model_single.data.magnets) == 1
    magnet = model_single.data.magnets[0]
    assert magnet.id == 12345
    assert isinstance(magnet, AllDebridMagnetStatusResponse.MagnetInfo)
    assert magnet.filename == "Ubuntu.24.04.iso"
    assert magnet.status_code == AllDebridMagnetStatusCode.READY

    # List of magnets under data.magnets
    list_payload = {
        "status": "success",
        "data": {
            "magnets": [
                {
                    "id": 12345,
                    "filename": "Ubuntu.24.04.iso",
                    "size": 1048576000,
                    "status": "Ready",
                    "statusCode": 4,
                },
                {
                    "id": 67890,
                    "filename": "Debian.12.iso",
                    "size": 2048576000,
                    "status": "Downloading",
                    "statusCode": 1,
                },
            ]
        },
    }
    model_list = AllDebridSuccessResponse[AllDebridMagnetStatusResponse].model_validate(
        list_payload
    )
    assert len(model_list.data.magnets) == 2
    assert model_list.data.magnets[0].id == 12345
    assert model_list.data.magnets[1].id == 67890
    assert isinstance(
        model_list.data.magnets[1], AllDebridMagnetStatusResponse.MagnetInfo
    )
    assert (
        model_list.data.magnets[1].status_code == AllDebridMagnetStatusCode.DOWNLOADING
    )


def test_alldebrid_magnet_files_response_coercion() -> None:
    # Single magnet object under data.magnets
    single_files_payload = {
        "status": "success",
        "data": {
            "magnets": {
                "id": 999,
                "files": [
                    {
                        "n": "Season 1",
                        "e": [
                            {
                                "n": "S01E01.mkv",
                                "s": 500000000,
                                "l": "https://cdn.example/s01e01.mkv",
                            },
                            {
                                "n": "S01E02.mkv",
                                "s": 500000000,
                                "l": "https://cdn.example/s01e02.mkv",
                            },
                        ],
                    }
                ],
            }
        },
    }
    resp = AllDebridSuccessResponse[AllDebridMagnetFilesResponse].model_validate(
        single_files_payload
    )
    assert len(resp.data.magnets) == 1
    assert resp.data.magnets[0].id == 999
    assert isinstance(resp.data.magnets[0], AllDebridMagnetFilesResponse.MagnetFiles)
    assert len(resp.data.magnets[0].files) == 1
    assert resp.data.magnets[0].files[0].n == "Season 1"
    assert len(resp.data.magnets[0].files[0].e or []) == 2


def test_alldebrid_pin_models() -> None:
    pin_get_payload = {
        "status": "success",
        "data": {
            "pin": "ABCD",
            "check": "1234567890abcdef",
            "expires_in": 300,
            "user_url": "https://alldebrid.com/pin",
            "base_url": "https://alldebrid.com/pin?pin=ABCD",
            "check_url": "https://api.alldebrid.com/v4/pin/check",
        },
    }
    pin_get = AllDebridSuccessResponse[AllDebridPinGetResponse].model_validate(
        pin_get_payload
    )
    assert pin_get.data.pin == "ABCD"
    assert pin_get.data.check == "1234567890abcdef"
    assert pin_get.data.expires_in == 300

    pin_check_payload = {
        "status": "success",
        "data": {
            "apikey": "my_new_alldebrid_api_key",
            "activated": True,
            "expires_in": 280,
        },
    }
    pin_check = AllDebridSuccessResponse[AllDebridPinCheckResponse].model_validate(
        pin_check_payload
    )
    assert pin_check.data.apikey == "my_new_alldebrid_api_key"
    assert pin_check.data.activated is True


# ============================================================================
# Error Classification & Mapping Tests
# ============================================================================


def test_error_classification_mappings() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"

    # Auth errors
    auth_detail = AllDebridErrorDetail(code="AUTH_BAD_APIKEY", message="Bad API key")
    auth_err = downloader._error_from_detail(auth_detail)
    assert isinstance(auth_err, ProviderAuthError)
    assert auth_err.raw_error == {"code": "AUTH_BAD_APIKEY", "message": "Bad API key"}

    banned_detail = AllDebridErrorDetail(code="AUTH_USER_BANNED", message="User banned")
    assert isinstance(downloader._error_from_detail(banned_detail), ProviderAuthError)

    pin_exp_detail = AllDebridErrorDetail(code="PIN_EXPIRED", message="PIN expired")
    assert isinstance(downloader._error_from_detail(pin_exp_detail), ProviderAuthError)

    # Quota / Premium errors
    prem_detail = AllDebridErrorDetail(
        code="MAGNET_MUST_BE_PREMIUM", message="Must be premium"
    )
    assert isinstance(
        downloader._error_from_detail(prem_detail), ProviderQuotaExceededError
    )

    limit_detail = AllDebridErrorDetail(
        code="FREE_TRIAL_LIMIT_REACHED", message="Limit reached"
    )
    assert isinstance(
        downloader._error_from_detail(limit_detail), ProviderQuotaExceededError
    )

    # Rate limit errors
    rate_detail = AllDebridErrorDetail(
        code="RATE_LIMIT_EXCEEDED", message="Too many calls"
    )
    rate_err = downloader._error_from_detail(rate_detail)
    assert isinstance(rate_err, ProviderRateLimitError)

    # VPN block / No server
    no_server_detail = AllDebridErrorDetail(
        code="MAGNET_NO_SERVER", message="No server"
    )
    assert isinstance(
        downloader._error_from_detail(no_server_detail), DebridVpnBlockedError
    )

    # Unavailable / Maintenance
    maint_detail = AllDebridErrorDetail(code="MAINTENANCE", message="Under maintenance")
    assert isinstance(
        downloader._error_from_detail(maint_detail), ProviderUnavailableError
    )

    # Unknown fallback
    custom_detail = AllDebridErrorDetail(
        code="UNKNOWN_NEW_CODE", message="Something weird"
    )
    custom_err = downloader._error_from_detail(custom_detail)
    assert isinstance(custom_err, ProviderError)
    assert not isinstance(custom_err, ProviderAuthError)
    assert custom_err.raw_error == {
        "code": "UNKNOWN_NEW_CODE",
        "message": "Something weird",
    }


def test_error_from_response_parsing() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"

    # 1. Error payload in JSON
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.json.return_value = {
        "status": "error",
        "error": {"code": "AUTH_BAD_APIKEY", "message": "Invalid credentials"},
    }
    err = downloader._error_from_response(mock_resp)
    assert isinstance(err, ProviderAuthError)

    # 2. HTTP 429 with non-JSON or missing error body
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.json.side_effect = ValueError("Not JSON")
    mock_resp_429.headers = {"Retry-After": "10"}
    err_429 = downloader._error_from_response(mock_resp_429)
    assert isinstance(err_429, ProviderRateLimitError)
    assert err_429.retry_after == 10.0

    # 3. HTTP 503 Server Error
    mock_resp_503 = MagicMock()
    mock_resp_503.status_code = 503
    mock_resp_503.json.side_effect = ValueError("Not JSON")
    mock_resp_503.headers = {}
    err_503 = downloader._error_from_response(mock_resp_503)
    assert isinstance(err_503, ProviderUnavailableError)


# ============================================================================
# Rate Limiting & Header Backoff Tests
# ============================================================================


def test_downloader_maybe_backoff_headers() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"

    # Response with rate limit info and Retry-After
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {
        "X-RateLimit-Limit": "600",
        "X-RateLimit-Remaining": "5",
        "X-RateLimit-Reset": "2",
        "Retry-After": "3",
    }
    retry_after = downloader._extract_retry_after(resp)
    assert retry_after == 3.0

    with patch("program.services.downloaders.alldebrid.logger.warning") as mock_warn:
        downloader._maybe_backoff(resp)
        mock_warn.assert_called_once()
        assert "Retry-After=3.0s" in mock_warn.call_args[0][0]


def test_downloader_maybe_backoff_exhausted_remaining() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"

    # Response with 0 remaining and reset time
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {
        "X-RateLimit-Limit": "600",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "4",
    }
    with patch("program.services.downloaders.alldebrid.logger.warning") as mock_warn:
        downloader._maybe_backoff(resp)
        mock_warn.assert_called_once()
        assert "quota exhausted" in mock_warn.call_args[0][0]


# ============================================================================
# API & Downloader PIN Device Auth Tests
# ============================================================================


def test_alldebrid_api_get_pin() -> None:
    with patch(
        "program.services.downloaders.alldebrid.SmartSession"
    ) as mock_session_cls:
        mock_session = mock_session_cls.return_value
        resp = MagicMock()
        resp.ok = True
        resp.headers = {}
        resp.json.return_value = {
            "status": "success",
            "data": {
                "pin": "WXYZ",
                "check": "check_token_123",
                "expires_in": 300,
                "user_url": "https://alldebrid.com/pin",
                "base_url": "https://alldebrid.com/pin?pin=WXYZ",
                "check_url": "https://api.alldebrid.com/v4/pin/check",
            },
        }
        mock_session.get.return_value = resp

        api = AllDebridAPI(api_key="dummy_key")
        pin_data = api.get_pin()
        assert pin_data.pin == "WXYZ"
        assert pin_data.check == "check_token_123"
        assert pin_data.expires_in == 300
        mock_session.get.assert_called_once_with("v4/pin/get")


def test_alldebrid_api_check_pin_activated() -> None:
    with patch(
        "program.services.downloaders.alldebrid.SmartSession"
    ) as mock_session_cls:
        mock_session = mock_session_cls.return_value
        resp = MagicMock()
        resp.ok = True
        resp.headers = {}
        resp.json.return_value = {
            "status": "success",
            "data": {
                "apikey": "user_api_key_456",
                "activated": True,
                "expires_in": 250,
            },
        }
        mock_session.get.return_value = resp

        api = AllDebridAPI(api_key="dummy_key")
        check_data = api.check_pin(check="check_token_123", pin="WXYZ")
        assert check_data.apikey == "user_api_key_456"
        assert check_data.activated is True
        mock_session.get.assert_called_once_with(
            "v4/pin/check", params={"check": "check_token_123", "pin": "WXYZ"}
        )


def test_alldebrid_api_check_pin_expired_raises_auth_error() -> None:
    with patch(
        "program.services.downloaders.alldebrid.SmartSession"
    ) as mock_session_cls:
        mock_session = mock_session_cls.return_value
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 400
        resp.headers = {}
        resp.json.return_value = {
            "status": "error",
            "error": {"code": "PIN_EXPIRED", "message": "The PIN code expired"},
        }
        mock_session.get.return_value = resp

        api = AllDebridAPI(api_key="dummy_key")
        with pytest.raises(ProviderAuthError) as exc_info:
            api.check_pin(check="check_token_123", pin="WXYZ")
        assert exc_info.value.provider_code == "PIN_EXPIRED"


def test_downloader_pin_helpers_delegation() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.api = MagicMock()

    downloader.api.get_pin.return_value = AllDebridPinGetResponse(
        pin="1234",
        check="check1",
        expires_in=300,
        user_url="https://alldebrid.com/pin",
    )
    res = downloader.get_pin()
    assert res.pin == "1234"
    downloader.api.get_pin.assert_called_once()

    downloader.api.check_pin.return_value = AllDebridPinCheckResponse(
        apikey="new_key", activated=True, expires_in=200
    )
    res_check = downloader.check_pin("check1", "1234")
    assert res_check.apikey == "new_key"
    downloader.api.check_pin.assert_called_once_with(check="check1", pin="1234")


# ============================================================================
# Downloader Operational Lifecycle Tests
# ============================================================================


def test_alldebrid_add_torrent_success() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"
    downloader.api = MagicMock()
    downloader._maybe_backoff = MagicMock()

    resp = MagicMock()
    resp.ok = True
    resp.headers = {}
    resp.json.return_value = {
        "status": "success",
        "data": {
            "magnets": [
                {
                    "id": 555123,
                    "name": "Ubuntu.24.04.iso",
                    "hash": "3648baf850d5930510c1f172b534200ebb5496e6",
                    "size": 1048576000,
                    "ready": True,
                }
            ]
        },
    }
    downloader.api.session.post.return_value = resp

    torrent_id = downloader.add_torrent("3648baf850d5930510c1f172b534200ebb5496e6")
    assert torrent_id == 555123
    downloader.api.session.post.assert_called_once_with(
        "v4/magnet/upload",
        data={
            "magnets[]": "magnet:?xt=urn:btih:3648baf850d5930510c1f172b534200ebb5496e6"
        },
    )


def test_alldebrid_get_torrent_info_progress_calculation() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"
    downloader.api = MagicMock()
    downloader._maybe_backoff = MagicMock()

    # 1. Downloading state with partial bytes
    resp_downloading = MagicMock()
    resp_downloading.ok = True
    resp_downloading.headers = {}
    resp_downloading.json.return_value = {
        "status": "success",
        "data": {
            "magnets": {
                "id": 1001,
                "filename": "Large.Movie.mkv",
                "size": 10000,
                "status": "Downloading",
                "statusCode": 1,
                "downloaded": 5000,
                "downloadSpeed": 1024000,
                "seeders": 15,
            }
        },
    }
    downloader.api.session.post.return_value = resp_downloading
    info = downloader.get_torrent_info("1001")
    assert info.status == "Downloading"
    assert info.progress == 50.0
    assert info.bytes == 10000
    downloader.api.session.post.assert_called_with(
        "v4.1/magnet/status", data={"id": "1001"}
    )

    # 2. Ready state (statusCode == 4)
    resp_ready = MagicMock()
    resp_ready.ok = True
    resp_ready.headers = {}
    resp_ready.json.return_value = {
        "status": "success",
        "data": {
            "magnets": {
                "id": 1001,
                "filename": "Large.Movie.mkv",
                "size": 10000,
                "status": "Ready",
                "statusCode": 4,
                "downloaded": 10000,
            }
        },
    }
    downloader.api.session.post.return_value = resp_ready
    info_ready = downloader.get_torrent_info("1001")
    assert info_ready.status == "Ready"
    assert info_ready.progress == 100.0


def test_alldebrid_delete_torrent() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"
    downloader.api = MagicMock()
    downloader._maybe_backoff = MagicMock()

    resp = MagicMock()
    resp.ok = True
    resp.headers = {}
    resp.json.return_value = {
        "status": "success",
        "data": {"message": "Magnet deleted"},
    }
    downloader.api.session.post.return_value = resp

    downloader.delete_torrent("1001")
    downloader.api.session.post.assert_called_once_with(
        url="v4/magnet/delete", data={"id": "1001"}
    )


def test_alldebrid_unrestrict_link() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"
    downloader.api = MagicMock()
    downloader._maybe_backoff = MagicMock()

    resp = MagicMock()
    resp.ok = True
    resp.headers = {}
    resp.json.return_value = {
        "status": "success",
        "data": {
            "link": "https://direct.cdn.alldebrid.com/file123.mkv",
            "host": "uptobox",
            "filename": "file123.mkv",
            "filesize": 500000000,
        },
    }
    downloader.api.session.get.return_value = resp

    stream_link = downloader.unrestrict_link("https://alldebrid.com/service/link123")
    assert stream_link is not None
    assert stream_link.download == "https://direct.cdn.alldebrid.com/file123.mkv"
    assert stream_link.filename == "file123.mkv"
    assert stream_link.filesize == 500000000
    downloader.api.session.get.assert_called_once_with(
        "v4/link/unlock", params={"link": "https://alldebrid.com/service/link123"}
    )


def test_alldebrid_get_user_info() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"
    downloader.api = MagicMock()
    downloader._maybe_backoff = MagicMock()

    resp = MagicMock()
    resp.ok = True
    resp.headers = {}
    resp.json.return_value = {
        "status": "success",
        "data": {
            "user": {
                "username": "cineflow_user",
                "email": "user@example.com",
                "isPremium": True,
                "isSubscribed": False,
                "premiumUntil": 1800000000,
                "lang": "en",
                "preferedDomain": 1,
                "fidelity_points": 100,
            }
        },
    }
    downloader.api.session.get.return_value = resp

    user_info = downloader.get_user_info()
    assert user_info is not None
    assert user_info.username == "cineflow_user"
    assert user_info.premium_status == "premium"
    assert user_info.points == 100
    downloader.api.session.get.assert_called_once_with("v4/user")


def test_alldebrid_instant_availability_parsing() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"
    downloader.api = MagicMock()
    downloader._maybe_backoff = MagicMock()

    downloader.add_torrent = MagicMock(return_value=555123)
    downloader.get_torrent_info = MagicMock(
        return_value=TorrentInfo(
            id=555123,
            name="Movie.Title.2024.1080p.mkv",
            status="Ready",
            infohash="3648baf850d5930510c1f172b534200ebb5496e6",
            bytes=1048576000,
        )
    )
    downloader._get_magnet_files = MagicMock(
        return_value=[
            AllDebridFile(
                n="Movie.Title.2024.1080p.mkv",
                s=1048576000,
                l="https://direct.cdn.alldebrid.com/movie.mkv",
            )
        ]
    )

    avail = downloader.get_instant_availability(
        "3648baf850d5930510c1f172b534200ebb5496e6",
        "movie",
    )
    assert avail is not None
    assert avail.cached is True
    assert len(avail.files) == 1
    assert avail.files[0].filename == "Movie.Title.2024.1080p.mkv"
    assert avail.files[0].download_url == "https://direct.cdn.alldebrid.com/movie.mkv"


def test_alldebrid_flatten_nested_file_tree() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"

    leaf1 = AllDebridFile(
        n="Season 1/Episode 1.mkv", s=500000000, l="https://cdn/s01e01.mkv"
    )
    leaf2 = AllDebridFile(
        n="Season 1/Episode 2.mkv", s=500000000, l="https://cdn/s01e02.mkv"
    )
    leaf3 = AllDebridFile(
        n="Season 2/Episode 1.mkv", s=500000000, l="https://cdn/s02e01.mkv"
    )
    leaf_root = AllDebridFile(n="Readme.txt", s=1000, l="https://cdn/readme.txt")

    dir_s1 = AllDebridDirectory(n="Season 1", e=[leaf1, leaf2])
    dir_s2 = AllDebridDirectory(n="Season 2", e=[leaf3])

    tree: list[AllDebridFile | AllDebridDirectory] = [dir_s1, dir_s2, leaf_root]
    result: list[AllDebridFile] = []

    downloader._flatten_magnet_files(tree, result)
    assert len(result) == 4
    assert result == [leaf1, leaf2, leaf3, leaf_root]


def test_alldebrid_extract_files_recursive() -> None:
    downloader = AllDebridDownloader.__new__(AllDebridDownloader)
    downloader.key = "alldebrid"

    leaf1 = AllDebridFile(
        n="Movie.Part1.1080p.mkv", s=1500000000, l="https://cdn/part1.mkv"
    )
    leaf2 = AllDebridFile(
        n="Movie.Part2.1080p.mkv", s=1500000000, l="https://cdn/part2.mkv"
    )
    dir_entry = AllDebridDirectory(n="Movie.Folder", e=[leaf1, leaf2])

    extracted_files: list[DebridFile] = []
    downloader._extract_files_recursive(
        [dir_entry],
        "movie",
        extracted_files,
        "3648baf850d5930510c1f172b534200ebb5496e6",
    )
    assert len(extracted_files) == 2
    assert extracted_files[0].filename == "Movie.Part1.1080p.mkv"
    assert extracted_files[0].download_url == "https://cdn/part1.mkv"
    assert extracted_files[1].filename == "Movie.Part2.1080p.mkv"
    assert extracted_files[1].download_url == "https://cdn/part2.mkv"
