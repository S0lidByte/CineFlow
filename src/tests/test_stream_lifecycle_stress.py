"""Aggressive High-Concurrency Stress & Endurance Test Suite for Stream Lifecycle Fencing.

Stress tests the D74 stream lifetime and FUSE handle lifecycle fencing under:
1. Massive concurrent `_get_stream` calls on same (path, fh) (serialization & single-creation).
2. Dead-stream concurrent replacement race (atomic replacement without double-creation).
3. Heavy churn: 200 streams racing between monitor timeouts, rapid releases, and reads.
4. D74 playback cache lease protection under extreme LRU eviction pressure.
5. Concurrent multi-threaded `release(fh)` calls (idempotency & race safety).
6. Simulated 300-cycle Plex Direct Play endurance with read-ahead buffer gap churn.
"""

from __future__ import annotations

import errno
import sys
import types
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

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.streaming.cache import Cache, CacheConfig


class StressMockStream:
    """Mock MediaStream tracking close calls and concurrency state."""

    def __init__(
        self,
        fh: int = 6,
        path: str = "/movies/Starship.Troopers.1997.mkv",
        is_timed_out: bool = False,
        is_killed: bool = False,
    ):
        self.fh = fh
        self.path = path
        self.stream_id = f"{path}:{fh}"
        self.is_timed_out = is_timed_out
        self.is_killed = MagicMock(value=is_killed)
        self.is_streaming = MagicMock(value=True)
        self.created_at = 100.0
        self.session_statistics = MagicMock(bytes_transferred=50 * 1024 * 1024)
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1
        self.is_streaming.value = False
        self.is_killed.value = True
        if Cache in di:
            di[Cache].release_stream(self.stream_id)


@pytest.fixture
def mock_vfs():
    """Create an isolated RivenVFS instance with thread-safe data structures."""
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


def test_stress_concurrent_get_stream_serialization(mock_vfs):
    """Stress Test 1: 100 concurrent coroutines calling _get_stream on empty slot."""

    async def _run():
        path = "/movies/The.Matrix.1999.mkv"
        fh = 42
        mock_vfs._file_handles[fh] = {"path": path}

        mock_vfs.vfs_db.get_entry_by_original_filename.return_value = MagicMock(
            url="https://debrid.example/matrix",
            provider="realdebrid",
            bitrate=45_000_000,
        )

        creation_counter = 0

        def stream_factory(*args, **kwargs):
            nonlocal creation_counter
            creation_counter += 1
            return StressMockStream(fh=fh, path=path)

        with patch(
            "program.services.filesystem.vfs.rivenvfs.MediaStream",
            side_effect=stream_factory,
        ):
            results = []

            async def _caller():
                s = await mock_vfs._get_stream(
                    path=path,
                    fh=fh,
                    file_size=50 * 1024 * 1024 * 1024,
                    original_filename="The.Matrix.1999.mkv",
                )
                results.append(s)

            async with trio.open_nursery() as nursery:
                for _ in range(100):
                    nursery.start_soon(_caller)

            # Assert: Exactly 1 MediaStream created despite 100 concurrent racers!
            assert creation_counter == 1
            assert len(results) == 100
            first_stream = results[0]
            assert all(s is first_stream for s in results)
            stream_key = mock_vfs._stream_key(path, fh)
            assert mock_vfs._active_streams[stream_key] is first_stream

    trio.run(_run)


