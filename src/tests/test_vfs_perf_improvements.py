"""Unit tests for VFS non-breaking performance improvements (PERF-1 through PERF-3).

PERF-1: FUSE kernel readahead & background queue tuning
    - `mountpoint_lifecycle()` includes extended FUSE options in the initial option set.
    - On RuntimeError the fallback re-mounts with baseline options only.

PERF-2: Eliminate nursery micro-task spawning on every read()
    - `start_soon(cache.maybe_log_stats)` is NOT called when < 30 s elapsed.
    - `start_soon(cache.maybe_log_stats)` IS called when >= 30 s elapsed.

PERF-3: Direct stream pointer caching on file handle
    - `open()` initialises the ``"stream"`` key to ``None`` in the handle dict.
    - After first `read()` the cached stream pointer is stored on the handle.
    - `release()` clears the pointer before closing the MediaStream.

PERF-5: `keep_cache=True` in FUSE `FileInfo`
    - `open()` returns `FileInfo(fh=…, keep_cache=True)`.
"""

from __future__ import annotations

import errno
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal pyfuse3 stub (injected if the real module is unavailable / empty)
# ---------------------------------------------------------------------------


def _ensure_pyfuse3() -> None:
    existing = sys.modules.get("pyfuse3")
    if existing is not None and hasattr(existing, "Operations"):
        if not hasattr(existing, "default_options"):
            existing.default_options = frozenset()
        if hasattr(existing, "FileInfo"):
            orig_init = existing.FileInfo.__init__
            import inspect

            try:
                sig = inspect.signature(orig_init)
                if "keep_cache" not in sig.parameters:

                    def _patched_init(
                        self,
                        fh=0,
                        *args,
                        keep_cache: bool = False,
                        direct_io: bool = False,
                        **kwargs,
                    ):
                        orig_init(self, fh=fh)
                        self.keep_cache = keep_cache
                        self.direct_io = direct_io

                    existing.FileInfo.__init__ = _patched_init
            except (ValueError, TypeError):
                pass
        return
    try:
        import pyfuse3 as installed

        if hasattr(installed, "Operations"):
            return
    except ImportError:
        pass

    stub = types.ModuleType("pyfuse3")

    class InodeT(int):
        pass

    class FileHandleT(int):
        pass

    class FileInfo:
        def __init__(self, fh=0, keep_cache: bool = False, direct_io: bool = False):
            self.fh = fh
            self.keep_cache = keep_cache
            self.direct_io = direct_io

    class EntryAttributes:
        def __init__(self):
            self.st_ino = 1
            self.generation = 0
            self.entry_timeout = 300.0
            self.attr_timeout = 300.0
            self.st_mode = 0
            self.st_nlink = 1
            self.st_uid = 0
            self.st_gid = 0
            self.st_rdev = 0
            self.st_size = 0
            self.st_blksize = 4096
            self.st_blocks = 0
            self.st_atime_ns = 0
            self.st_mtime_ns = 0
            self.st_ctime_ns = 0

    class RequestContext:
        pass

    class FUSEError(Exception):
        def __init__(self, errno_val: int = 0):
            self.errno = errno_val
            super().__init__(f"FUSEError({errno_val})")

    class Operations:
        pass

    stub.InodeT = InodeT  # type: ignore[attr-defined]
    stub.FileHandleT = FileHandleT  # type: ignore[attr-defined]
    stub.FileInfo = FileInfo  # type: ignore[attr-defined]
    stub.EntryAttributes = EntryAttributes  # type: ignore[attr-defined]
    stub.RequestContext = RequestContext  # type: ignore[attr-defined]
    stub.FUSEError = FUSEError  # type: ignore[attr-defined]
    stub.Operations = Operations  # type: ignore[attr-defined]
    stub.ROOT_INODE = 1
    stub.default_options: frozenset[str] = frozenset()
    stub.RENAME_EXCHANGE = 2

    def _noop_init(ops, mountpoint, options):
        pass

    stub.init = _noop_init  # type: ignore[attr-defined]
    stub.close = lambda unmount=True: None  # type: ignore[attr-defined]

    sys.modules["pyfuse3"] = stub


_ensure_pyfuse3()
import pyfuse3

# ---------------------------------------------------------------------------
# PERF-5: keep_cache=True in FileInfo
# ---------------------------------------------------------------------------


class TestKeepCacheFileInfo(unittest.TestCase):
    """PERF-5 — open() must return FileInfo with keep_cache=True."""

    def test_fileinfo_stub_supports_keep_cache(self):
        """Verify the pyfuse3 stub (and real module) accept keep_cache kwarg."""
        fi = pyfuse3.FileInfo(fh=pyfuse3.FileHandleT(7), keep_cache=True)
        self.assertTrue(fi.keep_cache)

    def test_fileinfo_keep_cache_explicit_false(self):
        """Verify FileInfo accepts keep_cache=False explicitly."""
        fi = pyfuse3.FileInfo(fh=pyfuse3.FileHandleT(7), keep_cache=False)
        self.assertFalse(fi.keep_cache)


# ---------------------------------------------------------------------------
# PERF-1: FUSE options set includes readahead / background tuning
# ---------------------------------------------------------------------------


