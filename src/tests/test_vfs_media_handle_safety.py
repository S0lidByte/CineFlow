"""Unit tests for RivenVFS media handle safety API."""

import errno
import sys
import threading
import types


def _ensure_pyfuse3() -> None:
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
    sys.modules["pyfuse3"] = stub
    sys.modules["pyfuse3.errno"] = errno_mod


_ensure_pyfuse3()

import pyfuse3

from program.services.filesystem.vfs.rivenvfs import RivenVFS, VfsMediaHandleState


def test_vfs_media_handle_safety_empty():
    mock_instance = object.__new__(RivenVFS)
    mock_instance._tree_lock = threading.RLock()
    mock_instance._file_handles = {}

    assert RivenVFS.has_open_media_handles(mock_instance) is False
    state = RivenVFS.get_media_handle_state(mock_instance)
    assert isinstance(state, VfsMediaHandleState)
    assert state.has_open_handles is False
    assert state.total_handles == 0
    assert state.open_paths == []


def test_vfs_media_handle_safety_with_open_handles():
    mock_instance = object.__new__(RivenVFS)
    mock_instance._tree_lock = threading.RLock()
    mock_instance._file_handles = {
        pyfuse3.FileHandleT(1): {
            "inode": pyfuse3.InodeT(100),
            "path": "/movies/Test.Movie.2024.mkv",
            "last_read_end": 1024,
            "subtitle_content": None,
        },
        pyfuse3.FileHandleT(2): {
            "inode": pyfuse3.InodeT(101),
            "path": None,
            "last_read_end": 0,
            "subtitle_content": None,
        },
    }

    assert RivenVFS.has_open_media_handles(mock_instance) is True
    state = RivenVFS.get_media_handle_state(mock_instance)
    assert state.has_open_handles is True
    assert state.total_handles == 2
    assert "/movies/Test.Movie.2024.mkv" in state.open_paths
    assert "inode:101" in state.open_paths
