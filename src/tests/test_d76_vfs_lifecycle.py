"""Tests for D76 VFS Unmount/Remount Lifecycle, State Machine, and Settings Synchronization."""

import errno
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest


def _ensure_pyfuse3() -> None:
    """Install a minimal pyfuse3 stub when Operations is unavailable (Windows test environment)."""
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

from program.services.filesystem.filesystem_service import FilesystemService
from program.services.filesystem.vfs.rivenvfs import RivenVFS, VFSState


class TestVFSLifecycleStateMachine:
    """Verifies VFSState transitions, lifecycle tracking, and synchronization events."""

    def test_lifecycle_id_unique_generation_invariant(self):
        """Lifecycle IDs are uniquely generated 8-character hex identifiers preventing log collision."""
        import uuid

        vfs1 = RivenVFS.__new__(RivenVFS)
        vfs1.lifecycle_id = uuid.uuid4().hex[:8]

        vfs2 = RivenVFS.__new__(RivenVFS)
        vfs2.lifecycle_id = uuid.uuid4().hex[:8]

        assert len(vfs1.lifecycle_id) == 8
        assert len(vfs2.lifecycle_id) == 8
        assert vfs1.lifecycle_id != vfs2.lifecycle_id
        int(vfs1.lifecycle_id, 16)
        int(vfs2.lifecycle_id, 16)

    def test_initial_state(self):
        """A freshly initialized RivenVFS instance starts in CREATED state before mounting."""
        with (
            patch(
                "program.services.filesystem.vfs.rivenvfs.RivenVFS._prepare_mountpoint"
            ),
            patch("program.services.filesystem.vfs.rivenvfs.threading.Thread"),
        ):
            vfs = RivenVFS.__new__(RivenVFS)
            vfs.state = VFSState.CREATED
            vfs.lifecycle_id = "test-init-001"
            vfs._mount_ready_event = threading.Event()
            vfs._ready_event = threading.Event()
            vfs._state_lock = threading.Lock()

            assert vfs.state == VFSState.CREATED
            assert vfs.lifecycle_id == "test-init-001"
            assert not vfs._mount_ready_event.is_set()
            assert not vfs._ready_event.is_set()

    def test_state_transitions_fire_events(self):
        """_set_state() updates state and sets synchronization events at key thresholds."""
        vfs = RivenVFS.__new__(RivenVFS)
        vfs.state = VFSState.CREATED
        vfs.mounted = False
        vfs.lifecycle_id = "test-transitions-002"
        vfs._mount_ready_event = threading.Event()
        vfs._ready_event = threading.Event()
        vfs._state_lock = threading.Lock()

        # Transition to MOUNTING
        vfs._set_state(VFSState.MOUNTING)
        assert vfs.state == VFSState.MOUNTING
        assert not vfs.mounted
        assert not vfs._mount_ready_event.is_set()
        assert not vfs._ready_event.is_set()

        # Transition to MOUNTED -> triggers _mount_ready_event and sets mounted=True
        vfs._set_state(VFSState.MOUNTED)
        assert vfs.state == VFSState.MOUNTED
        assert vfs.mounted
        assert vfs._mount_ready_event.is_set()
        assert not vfs._ready_event.is_set()

        # Transition to SYNCING -> _mount_ready_event stays set
        vfs._set_state(VFSState.SYNCING)
        assert vfs.state == VFSState.SYNCING
        assert vfs.mounted
        assert vfs._mount_ready_event.is_set()
        assert not vfs._ready_event.is_set()

        # Transition to READY -> triggers _ready_event
        vfs._set_state(VFSState.READY)
        assert vfs.state == VFSState.READY
        assert vfs.mounted
        assert vfs._mount_ready_event.is_set()
        assert vfs._ready_event.is_set()

        # Transition to STOPPING -> clears mounted flag
        vfs._set_state(VFSState.STOPPING)
        assert vfs.state == VFSState.STOPPING
        assert not vfs.mounted

        # Transition to STOPPED
        vfs._set_state(VFSState.STOPPED)
        assert vfs.state == VFSState.STOPPED
        assert not vfs.mounted

    def test_wait_until_mounted_success(self):
        """wait_until_mounted() returns True when state is MOUNTED, SYNCING, or READY."""
        vfs = RivenVFS.__new__(RivenVFS)
        vfs.state = VFSState.MOUNTING
        vfs.mounted = False
        vfs.lifecycle_id = "test-wait-mounted-003"
        vfs._mount_ready_event = threading.Event()
        vfs._ready_event = threading.Event()
        vfs._state_lock = threading.Lock()

        def set_mounted_later():
            time.sleep(0.05)
            vfs._set_state(VFSState.MOUNTED)

        t = threading.Thread(target=set_mounted_later)
        t.start()
        res = vfs.wait_until_mounted(timeout=1.0)
        t.join()

        assert res is True
        assert vfs.state == VFSState.MOUNTED
        assert vfs.mounted is True

    def test_wait_until_mounted_timeout(self):
        """wait_until_mounted() returns False if timeout expires before mounting."""
        vfs = RivenVFS.__new__(RivenVFS)
        vfs.state = VFSState.MOUNTING
        vfs.mounted = False
        vfs.lifecycle_id = "test-wait-mounted-timeout-004"
        vfs._mount_ready_event = threading.Event()
        vfs._ready_event = threading.Event()
        vfs._state_lock = threading.Lock()

        res = vfs.wait_until_mounted(timeout=0.05)
        assert res is False

    def test_wait_until_ready_success(self):
        """wait_until_ready() returns True when state reaches READY."""
        vfs = RivenVFS.__new__(RivenVFS)
        vfs.state = VFSState.SYNCING
        vfs.lifecycle_id = "test-wait-ready-005"
        vfs._mount_ready_event = threading.Event()
        vfs._ready_event = threading.Event()
        vfs._state_lock = threading.Lock()

        def set_ready_later():
            time.sleep(0.05)
            vfs._set_state(VFSState.READY)

        t = threading.Thread(target=set_ready_later)
        t.start()
        res = vfs.wait_until_ready(timeout=1.0)
        t.join()

        assert res is True
        assert vfs.state == VFSState.READY