def test_stress_concurrent_dead_stream_replacement(mock_vfs):
    """Stress Test 2: 50 concurrent coroutines detect dead stream and replace it atomically."""

    async def _run():
        path = "/movies/Interstellar.2014.mkv"
        fh = 88
        mock_vfs._file_handles[fh] = {"path": path}

        dead_stream = StressMockStream(fh=fh, path=path, is_killed=True)
        stream_key = mock_vfs._stream_key(path, fh)
        mock_vfs._active_streams[stream_key] = dead_stream

        mock_vfs.vfs_db.get_entry_by_original_filename.return_value = MagicMock(
            url="https://debrid.example/interstellar",
            provider="realdebrid",
            bitrate=80_000_000,
        )

        replacement_counter = 0

        def stream_factory(*args, **kwargs):
            nonlocal replacement_counter
            replacement_counter += 1
            return StressMockStream(fh=fh, path=path, is_killed=False)

        with patch(
            "program.services.filesystem.vfs.rivenvfs.MediaStream",
            side_effect=stream_factory,
        ):
            results = []

            async def _caller():
                s = await mock_vfs._get_stream(
                    path=path,
                    fh=fh,
                    file_size=80 * 1024 * 1024 * 1024,
                    original_filename="Interstellar.2014.mkv",
                )
                results.append(s)

            async with trio.open_nursery() as nursery:
                for _ in range(50):
                    nursery.start_soon(_caller)

            # Assert: Dead stream replaced exactly ONCE
            assert replacement_counter == 1
            assert len(results) == 50
            fresh_stream = results[0]
            assert fresh_stream is not dead_stream
            assert all(s is fresh_stream for s in results)
            assert mock_vfs._active_streams[stream_key] is fresh_stream

    trio.run(_run)


def test_stress_monitor_stream_timeouts_under_heavy_churn(mock_vfs):
    """Stress Test 3: 200 streams (100 active open handles, 100 orphaned) under timeout monitor."""

    async def _run():
        active_streams = []
        orphan_streams = []

        # Populate 100 active streams with open handles
        for i in range(1, 101):
            p = f"/movies/active_{i}.mkv"
            s = StressMockStream(fh=i, path=p, is_timed_out=True)
            k = mock_vfs._stream_key(p, i)
            mock_vfs._active_streams[k] = s
            mock_vfs._file_handles[i] = {"path": p}
            active_streams.append(s)

        # Populate 100 orphaned streams (handles NOT in _file_handles)
        for i in range(101, 201):
            p = f"/movies/orphan_{i}.mkv"
            s = StressMockStream(fh=i, path=p, is_timed_out=True)
            k = mock_vfs._stream_key(p, i)
            mock_vfs._active_streams[k] = s
            orphan_streams.append(s)

        # Run monitor loop under nursery for 10 rapid iterations
        async def run_monitor():
            with trio.fail_after(0.2):
                await mock_vfs._monitor_stream_timeouts()

        try:
            await run_monitor()
        except trio.TooSlowError:
            pass

        # All 100 orphaned streams MUST be closed
        assert all(s.close_count == 1 for s in orphan_streams)
        for s in orphan_streams:
            k = mock_vfs._stream_key(s.path, s.fh)
            assert k not in mock_vfs._active_streams

        # All 100 active streams with open handles MUST be preserved!
        assert all(s.close_count == 0 for s in active_streams)
        for s in active_streams:
            k = mock_vfs._stream_key(s.path, s.fh)
            assert k in mock_vfs._active_streams

        # Now randomly release 30 of the active handles concurrently
        async with trio.open_nursery() as nursery:
            for i in range(1, 31):
                nursery.start_soon(mock_vfs.release, i)

        # The 30 released must now be closed and removed
        for i in range(1, 31):
            s = active_streams[i - 1]
            k = mock_vfs._stream_key(s.path, s.fh)
            assert s.close_count == 1
            assert k not in mock_vfs._active_streams
            assert i not in mock_vfs._file_handles

        # The remaining 70 must STILL be alive
        for i in range(31, 101):
            s = active_streams[i - 1]
            k = mock_vfs._stream_key(s.path, s.fh)
            assert s.close_count == 0
            assert k in mock_vfs._active_streams
            assert i in mock_vfs._file_handles

    trio.run(_run)


