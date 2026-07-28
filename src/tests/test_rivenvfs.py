"""VFS characterization tests (mount-free; stubs pyfuse3 when unavailable)."""

from __future__ import annotations

import errno
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import trio


def _ensure_pyfuse3() -> None:
    """Install a minimal pyfuse3 stub when Operations is unavailable.

    Real pyfuse3 is Linux-only. Unit tests never mount FUSE; they only need
    helpers and the Operations base class used by RivenVFS.
    """
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
    stub.invalidate_entry_async = _noop
    stub.trio_token = None
    sys.modules["pyfuse3"] = stub
    sys.modules["pyfuse3.errno"] = errno_mod


_ensure_pyfuse3()

import pyfuse3

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSFile


@pytest.fixture
def mock_vfs(tmp_path):
    cache_dir = tmp_path / "riven-cache"
    cache_dir.mkdir()

    with (
        patch("pyfuse3.init"),
        patch("threading.Thread"),
        patch("kink.di"),
        patch(
            "program.services.filesystem.vfs.rivenvfs.settings_manager"
        ) as mock_settings,
        patch.object(RivenVFS, "sync", return_value=None),
    ):
        mock_settings.settings.filesystem.cache_dir = cache_dir
        mock_settings.settings.filesystem.cache_max_size_mb = 100
        mock_settings.settings.filesystem.cache_ttl_seconds = 3600
        mock_settings.settings.filesystem.cache_eviction = "LRU"
        mock_settings.settings.filesystem.cache_metrics = False
        mock_settings.settings.filesystem.library_profiles = {}

        vfs = RivenVFS(mountpoint=str(tmp_path / "mock_mtpt"), downloader=MagicMock())
        vfs.vfs_db = MagicMock()
        vfs.vfs_db.get_subtitle_content = MagicMock(return_value=b"subtitle data")
        # FUSE thread is mocked, so the Trio lock never gets created in _fuse_runner.
        vfs._active_streams_lock = MagicMock()
        vfs._active_streams_lock.__aenter__ = AsyncMock(return_value=None)
        vfs._active_streams_lock.__aexit__ = AsyncMock(return_value=None)
        yield vfs


def test_subtitle_caching(mock_vfs):
    """Reading a subtitle file multiple times only queries the DB once."""

    async def _run() -> None:
        fh = pyfuse3.FileHandleT(1)
        mock_vfs._file_handles[fh] = {
            "inode": 100,
            "last_read_end": 0,
            "subtitle_content": None,
        }

        mock_node = VFSFile(
            name="sub.srt",
            inode=pyfuse3.InodeT(100),
            parent=mock_vfs._root,
            original_filename="subtitle:parent:en",
            file_size=100,
            created_at="2020-01-01T00:00:00",
            updated_at="2020-01-01T00:00:00",
            entry_type="subtitle",
        )
        mock_vfs._inode_to_node[100] = mock_node

        with patch("program.services.filesystem.vfs.rivenvfs.di") as di_mock:
            di_mock.__getitem__.return_value = AsyncMock()

            data1 = await mock_vfs.read(fh, 0, 5)
            assert data1 == b"subti"

            data2 = await mock_vfs.read(fh, 5, 4)
            assert data2 == b"tle "

            mock_vfs.vfs_db.get_subtitle_content.assert_called_once_with("parent", "en")

    trio.run(_run)


def test_readdir_empty_directory(mock_vfs):
    """readdir on an empty directory returns . and .. without ENOENT."""

    async def _run() -> None:
        # readdir treats fh as the directory inode.
        fh = pyfuse3.FileHandleT(200)

        mock_node = VFSDirectory(
            name="empty_dir",
            inode=pyfuse3.InodeT(200),
            parent=mock_vfs._root,
        )
        mock_vfs._inode_to_node[200] = mock_node

        mock_vfs._get_path_from_inode = MagicMock(return_value="/empty_dir")
        mock_vfs._list_directory_cached = MagicMock(return_value=[])
        mock_vfs.getattr = AsyncMock()

        try:
            await mock_vfs.readdir(fh, 0, MagicMock())
        except pyfuse3.FUSEError as e:
            if e.errno == getattr(errno, "ENOENT", 2):
                pytest.fail("readdir raised ENOENT on empty directory")

    trio.run(_run)