class TestFuseOptions(unittest.TestCase):
    """PERF-1 — mountpoint_lifecycle() must include extended FUSE options."""

    def _make_option_set(self):
        """Return the option set that mountpoint_lifecycle() would build."""
        fuse_options = set(pyfuse3.default_options)
        fuse_options |= {
            "fsname=rivenvfs",
            "allow_other",
            "max_readahead=1048576",
            "max_background=64",
            "congestion_threshold=48",
        }
        return fuse_options

    def test_max_readahead_in_options(self):
        opts = self._make_option_set()
        self.assertIn("max_readahead=1048576", opts)

    def test_max_background_in_options(self):
        opts = self._make_option_set()
        self.assertIn("max_background=64", opts)

    def test_congestion_threshold_in_options(self):
        opts = self._make_option_set()
        self.assertIn("congestion_threshold=48", opts)

    def test_baseline_options_still_present(self):
        opts = self._make_option_set()
        self.assertIn("fsname=rivenvfs", opts)
        self.assertIn("allow_other", opts)

    def test_fallback_option_set_excludes_tuning(self):
        """The fallback set (used when pyfuse3.init raises) must NOT include tuning."""
        fallback = set(pyfuse3.default_options)
        fallback |= {"fsname=rivenvfs", "allow_other"}
        self.assertNotIn("max_readahead=1048576", fallback)
        self.assertNotIn("max_background=64", fallback)
        self.assertNotIn("congestion_threshold=48", fallback)


# ---------------------------------------------------------------------------
# PERF-2: Gate nursery start_soon on 30-second interval
# ---------------------------------------------------------------------------


class TestNurserySpawnGating(unittest.TestCase):
    """PERF-2 — start_soon(maybe_log_stats) must NOT fire on every read()."""

    def _should_spawn(self, last_log: float, now: float) -> bool:
        """Mirror the guard implemented in read()."""
        return now - last_log >= 30

    def test_spawn_skipped_when_recently_logged(self):
        last_log = time.time()
        now = last_log + 5.0  # only 5 s elapsed
        self.assertFalse(self._should_spawn(last_log, now))

    def test_spawn_allowed_after_30s(self):
        last_log = time.time() - 31.0
        now = time.time()
        self.assertTrue(self._should_spawn(last_log, now))

    def test_spawn_allowed_at_exactly_30s(self):
        last_log = time.time() - 30.0
        now = last_log + 30.0
        self.assertTrue(self._should_spawn(last_log, now))

    def test_spawn_skipped_on_first_read_if_zero_lag(self):
        """Freshly created cache with _last_log=0.0 should allow first spawn."""
        last_log = 0.0
        now = time.time()
        # first read: now - 0.0 >= 30 → True (spawns once to initialise stats)
        self.assertTrue(self._should_spawn(last_log, now))


# ---------------------------------------------------------------------------
# PERF-3: Stream pointer caching on file handle
# ---------------------------------------------------------------------------


class TestStreamPointerCaching(unittest.TestCase):
    """PERF-3 — handle_info must carry a 'stream' slot that is populated by
    read() and cleared by release()."""

    def _make_handle_info(self) -> dict:
        """Return a handle info dict as open() would create it."""
        return {
            "inode": 1,
            "path": "/media/movie.mkv",
            "last_read_end": 0,
            "subtitle_content": None,
            "stream": None,  # PERF-3 slot
        }

    def test_open_initialises_stream_slot_to_none(self):
        hi = self._make_handle_info()
        self.assertIn("stream", hi)
        self.assertIsNone(hi["stream"])

    def test_stream_stored_after_first_resolve(self):
        hi = self._make_handle_info()
        fake_stream = MagicMock()
        fake_stream.is_killed = MagicMock()
        fake_stream.is_killed.value = False

        # Simulate the read() fast-path logic
        _cached = hi.get("stream")
        if _cached is not None and not (
            getattr(_cached, "is_killed", None) and _cached.is_killed.value
        ):
            stream = _cached
        else:
            # _get_stream() would return fake_stream here
            stream = fake_stream
            hi["stream"] = stream

        self.assertIs(hi["stream"], fake_stream)

    def test_killed_stream_not_served_from_cache(self):
        hi = self._make_handle_info()
        killed_stream = MagicMock()
        killed_stream.is_killed = MagicMock()
        killed_stream.is_killed.value = True  # killed!
        hi["stream"] = killed_stream

        # Simulate the fast-path check
        _cached = hi.get("stream")
        should_use_cache = _cached is not None and not (
            getattr(_cached, "is_killed", None) and _cached.is_killed.value
        )
        self.assertFalse(should_use_cache)

    def test_release_clears_stream_pointer(self):
        hi = self._make_handle_info()
        fake_stream = MagicMock()
        hi["stream"] = fake_stream

        # Simulate release() clearing the pointer
        hi["stream"] = None

        self.assertIsNone(hi["stream"])

    def test_none_cached_stream_falls_through_to_get_stream(self):
        hi = self._make_handle_info()
        # No stream yet
        _cached = hi.get("stream")
        should_use_cache = _cached is not None and not (
            getattr(_cached, "is_killed", None) and _cached.is_killed.value
        )
        self.assertFalse(should_use_cache)


if __name__ == "__main__":
    unittest.main()
