import errno

# We need to ensure settings_manager does not crash when imported
from unittest.mock import AsyncMock, MagicMock, patch

import pyfuse3
import pytest
import trio

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSFile


@pytest.fixture
def mock_vfs():
    with (
        patch("pyfuse3.init"),
        patch("threading.Thread"),
        patch("kink.di"),
        patch(
            "program.services.filesystem.vfs.rivenvfs.settings_manager"
        ) as mock_settings,
    ):
        mock_settings.settings.filesystem.cache_dir.exists.return_value = True
        mock_settings.settings.filesystem.cache_max_size_mb = 100

        mock_downloader = MagicMock()

        # We catch potential kink DI errors just in case
        vfs = RivenVFS(mountpoint="/tmp/mock_mtpt", downloader=mock_downloader)

        # Inject mock DB
        vfs.vfs_db = MagicMock()
        vfs.vfs_db.get_subtitle_content = MagicMock(return_value=b"subtitle data")

        yield vfs


@pytest.mark.trio
async def test_subtitle_caching(mock_vfs):
    """Test that reading a subtitle file multiple times only queries the DB once."""
    fh = pyfuse3.FileHandleT(1)
    mock_vfs._file_handles[fh] = {
        "inode": 100,
        "last_read_end": 0,
        "subtitle_content": None,
    }

    mock_node = VFSFile(
        name="sub.srt", original_filename="subtitle:parent:en", file_size=100
    )
    mock_node.inode = 100
    mock_vfs._inode_to_node[100] = mock_node

    # Mocking Cache object
    with patch("program.services.filesystem.vfs.rivenvfs.di") as di_mock:
        di_mock.__getitem__.return_value = AsyncMock()

        # Read chunk 1
        data1 = await mock_vfs.read(fh, 0, 5)
        assert data1 == b"subti"

        # Read chunk 2
        data2 = await mock_vfs.read(fh, 5, 4)
        assert data2 == b"tle "

        # Assert DB was only hit once
        mock_vfs.vfs_db.get_subtitle_content.assert_called_once_with("parent", "en")


@pytest.mark.trio
async def test_readdir_empty_directory(mock_vfs):
    """Test that readdir on an empty directory returns . and .. without ENOENT."""
    fh = pyfuse3.FileHandleT(2)

    mock_node = VFSDirectory(name="empty_dir")
    mock_node.inode = 200
    mock_node.parent = mock_node  # Mock parent
    mock_vfs._inode_to_node[200] = mock_node

    mock_vfs._get_path_from_inode = MagicMock(return_value="/empty_dir")
    mock_vfs._list_directory_cached = MagicMock(return_value=[])

    mock_vfs.getattr = AsyncMock()  # to avoid errors if it tries to stat children

    try:
        await mock_vfs.readdir(fh, 0, MagicMock())
    except pyfuse3.FUSEError as e:
        if e.errno == getattr(errno, "ENOENT", 2):
            pytest.fail("readdir raised ENOENT on empty directory")


@pytest.mark.trio
async def test_stream_timeout_concurrency(mock_vfs):
    """Test that _monitor_stream_timeouts and release() do not encounter race conditions on _active_streams."""

    class MockStream:
        def __init__(self):
            self.is_timed_out = True

        async def close(self):
            # Simulate slight delay during close to widen race window
            await trio.sleep(0.01)

    mock_vfs._active_streams["test_path:1"] = MockStream()

    async def run_monitor():
        # Let it run for just one cycle
        try:
            with trio.fail_after(0.05):
                await mock_vfs._monitor_stream_timeouts()
        except trio.TooSlowError:
            pass

    async def run_release():
        # Attempt to release the exact same stream concurrently
        mock_vfs._inode_to_node[300] = MagicMock(path="test_path")
        mock_vfs._file_handles[1] = {"inode": 300}

        # Delay slightly to ensure monitor starts its iteration first
        await trio.sleep(0.005)

        await mock_vfs.release(1)

    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(run_monitor)
            nursery.start_soon(run_release)
    except Exception as e:
        pytest.fail(f"Concurrency test failed with exception: {e}")

    # Ensure the stream was removed safely
    assert "test_path:1" not in mock_vfs._active_streams


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
