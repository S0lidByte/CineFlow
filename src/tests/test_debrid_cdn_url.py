from unittest.mock import MagicMock, patch

import httpx
import pytest

from program.services.streaming.exceptions import DebridServiceLinkUnavailable
from program.utils.debrid_cdn_url import DebridCDNUrl, RefreshedURLIdenticalException


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


def test_cdn_hosts_equivalent_same_url_and_same_host():
    dead = "https://109-4.download.real-debrid.com/d/DEAD/file.mkv"
    same_host = "https://109-4.download.real-debrid.com/d/NEWTOKEN/file.mkv"
    other = "https://45.download.real-debrid.com/d/LIVE/file.mkv"

    assert DebridCDNUrl._cdn_hosts_equivalent(dead, dead) is True
    assert DebridCDNUrl._cdn_hosts_equivalent(dead, same_host) is True
    assert DebridCDNUrl._cdn_hosts_equivalent(dead, other) is False
    assert DebridCDNUrl._cdn_hosts_equivalent(None, dead) is False


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


def test_validate_identical_refresh_after_connect_error_raises_link_unavailable():
    """ConnectError + identical refresh must surface LinkUnavailable (VFS re-scrape)."""
    dead = "https://109-4.download.real-debrid.com/d/DEAD/file.mkv"
    cdn = _cdn_with_url(dead)

    stream_cm = MagicMock()
    stream_cm.__enter__.side_effect = httpx.ConnectError("Name does not resolve")
    stream_cm.__exit__.return_value = None

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with (
        patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client),
        patch.object(
            cdn,
            "_refresh_with_cooldown",
            side_effect=RefreshedURLIdenticalException,
        ),
    ):
        with pytest.raises(DebridServiceLinkUnavailable):
            cdn.validate()


def test_refresh_identical_host_schedules_rescrape():
    """Same NXDOMAIN host with a new path must mark dead + re-scrape, not loop."""
    dead = "https://109-4.download.real-debrid.com/d/DEAD/file.mkv"
    same_host = "https://109-4.download.real-debrid.com/d/NEWTOKEN/file.mkv"
    cdn = _cdn_with_url(dead)

    vfs_db = MagicMock()
    vfs_db.refresh_unrestricted_url.return_value = same_host
    vfs_db.schedule_dead_link_rescrape.return_value = True

    session = MagicMock()
    session.merge.return_value = cdn.entry
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = None

    with (
        patch("program.utils.debrid_cdn_url.db_session", return_value=session_cm),
        patch("program.utils.debrid_cdn_url.di") as mock_di,
    ):
        mock_di.__getitem__.return_value = vfs_db
        with pytest.raises(RefreshedURLIdenticalException):
            cdn._refresh()

    vfs_db.schedule_dead_link_rescrape.assert_called_once_with(
        entry=cdn.entry,
        session=session,
    )


def test_refresh_different_host_returns_new_url():
    dead = "https://109-4.download.real-debrid.com/d/DEAD/file.mkv"
    live = "https://45.download.real-debrid.com/d/LIVE/file.mkv"
    cdn = _cdn_with_url(dead)

    vfs_db = MagicMock()
    vfs_db.refresh_unrestricted_url.return_value = live

    session = MagicMock()
    session.merge.return_value = cdn.entry
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = None

    with (
        patch("program.utils.debrid_cdn_url.db_session", return_value=session_cm),
        patch("program.utils.debrid_cdn_url.di") as mock_di,
    ):
        mock_di.__getitem__.return_value = vfs_db
        assert cdn._refresh() == live

    vfs_db.schedule_dead_link_rescrape.assert_not_called()
    assert cdn.url == live


def test_from_filename_missing_entry_raises_link_unavailable():
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = None

    with patch("program.utils.debrid_cdn_url.db_session", return_value=session_cm):
        with pytest.raises(DebridServiceLinkUnavailable) as exc_info:
            DebridCDNUrl.from_filename("missing_file.mkv")

        assert "missing_file.mkv" in str(exc_info.value)


@pytest.fixture(autouse=True)
def _reset_ghost_entry_counters():
    from program.utils.debrid_cdn_url import clear_persistent_validate_failure_state

    clear_persistent_validate_failure_state()
    yield
    clear_persistent_validate_failure_state()


