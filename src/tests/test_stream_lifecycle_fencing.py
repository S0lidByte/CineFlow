"""D74 Stream Lifetime & FUSE Handle Lifecycle Surgical Remediation Tests.

Verifies the POSIX / FUSE invariant:
    FUSE handle lifetime > MediaStream lifetime > HTTP connection lifetime.

Stream objects and D74 cache leases must NEVER be destroyed during natural
media player (e.g. Plex Direct Play / ExoPlayer) read-ahead buffer gaps or playback pauses.
Destruction must strictly occur upon FUSE release(fh), VFS unmount, or process exit.
"""

from __future__ import annotations

import errno
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import trio
from kink import di


def _ensure_pyfuse3() -> None:
    """Install minimal pyfuse3 stubs when native pyfuse3 is unavailable."""
    existing = sys.modules.get("pyfuse3")
    if existing is not None and hasattr(existing, "Operations"):
        return
    try:
        import pyfuse3 as installed

        if hasattr(installed, "Operations"):
            return
    except ImportError:
        pass

    errno_mod = types.ModuleType("pyfuse3.errno")
    for name in ("ENOENT", "EIO", "EACCES", "EINVAL", "EPERM", "EBADF", "ENOTDIR"):
        setattr(errno_mod, name, getattr(errno, name, 2))

    stub = types.ModuleType("pyfuse3")

    class InodeT(int):
        pass

    class FileHandleT(int):
        pass

    class ModeT(int):
        pass

    class FileInfo:
        def __init__(self, fh=0):
            self.fh = fh

    class EntryAttributes:
        pass

    class FUSEError(OSError):
        def __init__(self, err: int):
            super().__init__(err, "fuse error")
            self.errno = err

    class RequestContext:
        pass

    stub.Operations = object
    stub.InodeT = InodeT
    stub.FileHandleT = FileHandleT
    stub.ModeT = ModeT
    stub.FileInfo = FileInfo
    stub.EntryAttributes = EntryAttributes
    stub.StatvfsData = type("StatvfsData", (), {})
    stub.FUSEError = FUSEError
    stub.RequestContext = RequestContext
    stub.ROOT_INODE = InodeT(1)
    stub.errno = errno_mod

    def _noop(*_args, **_kwargs):
        return None

    stub.readdir_reply = _noop
    stub.init = _noop
    stub.main = _noop
    stub.terminate = _noop
    stub.invalidate_inode = _noop

    sys.modules["pyfuse3"] = stub
    sys.modules["pyfuse3.errno"] = errno_mod


_ensure_pyfuse3()

import pyfuse3

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.streaming.cache import Cache, CacheConfig


class MockStream:
    """Mock MediaStream tracking close calls, timing out, and lifecycle state."""

    def __init__(
        self,
        fh: int = 6,
        path: str = "/movies/Starship.Troopers.1997.mkv",
        is_timed_out: bool = True,
        is_killed: bool = False,
    ):
        self.fh = fh
        self.path = path
        self.stream_id = f"{path}:{fh}"
        self.is_timed_out = is_timed_out
        self.is_killed = MagicMock(value=is_killed)
        self.is_streaming = MagicMock(value=True)
        self.created_at = 100.0
        self.session_statistics = MagicMock(bytes_transferred=30 * 1024 * 1024)
        self.close_called = False

    async def close(self) -> None:
        self.close_called = True
        self.is_streaming.value = False
        self.is_killed.value = True
        if Cache in di:
            di[Cache].release_stream(self.stream_id)


@pytest.fixture
def mock_vfs():
    """Create an isolated, unmounted RivenVFS instance."""
    with patch.object(RivenVFS, "__init__", lambda self: None):
        vfs = RivenVFS()
        vfs._tree_lock = MagicMock()
        vfs._tree_lock.__enter__ = MagicMock(return_value=None)
        vfs._tree_lock.__exit__ = MagicMock(return_value=None)
        vfs._active_streams_lock = trio.Lock()
        vfs._active_streams = {}
        vfs._active_stream_count = 0
        vfs._file_handles = {}
        vfs._inode_to_node = {}
        vfs.vfs_db = MagicMock()
        vfs.stream_nursery = MagicMock()
        vfs.http_pool = MagicMock()
        yield vfs