def test_stress_cache_lease_protection_under_eviction_pressure(mock_vfs, tmp_path):
    """Stress Test 4: D74 Cache lease protection under severe LRU eviction pressure."""

    async def _run():
        # Set small cache limit: 50 MB
        cache = Cache(
            CacheConfig(
                cache_dir=tmp_path / "stress_cache", max_size_bytes=50 * 1024 * 1024
            )
        )
        di[Cache] = cache

        # Create 10 active streams on open handles
        streams = []
        for i in range(1, 11):
            p = f"/movies/remux_{i}.mkv"
            s = StressMockStream(fh=i, path=p)
            mock_vfs._active_streams[mock_vfs._stream_key(p, i)] = s
            mock_vfs._file_handles[i] = {"path": p}
            streams.append(s)

            # Store a 4MB chunk with active lease for this stream
            chunk_data = b"L" * (4 * 1024 * 1024)
            await cache.put(
                cache_key=f"leased_chunk_{i}",
                start=0,
                data=chunk_data,
                stream_id=s.stream_id,
            )

        # Total leased data = 40 MB (out of 50 MB limit)
        assert len(cache._leases_by_stream) == 10

        # Now spam 30 MB of unleased background chunks (total attempting to reach 70 MB)
        for j in range(1, 8):
            unleased_data = b"U" * (4 * 1024 * 1024)
            await cache.put(
                cache_key=f"unleased_chunk_{j}",
                start=0,
                data=unleased_data,
                stream_id=None,  # No lease!
            )

        # Invariant: ALL 10 leased chunks MUST survive eviction!
        for i in range(1, 11):
            data = await cache.get(f"leased_chunk_{i}", 0, 4 * 1024 * 1024 - 1)
            assert data == chunk_data

        # Release 5 streams
        for i in range(1, 6):
            await mock_vfs.release(i)
            assert streams[i - 1].stream_id not in cache._leases_by_stream

        # Remaining 5 streams still have their active leases protected
        for i in range(6, 11):
            assert streams[i - 1].stream_id in cache._leases_by_stream

    trio.run(_run)


def test_stress_concurrent_release_idempotency(mock_vfs):
    """Stress Test 5: Concurrent multi-caller release(fh) on identical handles."""

    async def _run():
        path = "/movies/BladeRunner.2049.mkv"
        fh = 99
        stream = StressMockStream(fh=fh, path=path)
        stream_key = mock_vfs._stream_key(path, fh)
        mock_vfs._active_streams[stream_key] = stream
        mock_vfs._file_handles[fh] = {"path": path}

        # 20 concurrent coroutines call release(fh=99)
        async with trio.open_nursery() as nursery:
            for _ in range(20):
                nursery.start_soon(mock_vfs.release, fh)

        # Assert: Cleanly closed, no exception, handle popped
        assert fh not in mock_vfs._file_handles
        assert stream_key not in mock_vfs._active_streams
        # Stream close was called
        assert stream.close_count >= 1

    trio.run(_run)


def test_stress_massive_plex_buffer_gap_endurance(mock_vfs):
    """Stress Test 6: 300 cycles of read-ahead buffer gap churn across 20 concurrent streams."""

    async def _run():
        # Setup 20 active streams representing concurrent 4K Direct Play sessions
        streams = {}
        for fh in range(1, 21):
            path = f"/movies/remux_session_{fh}.mkv"
            s = StressMockStream(fh=fh, path=path, is_timed_out=False)
            streams[fh] = s
            mock_vfs._active_streams[mock_vfs._stream_key(path, fh)] = s
            mock_vfs._file_handles[fh] = {"path": path}

        cold_recreations = 0

        # Simulate 300 cycles of Plex reading, pausing (buffer gap), monitoring, and resuming
        for _cycle in range(300):
            # 1. Half of clients enter buffer gap (is_timed_out = True)
            for fh in range(1, 11):
                streams[fh].is_timed_out = True

            # 2. Run monitor check
            async def run_monitor():
                with trio.fail_after(0.01):
                    await mock_vfs._monitor_stream_timeouts()

            try:
                await run_monitor()
            except trio.TooSlowError:
                pass

            # 3. All 20 clients issue reads
            for fh in range(1, 21):
                path = f"/movies/remux_session_{fh}.mkv"
                retrieved = await mock_vfs._get_stream(
                    path=path,
                    fh=fh,
                    file_size=60 * 1024 * 1024 * 1024,
                    original_filename=f"remux_session_{fh}.mkv",
                )
                if retrieved is not streams[fh]:
                    cold_recreations += 1

            # 4. Reset timeouts
            for fh in range(1, 11):
                streams[fh].is_timed_out = False

        # Invariant: Exactly ZERO cold re-creations across all 300 cycles!
        assert cold_recreations == 0
        assert all(s.close_count == 0 for s in streams.values())
        assert len(mock_vfs._active_streams) == 20

    trio.run(_run)
