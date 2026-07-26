from unittest.mock import MagicMock, patch

import httpx

from program.utils.debrid_cdn_url import DebridCDNUrl


def _cdn_with_url(url: str) -> DebridCDNUrl:
    entry = MagicMock()
    entry.original_filename = "test.mkv"
    entry.unrestricted_url = url
    entry.provider = "realdebrid"
    return DebridCDNUrl(entry)


def test_sanitize_logged_url_redacts_sensitive_query_params():
    url = (
        "https://example.com/stream?apikey=shh"
        "&token=tok"
        "&access_token=at"
        "&refresh_token=rt"
        "&client_secret=cs"
        "&password=pwd"
        "&safe=ok"
    )

    sanitized = DebridCDNUrl._sanitize_logged_url(url)

    assert "apikey=shh" not in sanitized
    assert "token=tok" not in sanitized
    assert "access_token=at" not in sanitized
    assert "refresh_token=rt" not in sanitized
    assert "client_secret=cs" not in sanitized
    assert "password=pwd" not in sanitized
    assert "apikey=%5Bredacted%5D" in sanitized
    assert "token=%5Bredacted%5D" in sanitized
    assert "access_token=%5Bredacted%5D" in sanitized
    assert "refresh_token=%5Bredacted%5D" in sanitized
    assert "client_secret=%5Bredacted%5D" in sanitized
    assert "password=%5Bredacted%5D" in sanitized
    assert "safe=ok" in sanitized


def test_sanitize_logged_url_no_query():
    url = "https://example.com/stream/file"
    assert DebridCDNUrl._sanitize_logged_url(url) == url


def test_validate_refreshes_on_connect_error():
    """NXDOMAIN / ConnectError must refresh once — not retry the dead host forever."""
    dead = "https://109-4.download.real-debrid.com/d/DEAD/file.mkv"
    live = "https://45.download.real-debrid.com/d/LIVE/file.mkv"
    cdn = _cdn_with_url(dead)

    stream_cm = MagicMock()
    stream_cm.__enter__.side_effect = [
        httpx.ConnectError("Name does not resolve"),
        MagicMock(**{"raise_for_status.return_value": None}),
    ]
    stream_cm.__exit__.return_value = None

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with (
        patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client),
        patch.object(cdn, "_refresh_with_cooldown", return_value=live) as refresh,
    ):
        assert cdn.validate() == live
        refresh.assert_called_once()
        assert cdn.url == live


def test_validate_refreshes_on_timeout():
    dead = "https://109-4.download.real-debrid.com/d/DEAD/file.mkv"
    live = "https://45.download.real-debrid.com/d/LIVE/file.mkv"
    cdn = _cdn_with_url(dead)

    stream_cm = MagicMock()
    stream_cm.__enter__.side_effect = [
        httpx.TimeoutException("timed out"),
        MagicMock(**{"raise_for_status.return_value": None}),
    ]
    stream_cm.__exit__.return_value = None

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with (
        patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client),
        patch.object(cdn, "_refresh_with_cooldown", return_value=live) as refresh,
    ):
        assert cdn.validate() == live
        refresh.assert_called_once()