def test_monitor_stream_timeouts_preserves_stream_with_open_fuse_handle(mock_vfs):
    """Test A: Idle stream must NOT be closed while FUSE file handle is open."""

    async def _run():
        stream = MockStream(
            fh=6, path="/movies/Starship.Troopers.1997.mkv", is_timed_out=True
        )
        stream_key = mock_vfs._stream_key(stream.path, stream.fh)

        mock_vfs._active_streams[stream_key] = stream
        mock_vfs._file_handles[6] = {"path": stream.path, "inode": 42}

        # Run monitor loop for one single check
        async def run_monitor():
            with trio.fail_after(0.05):
                await mock_vfs._monitor_stream_timeouts()

        try:
            await run_monitor()
        except trio.TooSlowError:
            pass

        # Stream must be strictly preserved!
        assert stream_key in mock_vfs._active_streams
        assert stream.close_called is False
        assert mock_vfs._file_handles[6]["path"] == stream.path

    trio.run(_run)


def test_monitor_stream_timeouts_closes_orphaned_stream_when_handle_closed(mock_vfs):
    """Test B: Orphaned stream (handle not in _file_handles) IS closed on timeout."""

    async def _run():
        stream = MockStream(
            fh=6, path="/movies/Starship.Troopers.1997.mkv", is_timed_out=True
        )
        stream_key = mock_vfs._stream_key(stream.path, stream.fh)

        mock_vfs._active_streams[stream_key] = stream
        # Handle 6 is explicitly NOT in _file_handles

        async def run_monitor():
            with trio.fail_after(0.05):
                await mock_vfs._monitor_stream_timeouts()

        try:
            await run_monitor()
        except trio.TooSlowError:
            pass

        # Orphaned stream must be closed and removed!
        assert stream_key not in mock_vfs._active_streams
        assert stream.close_called is True

    trio.run(_run)


def test_release_closes_stream_and_releases_d74_cache_leases(mock_vfs, tmp_path):
    """Test C: FUSE release(fh) closes the stream and releases D74 cache leases."""

    async def _run():
        cache = Cache(
            CacheConfig(cache_dir=tmp_path / "cache", max_size_bytes=100 * 1024 * 1024)
        )
        di[Cache] = cache

        stream = MockStream(fh=6, path="/movies/Starship.Troopers.1997.mkv")
        stream_key = mock_vfs._stream_key(stream.path, stream.fh)

        # Cache a chunk with stream_id to acquire active lease
        chunk_data = b"X" * (8 * 1024 * 1024)
        await cache.put(
            cache_key="trooper_chunk_0",
            start=0,
            data=chunk_data,
            stream_id=stream.stream_id,
        )
        assert stream.stream_id in cache._leases_by_stream

        mock_vfs._active_streams[stream_key] = stream
        mock_vfs._file_handles[6] = {"path": stream.path, "inode": 42}

        # Client closes file descriptor -> FUSE release(6)
        await mock_vfs.release(6)

        assert 6 not in mock_vfs._file_handles
        assert stream_key not in mock_vfs._active_streams
        assert stream.close_called is True
        # D74 cache leases must be completely released on release(fh)
        assert stream.stream_id not in cache._leases_by_stream

    trio.run(_run)


def test_get_stream_reuses_active_stream_on_subsequent_reads(mock_vfs):
    """Test D: _get_stream reuses active stream for open handle (0ms cold penalty)."""

    async def _run():
        stream = MockStream(fh=6, path="/movies/Starship.Troopers.1997.mkv")
        stream_key = mock_vfs._stream_key(stream.path, stream.fh)
        mock_vfs._active_streams[stream_key] = stream
        mock_vfs._file_handles[6] = {"path": stream.path}

        retrieved = await mock_vfs._get_stream(
            path=stream.path,
            fh=stream.fh,
            file_size=62 * 1024 * 1024 * 1024,
            original_filename="Starship.Troopers.1997.mkv",
        )

        assert retrieved is stream
        # Database must not have been queried because existing stream was reused
        mock_vfs.vfs_db.get_entry_by_original_filename.assert_not_called()

    trio.run(_run)