def test_persistent_validate_none_schedules_rescrape_after_threshold():
    """Ghost entries: second soft validate failure schedules dead-link re-scrape."""
    from program.utils.debrid_cdn_url import PERSISTENT_VALIDATE_NONE_THRESHOLD

    cdn = _cdn_with_url("")
    cdn.url = None
    vfs_db = MagicMock()
    vfs_db.schedule_dead_link_rescrape.return_value = True

    session = MagicMock()
    session.merge.return_value = cdn.entry
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = None

    with (
        patch.object(cdn, "_refresh_with_cooldown", return_value=None),
        patch("program.utils.debrid_cdn_url.db_session", return_value=session_cm),
        patch("program.utils.debrid_cdn_url.di") as mock_di,
    ):
        mock_di.__getitem__.return_value = vfs_db

        assert cdn.validate() is None
        vfs_db.schedule_dead_link_rescrape.assert_not_called()

        for _ in range(PERSISTENT_VALIDATE_NONE_THRESHOLD - 1):
            assert cdn.validate() is None

        vfs_db.schedule_dead_link_rescrape.assert_called_once_with(
            entry=cdn.entry,
            session=session,
        )


def test_persistent_validate_none_skipped_during_refresh_cooldown():
    cdn = _cdn_with_url("https://example.com/d/DEAD/file.mkv")
    cdn._set_refresh_cooldown(120.0)

    stream_cm = MagicMock()
    stream_cm.__enter__.side_effect = httpx.ConnectError("Name does not resolve")
    stream_cm.__exit__.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with (
        patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client),
        patch.object(cdn, "_refresh_with_cooldown", return_value=None),
        patch.object(cdn, "_maybe_schedule_after_validate_none") as schedule,
    ):
        assert cdn.validate() is None
        schedule.assert_not_called()


def test_successful_validate_clears_ghost_failure_count():
    live = "https://45.download.real-debrid.com/d/LIVE/file.mkv"
    cdn = _cdn_with_url(live)

    from program.utils import debrid_cdn_url as mod

    mod._validate_none_counts[cdn.filename] = 1

    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = MagicMock(
        **{"raise_for_status.return_value": None}
    )
    stream_cm.__exit__.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client):
        assert cdn.validate() == live

    assert cdn.filename not in mod._validate_none_counts


@pytest.fixture(autouse=True)
def _reset_ghost_entry_counters():
    from program.utils.debrid_cdn_url import clear_persistent_validate_failure_state

    clear_persistent_validate_failure_state()
    yield
    clear_persistent_validate_failure_state()


def test_persistent_validate_none_schedules_rescrape_after_threshold():
    """Ghost entries: second soft validate failure schedules dead-link re-scrape."""
    from program.utils.debrid_cdn_url import PERSISTENT_VALIDATE_NONE_THRESHOLD

    cdn = _cdn_with_url("")
    cdn.url = None
    vfs_db = MagicMock()
    vfs_db.schedule_dead_link_rescrape.return_value = True

    session = MagicMock()
    session.merge.return_value = cdn.entry
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = None

    with (
        patch.object(cdn, "_refresh_with_cooldown", return_value=None),
        patch("program.utils.debrid_cdn_url.db_session", return_value=session_cm),
        patch("program.utils.debrid_cdn_url.di") as mock_di,
    ):
        mock_di.__getitem__.return_value = vfs_db

        assert cdn.validate() is None
        vfs_db.schedule_dead_link_rescrape.assert_not_called()

        for _ in range(PERSISTENT_VALIDATE_NONE_THRESHOLD - 1):
            assert cdn.validate() is None

        vfs_db.schedule_dead_link_rescrape.assert_called_once_with(
            entry=cdn.entry,
            session=session,
        )


def test_persistent_validate_none_skipped_during_refresh_cooldown():
    cdn = _cdn_with_url("https://example.com/d/DEAD/file.mkv")
    cdn._set_refresh_cooldown(120.0)

    stream_cm = MagicMock()
    stream_cm.__enter__.side_effect = httpx.ConnectError("Name does not resolve")
    stream_cm.__exit__.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with (
        patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client),
        patch.object(cdn, "_refresh_with_cooldown", return_value=None),
        patch.object(cdn, "_maybe_schedule_after_validate_none") as schedule,
    ):
        # Cooldown path returns None before max-attempt scheduling
        assert cdn.validate() is None
        schedule.assert_not_called()


def test_successful_validate_clears_ghost_failure_count():
    live = "https://45.download.real-debrid.com/d/LIVE/file.mkv"
    cdn = _cdn_with_url(live)

    from program.utils import debrid_cdn_url as mod

    mod._validate_none_counts[cdn.filename] = 1

    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = MagicMock(
        **{"raise_for_status.return_value": None}
    )
    stream_cm.__exit__.return_value = None
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.stream.return_value = stream_cm

    with patch("program.utils.debrid_cdn_url.httpx.Client", return_value=client):
        assert cdn.validate() == live

    assert cdn.filename not in mod._validate_none_counts
