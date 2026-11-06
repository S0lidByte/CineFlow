"""Permanent debrid availability failures must avoid duplicate fallback probing."""

from unittest.mock import Mock, patch

import pytest

from program.media.item import ProcessedItemType
from program.services.downloaders.alldebrid import AllDebridDownloader
from program.services.downloaders.models import (
    DebridFile,
    TorrentContainer,
    TorrentFile,
    TorrentInfo,
)
from program.services.downloaders.realdebrid import RealDebridDownloader
from program.services.downloaders.shared import (
    DebridInfringingError,
    DebridVpnBlockedError,
)
from program.utils.request import CircuitBreakerOpen
from routers.secure.scrape import resolve_torrent_container


class FakeService:
    def __init__(self, key: str, availability_result=None):
        self.key = key
        self.availability_result = availability_result
        self.add_torrent = Mock()

    def get_instant_availability(self, _infohash: str, _item_type: ProcessedItemType):
        if isinstance(self.availability_result, Exception):
            raise self.availability_result
        return self.availability_result


class FakeDownloader:
    def __init__(self, services):
        self.initialized_services = services
        self.service = None


@pytest.fixture
def rd_downloader():
    with patch.object(RealDebridDownloader, "__init__", lambda *_: None):
        downloader = RealDebridDownloader()
        downloader.key = "realdebrid"
        downloader.api = Mock()
        downloader.api.session = Mock()
        downloader._fair_usage_until = 0.0
        downloader._fair_usage_warned = False
        return downloader


@pytest.fixture
def ad_downloader():
    with patch.object(AllDebridDownloader, "__init__", lambda *_: None):
        downloader = AllDebridDownloader()
        downloader.key = "alldebrid"
        downloader.api = Mock()
        downloader.api.session = Mock()
        return downloader


def test_realdebrid_451_add_torrent_is_infringing(rd_downloader):
    response = Mock(ok=False, status_code=451)
    response.json.return_value = {
        "error": "Torrent is infringing",
        "error_code": 35,
    }
    rd_downloader.api.session.post.return_value = response
    rd_downloader._maybe_backoff = Mock()

    with pytest.raises(DebridInfringingError):
        rd_downloader.add_torrent("deadbeef")


def test_alldebrid_no_server_add_torrent_is_vpn_blocked(ad_downloader):
    response = Mock(ok=False, status_code=400)
    response.json.return_value = {
        "status": "error",
        "error": {"code": "MAGNET_NO_SERVER", "message": "No server"},
    }
    ad_downloader.api.session.post.return_value = response
    ad_downloader._maybe_backoff = Mock()

    with pytest.raises(DebridVpnBlockedError):
        ad_downloader.add_torrent("deadbeef")


@pytest.mark.asyncio
async def test_infringing_error_skips_fallback_probe():
    service = FakeService("realdebrid", DebridInfringingError("blocked"))

    with pytest.raises(DebridInfringingError):
        await resolve_torrent_container("deadbeef", FakeDownloader([service]))

    service.add_torrent.assert_not_called()


@pytest.mark.asyncio
async def test_vpn_block_error_skips_fallback_probe():
    service = FakeService("alldebrid", DebridVpnBlockedError("no server"))

    with pytest.raises(DebridVpnBlockedError):
        await resolve_torrent_container("deadbeef", FakeDownloader([service]))

    service.add_torrent.assert_not_called()


@pytest.mark.asyncio
async def test_circuit_breaker_open_skips_fallback_and_uses_later_provider():
    open_service = FakeService(
        "realdebrid", CircuitBreakerOpen("api.real-debrid.com", 12.0)
    )
    later_service = FakeService("alldebrid")
    later_service.add_torrent.return_value = "torrent-id"
    later_service.get_torrent_info = Mock(
        return_value=TorrentInfo(
            id="torrent-id",
            name="Movie",
            files={
                1: TorrentFile(
                    id=1,
                    path="movie.mkv",
                    bytes=2_000_000,
                    selected=1,
                    download_url="https://files.example/movie.mkv",
                )
            },
        )
    )
    later_service.get_instant_availability = Mock(return_value=None)

    container, error, service = await resolve_torrent_container(
        "deadbeef",
        FakeDownloader([open_service, later_service]),
        min_filesize_override=0,
    )

    assert container is not None
    assert container.files[0].filename == "movie.mkv"
    assert error is None
    assert service is later_service
    open_service.add_torrent.assert_not_called()
    later_service.add_torrent.assert_called_once_with("deadbeef")


@pytest.mark.asyncio
async def test_normal_availability_miss_still_uses_fallback_probe():
    service = FakeService("realdebrid")
    service.add_torrent.side_effect = RuntimeError("fallback attempted")

    container, error, _ = await resolve_torrent_container(
        "deadbeef", FakeDownloader([service])
    )

    assert container is None
    assert "fallback attempted" in error
    service.add_torrent.assert_called_once_with("deadbeef")


@pytest.mark.asyncio
async def test_later_provider_success_wins_over_permanent_error():
    fallback_container = TorrentContainer(
        infohash="deadbeef",
        files=[DebridFile(file_id=1, filename="movie.mkv", filesize=2_000_000)],
    )
    blocked_service = FakeService("realdebrid", DebridInfringingError("blocked"))
    successful_service = FakeService("alldebrid", fallback_container)

    container, error, service = await resolve_torrent_container(
        "deadbeef", FakeDownloader([blocked_service, successful_service])
    )

    assert container == fallback_container
    assert error is None
    assert service is successful_service
    blocked_service.add_torrent.assert_not_called()
    successful_service.add_torrent.assert_not_called()


@pytest.mark.asyncio
async def test_infringing_error_has_priority_over_vpn_block():
    services = [
        FakeService("alldebrid", DebridVpnBlockedError("no server")),
        FakeService("realdebrid", DebridInfringingError("blocked")),
    ]

    with pytest.raises(DebridInfringingError):
        await resolve_torrent_container("deadbeef", FakeDownloader(services))

    for service in services:
        service.add_torrent.assert_not_called()