def test_get_stream_replaces_dead_killed_stream(mock_vfs):
    """Test E: _get_stream replaces a dead/killed stream instead of returning a zombie."""

    async def _run():
        dead_stream = MockStream(
            fh=6, path="/movies/Starship.Troopers.1997.mkv", is_killed=True
        )
        stream_key = mock_vfs._stream_key(dead_stream.path, dead_stream.fh)
        mock_vfs._active_streams[stream_key] = dead_stream
        mock_vfs._file_handles[6] = {"path": dead_stream.path}

        mock_vfs.vfs_db.get_entry_by_original_filename.return_value = MagicMock(
            url="https://debrid.example/stream",
            provider="realdebrid",
            bitrate=65_000_000,
        )

        with patch(
            "program.services.filesystem.vfs.rivenvfs.MediaStream"
        ) as mock_ms_cls:
            fresh_stream = MockStream(fh=6, path=dead_stream.path, is_killed=False)
            mock_ms_cls.return_value = fresh_stream

            retrieved = await mock_vfs._get_stream(
                path=dead_stream.path,
                fh=dead_stream.fh,
                file_size=62 * 1024 * 1024 * 1024,
                original_filename="Starship.Troopers.1997.mkv",
            )

            assert retrieved is fresh_stream
            assert mock_vfs._active_streams[stream_key] is fresh_stream

    trio.run(_run)


def test_shed_stalled_streams_preserves_open_handles(mock_vfs):
    """Test F: _shed_stalled_streams never sheds streams with actively open FUSE handles."""

    async def _run():
        active_stream = MockStream(fh=6, path="/movies/active.mkv", is_timed_out=True)
        orphan_stream = MockStream(fh=7, path="/movies/orphan.mkv", is_timed_out=True)

        k_active = mock_vfs._stream_key(active_stream.path, active_stream.fh)
        k_orphan = mock_vfs._stream_key(orphan_stream.path, orphan_stream.fh)

        mock_vfs._active_streams[k_active] = active_stream
        mock_vfs._active_streams[k_orphan] = orphan_stream

        # Handle 6 is actively open, Handle 7 is orphaned
        mock_vfs._file_handles[6] = {"path": active_stream.path}

        await mock_vfs._shed_stalled_streams()

        # Active stream must survive pool heal
        assert k_active in mock_vfs._active_streams
        assert active_stream.close_called is False

        # Orphan stream must be shed
        assert k_orphan not in mock_vfs._active_streams
        assert orphan_stream.close_called is True

    trio.run(_run)


def test_plex_direct_play_69s_buffer_gap_lifecycle_simulation(mock_vfs, tmp_path):
    """Test G: Full simulated Plex Direct Play 69s read-ahead buffer gap scenario."""

    async def _run():
        cache = Cache(
            CacheConfig(cache_dir=tmp_path / "cache", max_size_bytes=200 * 1024 * 1024)
        )
        di[Cache] = cache

        # 1. Plex opens Starship Troopers -> fh=6
        path = "/movies/Starship.Troopers.1997.mkv"
        fh = 6
        mock_vfs._file_handles[fh] = {"path": path, "inode": 100}

        # 2. First read creates MediaStream
        stream = MockStream(fh=fh, path=path, is_timed_out=False)
        stream_key = mock_vfs._stream_key(path, fh)
        mock_vfs._active_streams[stream_key] = stream

        # 3. D74 Cache chunk stored with active lease for this stream
        chunk_data = b"S" * (8 * 1024 * 1024)
        await cache.put(
            cache_key="starship_chunk_0",
            start=0,
            data=chunk_data,
            stream_id=stream.stream_id,
        )
        assert stream.stream_id in cache._leases_by_stream

        # 4. Plex buffer fills; 69s idle gap occurs. stream.is_timed_out becomes True.
        stream.is_timed_out = True

        # 5. Monitor runs multiple iterations during the gap
        async def run_monitor():
            with trio.fail_after(0.05):
                await mock_vfs._monitor_stream_timeouts()

        try:
            await run_monitor()
        except trio.TooSlowError:
            pass

        # Invariant: Stream and D74 leases MUST be alive during the 69s gap
        assert stream_key in mock_vfs._active_streams
        assert stream.close_called is False
        assert stream.stream_id in cache._leases_by_stream

        # 6. Plex buffer drains at t=69s; Plex issues next read() on fh=6
        read_stream = await mock_vfs._get_stream(
            path=path,
            fh=fh,
            file_size=62 * 1024 * 1024 * 1024,
            original_filename="Starship.Troopers.1997.mkv",
        )
        # Reuses warm stream immediately; 0 connection overhead!
        assert read_stream is stream

        # 7. Playback ends; Plex closes file -> release(fh=6)
        await mock_vfs.release(fh)

        # Full deterministic teardown
        assert fh not in mock_vfs._file_handles
        assert stream_key not in mock_vfs._active_streams
        assert stream.close_called is True
        assert stream.stream_id not in cache._leases_by_stream

    trio.run(_run)
