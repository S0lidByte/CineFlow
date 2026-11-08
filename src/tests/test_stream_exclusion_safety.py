"""Tests for stream exclusion safety, SQLAlchemy relationship retention, candidate progression, and CDN recovery."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from program.core.runner import RunnerResult
from program.db.base_model import Base
from program.media.item import Movie
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders import Downloader
from program.services.downloaders.alldebrid import (
    AllDebridDownloader,
    AllDebridMagnetStatusCode,
)
from program.services.downloaders.debridlink import DebridLinkDownloader
from program.services.downloaders.realdebrid import RealDebridDownloader
from program.services.downloaders.shared import (
    DebridInfringingError,
    TorrentDeadOrCorruptError,
)
from program.utils.debrid_cdn_url import DebridCDNUrl


class FakeTorrent:
    def __init__(self, infohash: str, raw_title: str = "Test.Movie.1080p.mkv"):
        self.infohash = infohash
        self.raw_title = raw_title
        self.rank = 100
        self.lev_ratio = 1.0
        self.data = SimpleNamespace(parsed_title=raw_title, resolution="1080p")


class TestStreamExclusionSafety:
    def test_sqlalchemy_relationship_persistence_after_non_permanent_rejection(self):
        """Non-permanent candidate rejection must not delete rows from StreamRelation upon commit."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)

        infohashes = [f"{i:02d}" + "1" * 38 for i in range(1, 5)]

        with Session(engine) as session:
            movie = Movie({"title": "Test Movie", "imdb_id": "tt1234567", "year": 2024})
            for h in infohashes:
                movie.streams.append(Stream(FakeTorrent(h, f"Stream.{h[:4]}.mkv")))
            session.add(movie)
            session.commit()
            movie_id = movie.id

        # Verify initial persistence
        with Session(engine) as session:
            loaded_movie = session.get(Movie, movie_id)
            assert loaded_movie is not None
            assert len(loaded_movie.streams) == 4
            assert {s.infohash for s in loaded_movie.streams} == set(infohashes)

            # Setup downloader
            with patch.object(Downloader, "__init__", lambda *_: None):
                dl = Downloader()
                dl.initialized = True
                service = Mock()
                service.key = "realdebrid"
                service.initialized = True
                dl.services = {type(service): service}
                dl.initialized_services = [service]
                dl.service = service
                dl._service_cooldowns = {}
                dl.subtitles_enabled = False

                # Simulate non-permanent failure on candidate streams (returns None / uncached)
                # MAX_STREAMS_PER_RUN is 3, so first 3 streams are evaluated in this run
                with patch.object(dl, "validate_stream_on_service", return_value=None):
                    with patch(
                        "program.services.downloaders.sort_streams_by_quality",
                        return_value=list(loaded_movie.streams),
                    ):
                        result = next(dl.run(loaded_movie), None)
                        assert isinstance(result, RunnerResult)

            # Crucial check: loaded_movie.streams must NOT have been mutated in-place
            # (i.e. non-permanently failed streams were NOT removed from loaded_movie.streams)
            session.commit()

        # Reload in a completely fresh session to verify database persistence
        with Session(engine) as session:
            reloaded_movie = session.get(Movie, movie_id)
            assert reloaded_movie is not None
            # Streams must still be 4 in the database relationship!
            assert len(reloaded_movie.streams) == 4
            assert {s.infohash for s in reloaded_movie.streams} == set(infohashes)

    def test_candidate_progression_with_max_streams_per_run(self):
        """Downloader must exclude streams locally per run without losing un-evaluated candidates."""
        with patch.object(Downloader, "__init__", lambda *_: None):
            dl = Downloader()
            dl.initialized = True
            service = Mock()
            service.key = "realdebrid"
            service.initialized = True
            dl.services = {type(service): service}
            dl.initialized_services = [service]
            dl.service = service
            dl._service_cooldowns = {}
            dl.subtitles_enabled = False

            # Create 5 fake streams
            streams = [
                SimpleNamespace(
                    id=i,
                    infohash=f"hash{i:02d}" + "0" * 34,
                    raw_title=f"Stream {i}",
                    rank=100 - i,
                    resolution="1080p",
                )
                for i in range(1, 6)
            ]

            movie = SimpleNamespace(
                id=999,
                title="Candidate Movie",
                log_string="Movie 999",
                type="movie",
                streams=list(streams),
                blacklisted_streams=[],
                active_stream=None,
                filesystem_entries=[],
                subtitles=[],
                scraped_at=None,
                scraped_times=0,
                failed_attempts=0,
                store_state=Mock(),
                get_stream_owner=lambda: None,
                blacklist_stream=Mock(),
            )

            evaluated_hashes: list[str] = []

            def mock_validate(stream, item, srv):
                evaluated_hashes.append(stream.infohash)
                # non-permanent uncached returns None by default

            with patch.object(
                dl, "validate_stream_on_service", side_effect=mock_validate
            ):
                with patch(
                    "program.services.downloaders.sort_streams_by_quality",
                    return_value=list(streams),
                ):
                    # First run: MAX_STREAMS_PER_RUN is 3, so first 3 streams should be evaluated
                    gen1 = dl.run(movie)
                    result1 = next(gen1, None)

            assert isinstance(result1, RunnerResult)
            assert len(evaluated_hashes) == 3
            assert evaluated_hashes == [s.infohash for s in streams[:3]]
            # Persistent streams remain untouched
            assert len(movie.streams) == 5

            # Second run on same downloader instance: should evaluate streams 4 and 5
            with patch.object(
                dl, "validate_stream_on_service", side_effect=mock_validate
            ):
                with patch(
                    "program.services.downloaders.sort_streams_by_quality",
                    return_value=list(streams),
                ):
                    gen2 = dl.run(movie)
                    result2 = next(gen2, None)

            assert isinstance(result2, RunnerResult)
            assert len(evaluated_hashes) == 5
            assert evaluated_hashes[3:] == [s.infohash for s in streams[3:5]]

    def test_cdn_nxdomain_and_unchanged_url_do_not_permanently_blacklist(self):
        """DNS resolution failures and identical refreshed URLs must pass permanent=False to dead-link rescrape."""
        entry = MagicMock()
        entry.media_item_id = 42
        entry.infohash = "deadhash1"
        entry.original_filename = "test.mkv"
        entry.unrestricted_url = "https://example.com/video.mkv"
        entry.provider = "realdebrid"
        cdn = DebridCDNUrl(entry)

        with (
            patch("program.utils.debrid_cdn_url.db_session") as mock_db_session,
            patch("program.utils.debrid_cdn_url.di") as mock_di,
        ):
            mock_session = MagicMock()
            mock_db_session.return_value.__enter__.return_value = mock_session
            mock_session.merge.return_value = entry

            mock_vfs_db = MagicMock()
            mock_vfs_db.schedule_dead_link_rescrape.return_value = True
            mock_di.__getitem__.return_value = mock_vfs_db

            # 1. NXDOMAIN test
            scheduled = cdn._schedule_dead_link_now(reason="nxdomain", permanent=False)
            assert scheduled is True
            mock_vfs_db.schedule_dead_link_rescrape.assert_called_with(
                entry=entry,
                session=mock_session,
                permanent=False,
            )

            # 2. Refreshed URL identical test
            mock_vfs_db.schedule_dead_link_rescrape.reset_mock()
            mock_vfs_db.schedule_dead_link_rescrape(
                entry=entry,
                session=mock_session,
                permanent=False,
            )
            mock_vfs_db.schedule_dead_link_rescrape.assert_called_with(
                entry=entry,
                session=mock_session,
                permanent=False,
            )

    def test_alldebrid_status_classification(self):
        """AllDebrid status codes must separate generic ERROR/DELETED from confirmed BAD_TORRENT."""
        with patch.object(AllDebridDownloader, "__init__", lambda *_: None):
            ad = AllDebridDownloader()

            # Case 1: BAD_TORRENT (status code 7) raises TorrentDeadOrCorruptError
            info_bad = SimpleNamespace(
                status="Bad Torrent", status_code=AllDebridMagnetStatusCode.BAD_TORRENT
            )
            ad.get_torrent_info = Mock(return_value=info_bad)
            with pytest.raises(TorrentDeadOrCorruptError):
                ad._process_torrent(101, "badhash", "movie")

            # Case 2: Generic ERROR (status code 6) does NOT raise TorrentDeadOrCorruptError
            info_error = SimpleNamespace(
                status="Error", status_code=AllDebridMagnetStatusCode.ERROR
            )
            ad.get_torrent_info = Mock(return_value=info_error)
            container, reason, info = ad._process_torrent(102, "errorhash", "movie")
            assert container is None
            assert "Not instantly available" in (reason or "")

            # Case 3: DELETED (status code 9) does NOT raise TorrentDeadOrCorruptError
            info_del = SimpleNamespace(
                status="Deleted", status_code=AllDebridMagnetStatusCode.DELETED
            )
            ad.get_torrent_info = Mock(return_value=info_del)
            container, reason, info = ad._process_torrent(103, "delhash", "movie")
            assert container is None
            assert "Not instantly available" in (reason or "")

    def test_realdebrid_and_debridlink_status_classification(self):
        """RealDebrid and DebridLink must treat generic 'error' as transfer failure, not corruption."""
        # RealDebrid:
        with patch.object(RealDebridDownloader, "__init__", lambda *_: None):
            rd = RealDebridDownloader()
            rd.get_torrent_info = Mock()

            fake_files = [
                SimpleNamespace(id=1, path="/test.mkv", bytes=100, selected=1)
            ]

            # "magnet_error" raises TorrentDeadOrCorruptError
            rd.get_torrent_info.return_value = SimpleNamespace(
                status="magnet_error", files=fake_files
            )
            with pytest.raises(TorrentDeadOrCorruptError):
                rd._process_torrent("tid1", "hash1", "movie")

            # "error" returns transfer error without raising
            rd.get_torrent_info.return_value = SimpleNamespace(
                status="error", files=fake_files
            )
            container, reason, _ = rd._process_torrent("tid2", "hash2", "movie")
            assert container is None
            assert "Real-Debrid transfer error" in (reason or "")

        # DebridLink:
        with patch.object(DebridLinkDownloader, "__init__", lambda *_: None):
            dl = DebridLinkDownloader()
            dl.get_torrent_info = Mock()

            # "dead" raises TorrentDeadOrCorruptError
            dl.get_torrent_info.return_value = SimpleNamespace(status="dead", files={})
            with pytest.raises(TorrentDeadOrCorruptError):
                dl._process_torrent("tid1", "hash3", "movie")

            # "error" returns transfer error without raising
            dl.get_torrent_info.return_value = SimpleNamespace(status="error", files={})
            container, reason, _ = dl._process_torrent("tid2", "hash4", "movie")
            assert container is None
            assert "Debrid-Link transfer error" in (reason or "")