def test_stream_timeout_concurrency(mock_vfs):
    """_monitor_stream_timeouts and release() must not race on _active_streams."""

    class MockStream:
        def __init__(self):
            self.is_timed_out = True

        async def close(self):
            await trio.sleep(0.01)

    async def _run() -> None:
        mock_vfs._active_streams_lock = trio.Lock()
        mock_vfs._active_streams["test_path:1"] = MockStream()

        async def run_monitor():
            try:
                with trio.fail_after(0.05):
                    await mock_vfs._monitor_stream_timeouts()
            except trio.TooSlowError:
                pass

        async def run_release():
            mock_vfs._inode_to_node[300] = MagicMock(path="test_path")
            mock_vfs._file_handles[1] = {"inode": 300}
            await trio.sleep(0.005)
            await mock_vfs.release(1)

        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(run_monitor)
                nursery.start_soon(run_release)
        except Exception as e:
            pytest.fail(f"Concurrency test failed with exception: {e}")

        assert "test_path:1" not in mock_vfs._active_streams

    trio.run(_run)


def test_close_waits_for_fuse_thread_and_unmounts(mock_vfs):
    """close() should wait for the background thread and force cleanup if needed."""

    mock_vfs._thread.is_alive.side_effect = [True, False]

    with (
        patch.object(pyfuse3, "trio_token", object(), create=True),
        patch("program.services.filesystem.vfs.rivenvfs.trio.from_thread.run"),
        patch.object(mock_vfs, "_is_mountpoint_mounted", return_value=True),
        patch.object(mock_vfs, "_force_unmount_mountpoint") as force_unmount,
    ):
        mock_vfs.close()

    mock_vfs._thread.join.assert_called_once_with(timeout=10)
    force_unmount.assert_called_once_with(mock_vfs._mountpoint)


def test_get_parent_inodes_collects_ancestors(mock_vfs):
    """Ancestor inodes from a file node up to (but not including) root are collected."""

    root = mock_vfs._root
    movies = VFSDirectory(name="movies", inode=pyfuse3.InodeT(2), parent=root)
    nested = VFSDirectory(name="Action", inode=pyfuse3.InodeT(3), parent=movies)
    file_node = VFSFile(
        name="movie.mkv",
        inode=pyfuse3.InodeT(4),
        parent=nested,
        original_filename="movie.mkv",
        file_size=100,
        created_at="2020-01-01T00:00:00",
        updated_at="2020-01-01T00:00:00",
        entry_type="media",
    )

    parents = mock_vfs._get_parent_inodes(file_node)
    assert parents == [pyfuse3.InodeT(3), pyfuse3.InodeT(2)]


def test_flush_pending_invalidations_clears_and_calls_kernel(mock_vfs):
    """Pending inode invalidations are flushed to pyfuse3 and then cleared."""

    parent_a = pyfuse3.InodeT(10)
    parent_b = pyfuse3.InodeT(11)
    mock_vfs._pending_invalidations = {parent_a, parent_b}

    with patch(
        "program.services.filesystem.vfs.rivenvfs.pyfuse3.invalidate_inode"
    ) as invalidate:
        mock_vfs._flush_pending_invalidations()

        assert invalidate.call_count == 2
        called_inodes = {call.args[0] for call in invalidate.call_args_list}
        assert called_inodes == {parent_a, parent_b}
        assert mock_vfs._pending_invalidations == set()


def test_flush_pending_invalidations_ignores_enoent(mock_vfs):
    """ENOENT from kernel invalidate is expected and must not raise."""

    mock_vfs._pending_invalidations = {pyfuse3.InodeT(42)}

    def _raise_enoent(*_args, **_kwargs):
        raise OSError(errno.ENOENT, "not cached")

    with patch(
        "program.services.filesystem.vfs.rivenvfs.pyfuse3.invalidate_inode",
        side_effect=_raise_enoent,
    ):
        mock_vfs._flush_pending_invalidations()

    assert mock_vfs._pending_invalidations == set()


def test_flush_pending_invalidations_noop_when_empty(mock_vfs):
    """Empty pending set is a no-op and does not call the kernel."""

    mock_vfs._pending_invalidations = set()

    with patch(
        "program.services.filesystem.vfs.rivenvfs.pyfuse3.invalidate_inode"
    ) as invalidate:
        mock_vfs._flush_pending_invalidations()
        invalidate.assert_not_called()


def test_sync_individual_flushes_pending_invalidations(mock_vfs):
    """Individual sync must flush collected parent invalidations after re-add."""

    item = MagicMock()
    item.id = 7
    session = MagicMock()

    with (
        patch("sqlalchemy.orm.object_session", return_value=session),
        patch.object(mock_vfs, "remove") as remove,
        patch.object(mock_vfs, "add") as add,
        patch.object(mock_vfs, "_flush_pending_invalidations") as flush,
    ):
        mock_vfs._sync_individual(item)

    session.refresh.assert_called_once()
    remove.assert_called_once_with(item)
    add.assert_called_once_with(item)
    flush.assert_called_once_with()