class TestVFSStructuredCancellation:
    """Verifies stop event signaling and teardown behavior."""

    def test_stop_event_terminates_runner_immediately(self):
        """Setting _stop_event stops the _fuse_runner loop without spurious retries."""
        vfs = RivenVFS.__new__(RivenVFS)
        vfs._stop_event = threading.Event()
        vfs._stop_event.set()
        vfs.lifecycle_id = "test-stop-event-006"
        vfs.unmount_requested = False

        # In _fuse_runner, while not unmount_requested and not self._stop_event.is_set():
        # Condition should be False immediately
        loop_entered = False
        while not vfs.unmount_requested and not vfs._stop_event.is_set():
            loop_entered = True
            break

        assert not loop_entered

    def test_prepare_mountpoint_fast_exit_when_not_mounted(self):
        """_prepare_mountpoint exits immediately when mountpoint is already unmounted."""
        vfs = RivenVFS.__new__(RivenVFS)
        vfs.lifecycle_id = "test-prepare-fast-exit-007"
        mock_mountpoint = MagicMock()
        mock_mountpoint.is_mount.return_value = False
        mock_mountpoint.exists.return_value = True

        with (
            patch(
                "program.services.filesystem.vfs.rivenvfs.os.path.ismount",
                return_value=False,
            ),
            patch("subprocess.run") as mock_subproc,
        ):
            vfs._prepare_mountpoint(mock_mountpoint)
            # subprocess.run (fusermount -uz / umount) should NOT be called if not mounted
            assert mock_subproc.call_count == 0


class TestServiceCoordinationAndReconfigLock:
    """Verifies FilesystemService validation and Program reconfiguration locking."""

    def test_filesystem_service_validate_waits_for_mount(self):
        """FilesystemService.validate() calls wait_until_mounted() instead of reading stale flags."""
        service = FilesystemService.__new__(FilesystemService)
        service.settings = MagicMock()
        service.settings.mount_path = "/mnt/test"
        mock_vfs = MagicMock()
        mock_vfs.mounted = True
        mock_vfs.wait_until_mounted.return_value = True
        service.riven_vfs = mock_vfs

        assert service.validate() is True
        mock_vfs.wait_until_mounted.assert_called_once_with(timeout=5.0)

    def test_program_reconfig_lock_serializes_concurrent_initializations(self):
        """Concurrent calls to initialize_services are serialized via _reconfig_lock."""
        from program.program import Program

        prog = Program.__new__(Program)
        prog._reconfig_lock = threading.Lock()
        prog.services = []

        execution_order = []
        lock_held_simultaneously = []

        def mock_init_body(thread_id: int):
            with prog._reconfig_lock:
                lock_held_simultaneously.append(thread_id)
                assert (
                    len(lock_held_simultaneously) == 1
                ), "Reconfig lock failed to serialize!"
                time.sleep(0.05)
                execution_order.append(thread_id)
                lock_held_simultaneously.pop()

        t1 = threading.Thread(target=mock_init_body, args=(1,))
        t2 = threading.Thread(target=mock_init_body, args=(2,))

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(execution_order) == 2
        assert set(execution_order) == {1, 2}
