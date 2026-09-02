"""Real Linux FUSE stress certification for mount-scoped VFS HTTP ownership.

Each scenario performs ordinary blocking POSIX reads against an actual pyfuse3
mount.  The local range server and recording pool are test-only instrumentation;
RivenVFS, MediaStream, TrioStreamingHttpPool, and HTTPX are production paths.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import sniffio
import trio

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/dev/fuse").exists(),
    reason="requires Linux pyfuse3 and an accessible /dev/fuse device",
)

import pyfuse3  # noqa: I001 - Keep the Linux extension import after the guard.
from kink import di

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSFile
from program.services.streaming.http_pool import TrioStreamingHttpPool


_PAYLOAD = bytes(range(256)) * 8192
_FILENAME = "real-fuse-stress.bin"
_MOUNT_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class _Entry:
    url: str
    provider: str = "harness"


class _HarnessVFSDatabase:
    def __init__(self, *_: Any, **__: Any) -> None:
        self.entry: _Entry | None = None

    def get_entry_by_original_filename(self, *, original_filename: str) -> _Entry | None:
        return self.entry if original_filename == _FILENAME else None


class _LocalDebridUrl:
    url = ""

    @classmethod
    def from_filename(cls, _: str) -> _LocalDebridUrl:
        return cls()

    def validate(self) -> str:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(self.url, headers={"Range": "bytes=0-0"})
            response.raise_for_status()
        return self.url


@dataclass
class _ServerState:
    delay_seconds: float = 0.0
    request_ranges: list[tuple[int, int]] | None = None
    on_request: Callable[[int, int], None] | None = None
    pause_hook: Callable[[int, int], threading.Event | None] | None = None

    def __post_init__(self) -> None:
        self.lock = threading.Lock()
        self.hold_open = threading.Event()
        self.hold_open.set()
        if self.request_ranges is None:
            self.request_ranges = []


class _RangeHandler(BaseHTTPRequestHandler):
    payload = _PAYLOAD
    state = _ServerState()

    def do_GET(self) -> None:
        total = len(self.payload)
        raw_range = self.headers.get("Range", "bytes=0-").removeprefix("bytes=")
        try:
            raw_start, raw_end = raw_range.split("-", 1)
            start = int(raw_start or 0)
            end = min(int(raw_end) if raw_end else total - 1, total - 1)
            if start < 0 or end < start or start >= total:
                raise ValueError
        except ValueError:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return

        with self.state.lock:
            self.state.request_ranges.append((start, end))
            on_req = self.state.on_request
            pause_hook = self.state.pause_hook

        if on_req is not None:
            on_req(start, end)

        if self.state.delay_seconds:
            time.sleep(self.state.delay_seconds)
        self.state.hold_open.wait(timeout=10.0)

        body = self.payload[start : end + 1]
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

        pause_event = pause_hook(start, end) if pause_hook is not None else None
        if pause_event is not None:
            split = min(512, len(body))
            try:
                if split > 0:
                    self.wfile.write(body[:split])
                    self.wfile.flush()
            except BrokenPipeError:
                return
            pause_event.wait(timeout=10.0)
            try:
                self.wfile.write(body[split:])
                self.wfile.flush()
            except BrokenPipeError:
                return
        else:
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except BrokenPipeError:
                pass

    def log_message(self, *_: Any) -> None:
        pass


@contextmanager
def _range_server() -> Iterator[tuple[str, _ServerState]]:
    state = _ServerState()
    handler = type("HarnessRangeHandler", (_RangeHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/media.bin", state
    finally:
        state.hold_open.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _RecordingTransport(httpx.AsyncBaseTransport):
    runtimes: list[str] = []

    def __init__(self) -> None:
        self._delegate = httpx.AsyncHTTPTransport(http2=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.runtimes.append(sniffio.current_async_library())
        return await self._delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self._delegate.aclose()


class _RecordingPool(TrioStreamingHttpPool):
    instances: list[_RecordingPool] = []
    recycles = 0
    fail_generations: set[int] = set()
    fail_admissions = 0
    heal_started = threading.Event()
    permit_heal = threading.Event()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)

    def _create_client(self, *, proxy_url: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_RecordingTransport(),
            follow_redirects=True,
            timeout=httpx.Timeout(10.0),
        )

    @asynccontextmanager
    async def admit(self, kind: str) -> AsyncIterator[None]:
        if self.generation in type(self).fail_generations:
            raise httpx.PoolTimeout(f"deterministic real-FUSE timeout on gen {self.generation}")
        if type(self).fail_admissions > 0:
            type(self).fail_admissions -= 1
            raise httpx.PoolTimeout("deterministic real-FUSE harness admission timeout")
        async with super().admit(kind):
            yield

    async def heal_on_pool_timeout(
        self,
        *,
        failed_generation: int | None = None,
        pool_repr: str = "",
    ) -> bool:
        type(self).heal_started.set()
        await trio.to_thread.run_sync(type(self).permit_heal.wait)
        healed = await super().heal_on_pool_timeout(
            failed_generation=failed_generation,
            pool_repr=pool_repr,
        )
        if healed:
            type(self).recycles += 1
        return healed

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.recycles = 0
        cls.fail_generations = set()
        cls.fail_admissions = 0
        cls.heal_started.clear()
        cls.permit_heal.set()


def _wait_for(predicate: Callable[[], bool], message: str) -> None:
    deadline = time.monotonic() + _MOUNT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _mounted(path: Path) -> bool:
    try:
        return any(f" {path} " in line for line in Path("/proc/mounts").read_text().splitlines())
    except OSError:
        return False


def _install_file(vfs: RivenVFS) -> str:
    with vfs._tree_lock:
        directory = VFSDirectory(name="certification", inode=vfs._assign_inode(), parent=vfs._root)
        vfs._root.add_child(directory)
        vfs._inode_to_node[directory.inode] = directory
        media = VFSFile(
            name=_FILENAME,
            inode=vfs._assign_inode(),
            parent=directory,
            original_filename=_FILENAME,
            file_size=len(_PAYLOAD),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            entry_type="media",
        )
        directory.add_child(media)
        vfs._inode_to_node[media.inode] = media
    return f"certification/{_FILENAME}"


@contextmanager
def _mounted_vfs(tmp_path: Path, url: str) -> Iterator[tuple[RivenVFS, Path]]:
    mountpoint = tmp_path / "mount"
    cache_dir = tmp_path / "cache"
    mountpoint.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    from program.settings import settings_manager

    filesystem = settings_manager.settings.filesystem
    original_cache_dir, original_hot_dir = filesystem.cache_dir, filesystem.cache_hot_dir
    filesystem.cache_dir, filesystem.cache_hot_dir = cache_dir, None
    vfs: RivenVFS | None = None
    try:
        _LocalDebridUrl.url = url
        with (
            patch("program.services.filesystem.vfs.rivenvfs.VFSDatabase", _HarnessVFSDatabase),
            patch("program.services.filesystem.vfs.rivenvfs.DebridCDNUrl", _LocalDebridUrl),
            patch("program.services.streaming.http_pool.TrioStreamingHttpPool", _RecordingPool),
            patch.object(RivenVFS, "sync", return_value=None),
        ):
            vfs = RivenVFS(str(mountpoint), MagicMock())
            assert isinstance(vfs.vfs_db, _HarnessVFSDatabase)
            vfs.vfs_db.entry = _Entry(url)
            path = mountpoint / _install_file(vfs)
            _wait_for(lambda: vfs.mounted and _mounted(mountpoint), "FUSE mount did not appear")
            _wait_for(path.exists, "kernel did not resolve harness file")
            yield vfs, path
    finally:
        filesystem.cache_dir, filesystem.cache_hot_dir = original_cache_dir, original_hot_dir
        if vfs is not None:
            vfs.close()
            _wait_for(lambda: not _mounted(mountpoint), "FUSE mount remained after close")


def _read_range(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb", buffering=0) as handle:
        handle.seek(offset)
        return handle.read(size)


def _assert_pool_clean(pool: _RecordingPool) -> None:
    assert pool._closed
    assert pool.active_leases == 0
    assert not pool._retired_generations
    assert not pool._clients


def test_real_kernel_concurrent_readers_share_one_mount_pool(tmp_path: Path) -> None:
    """2/4/8 concurrent kernel readers preserve exact ranges and a single pool."""
    from program.utils.async_client import AsyncClient
    from program.utils.proxy_client import ProxyClient

    global_async, global_proxy = di._services.get(AsyncClient), di._services.get(ProxyClient)
    async_closed, proxy_closed = getattr(global_async, "is_closed", None), getattr(global_proxy, "is_closed", None)
    _RecordingPool.reset()
    _RecordingTransport.runtimes.clear()

    with _range_server() as (url, state), _mounted_vfs(tmp_path, url) as (vfs, path):
        for readers in (2, 4, 8):
            requests = [(readers * 71_003 + index * 65_537, 4096 + index) for index in range(readers)]
            with ThreadPoolExecutor(max_workers=readers) as executor:
                actual = list(executor.map(lambda args: _read_range(path, *args), requests))
            assert actual == [_PAYLOAD[offset : offset + size] for offset, size in requests]

        assert len(_RecordingPool.instances) == 1
        assert vfs.http_pool is _RecordingPool.instances[0]
        assert set(_RecordingTransport.runtimes) == {"trio"}
        assert state.request_ranges

    pool = _RecordingPool.instances[0]
    _assert_pool_clean(pool)
    assert di._services.get(AsyncClient) is global_async
    assert di._services.get(ProxyClient) is global_proxy
    assert getattr(global_async, "is_closed", None) is async_closed
    assert getattr(global_proxy, "is_closed", None) is proxy_closed


def test_real_kernel_five_mount_cycles_use_fresh_clean_pools(tmp_path: Path) -> None:
    """Five real mount/read/unmount cycles neither reuse nor leak mount pools."""
    _RecordingPool.reset()
    with _range_server() as (url, _state):
        for cycle in range(5):
            with _mounted_vfs(tmp_path / str(cycle), url) as (vfs, path):
                assert _read_range(path, cycle * 4096, 4096) == _PAYLOAD[cycle * 4096 : (cycle + 1) * 4096]
                assert vfs.http_pool is _RecordingPool.instances[-1]
            _assert_pool_clean(_RecordingPool.instances[-1])

    assert len(_RecordingPool.instances) == 5
    assert len({id(pool) for pool in _RecordingPool.instances}) == 5


def test_real_kernel_pool_timeout_heals_then_next_mounted_read_succeeds(tmp_path: Path) -> None:
    """A mounted read drives local PoolTimeout recovery without mutating global DI."""
    from program.utils.async_client import AsyncClient
    from program.utils.proxy_client import ProxyClient

    global_async, global_proxy = di._services.get(AsyncClient), di._services.get(ProxyClient)
    _RecordingPool.reset()
    _RecordingPool.fail_admissions = 1
    with _range_server() as (url, _state), _mounted_vfs(tmp_path, url) as (vfs, path):
        generation_before = vfs.http_pool.generation if vfs.http_pool else 0
        # MediaStream retries once after the mount pool heals, so this first
        # kernel read is the recovered request rather than an expected error.
        assert _read_range(path, 0, 4096) == _PAYLOAD[:4096]
        _wait_for(lambda: _RecordingPool.recycles == 1, "mounted PoolTimeout did not heal")
        assert vfs.http_pool is not None
        assert vfs.http_pool.generation == generation_before + 1
        assert _read_range(path, 65_537, 4096) == _PAYLOAD[65_537 : 65_537 + 4096]
        assert _RecordingPool.recycles == 1

    _assert_pool_clean(_RecordingPool.instances[0])
    assert di._services.get(AsyncClient) is global_async
    assert di._services.get(ProxyClient) is global_proxy


def test_mount_pool_heal_preserves_active_generation_until_lease_drains() -> None:
    """Pool-level lifecycle proof complements kernel reads without bypassing FUSE tests."""
    async def run() -> None:
        _RecordingPool.reset()
        pool = _RecordingPool()
        try:
            first = pool.acquire_lease()
            old_client = first.client
            assert first.generation == 1
            assert await pool.heal_on_pool_timeout()
            assert pool.generation == 2
            assert first.generation in pool._retired_generations
            assert not old_client.is_closed
            second = pool.acquire_lease()
            assert second.generation == 2
            await pool.release_lease(second)
            assert not old_client.is_closed
            await pool.release_lease(first)
            assert old_client.is_closed
            assert not pool._retired_generations
        finally:
            await pool.teardown()

    trio.run(run)


def test_real_kernel_concurrent_timeout_storm_single_flight_heal(tmp_path: Path) -> None:
    """4 and 8 concurrent mounted readers hitting timeout collapse into a single coordinated heal."""
    from program.utils.async_client import AsyncClient
    from program.utils.proxy_client import ProxyClient

    global_async, global_proxy = di._services.get(AsyncClient), di._services.get(ProxyClient)

    for readers in (4, 8):
        _RecordingPool.reset()
        _RecordingPool.fail_generations = {1}
        _RecordingPool.permit_heal.clear()

        with _range_server() as (url, _state), _mounted_vfs(tmp_path / f"storm_{readers}", url) as (vfs, path):
            assert vfs.http_pool is not None
            pool = vfs.http_pool
            generation_before = pool.generation

            requests = [(index * 65_536, 4096) for index in range(readers)]
            with ThreadPoolExecutor(max_workers=readers) as executor:
                futures = [executor.submit(_read_range, path, *request) for request in requests]
                _wait_for(_RecordingPool.heal_started.is_set, "Heal storm was not initiated")
                _RecordingPool.permit_heal.set()
                actual = [future.result(timeout=10.0) for future in futures]

            assert actual == [_PAYLOAD[offset : offset + size] for offset, size in requests]
            assert pool.generation == generation_before + 1
            assert _RecordingPool.recycles == 1

        _assert_pool_clean(_RecordingPool.instances[0])

    assert di._services.get(AsyncClient) is global_async
    assert di._services.get(ProxyClient) is global_proxy


def test_real_kernel_active_read_survives_generation_rollover_and_drains_on_release(tmp_path: Path) -> None:
    """An active mounted read holding an old generation survives pool heal until its release."""
    _RecordingPool.reset()
    read_a_pause = threading.Event()
    read_a_started = threading.Event()

    with _range_server() as (url, state), _mounted_vfs(tmp_path, url) as (vfs, path):
        with state.lock:
            state.pause_hook = lambda s, e: read_a_pause if s == 0 else None
            state.on_request = lambda s, e: read_a_started.set() if s == 0 else None

        assert vfs.http_pool is not None
        pool = vfs.http_pool
        old_client = pool.get_client()

        # Start Read A in background thread. It will pause midway after headers and initial bytes.
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(_read_range, path, 0, 131_072)
            _wait_for(read_a_started.is_set, "Read A upstream did not start")
            _wait_for(lambda: pool.active_leases >= 1, "Read A did not acquire generation lease")

            assert pool.generation == 1
            assert pool._active_leases_by_gen.get(1, 0) >= 1
            assert not old_client.is_closed

            # Trigger a pool heal while Read A is actively reading
            _RecordingPool.fail_admissions = 1
            # Perform Read B via the mount which triggers heal and completes on Gen 2
            read_b_data = _read_range(path, 524_288, 4096)
            assert read_b_data == _PAYLOAD[524_288 : 524_288 + 4096]

            # Verify pool state: generation bumped, gen 1 retired but NOT closed because Read A is active
            assert pool.generation == 2
            assert 1 in pool._retired_generations
            assert not old_client.is_closed
            assert pool._active_leases_by_gen.get(1, 0) >= 1

            # Release Read A so it finishes receiving its bytes
            read_a_pause.set()
            assert future_a.result(timeout=10.0) == _PAYLOAD[:131_072]

            # Read A is finished: old generation 1 must now be drained and closed
            _wait_for(lambda: old_client.is_closed, "Retired Gen 1 client did not close after lease drain")
            assert 1 not in pool._retired_generations

    _assert_pool_clean(_RecordingPool.instances[0])



def test_real_kernel_read_cancellation_preserves_pool_health(tmp_path: Path) -> None:
    """Cancelling an active mounted kernel read releases its lease and keeps the pool healthy."""
    _RecordingPool.reset()
    read_pause = threading.Event()
    read_started = threading.Event()

    with _range_server() as (url, state), _mounted_vfs(tmp_path, url) as (vfs, path):
        with state.lock:
            state.pause_hook = lambda s, e: read_pause if s == 0 else None
            state.on_request = lambda s, e: read_started.set() if s == 0 else None

        assert vfs.http_pool is not None
        pool = vfs.http_pool

        fd = os.open(str(path), os.O_RDONLY)
        read_error: list[Exception] = []

        def _do_read() -> None:
            try:
                os.read(fd, 65_536)
            except Exception as exc:
                read_error.append(exc)

        thread = threading.Thread(target=_do_read, daemon=True)
        thread.start()

        _wait_for(read_started.is_set, "Cancellable read upstream did not start")
        _wait_for(lambda: pool.active_leases >= 1, "Cancellable read did not acquire lease")

        # Close the file descriptor to trigger FUSE release
        os.close(fd)
        read_pause.set()
        thread.join(timeout=5.0)

        # Leases must drain completely after file handle release
        _wait_for(lambda: pool.active_leases == 0, "Pool active leases did not drain after fd close")

        # Subsequent mounted read must succeed cleanly
        assert _read_range(path, 1024, 2048) == _PAYLOAD[1024 : 1024 + 2048]

    _assert_pool_clean(_RecordingPool.instances[0])


def test_real_kernel_cancellation_during_healing_allows_sibling_recovery(tmp_path: Path) -> None:
    """Cancellation of one waiting reader during heal does not abort sibling recovery."""
    _RecordingPool.reset()
    _RecordingPool.permit_heal.clear()
    _RecordingPool.fail_generations = {1}

    with _range_server() as (url, _state), _mounted_vfs(tmp_path, url) as (vfs, path):
        assert vfs.http_pool is not None
        pool = vfs.http_pool

        fd_a = os.open(str(path), os.O_RDONLY)
        fd_b = os.open(str(path), os.O_RDONLY)
        res_a: list[bytes] = []

        def _worker_a() -> None:
            try:
                res_a.append(os.read(fd_a, 4096))
            finally:
                os.close(fd_a)

        def _worker_b() -> None:
            try:
                os.read(fd_b, 4096)
            except Exception:
                pass
            finally:
                try:
                    os.close(fd_b)
                except OSError:
                    pass

        thread_a = threading.Thread(target=_worker_a, daemon=True)
        thread_b = threading.Thread(target=_worker_b, daemon=True)
        thread_a.start()
        thread_b.start()

        # Wait for heal to be entered.
        _wait_for(_RecordingPool.heal_started.is_set, "Heal was not entered")

        # Cancel caller B while sibling recovery is blocked.
        try:
            os.close(fd_b)
        except OSError:
            pass

        # Let the remaining caller complete recovery.
        _RecordingPool.permit_heal.set()
        thread_a.join(timeout=10.0)
        thread_b.join(timeout=5.0)

        assert res_a == [_PAYLOAD[:4096]]
        assert pool.generation == 2
        assert _RecordingPool.recycles == 1

        # Subsequent mounted read succeeds
        assert _read_range(path, 8192, 4096) == _PAYLOAD[8192 : 8192 + 4096]

    _assert_pool_clean(_RecordingPool.instances[0])


def test_real_kernel_unmount_during_active_read_cleans_all_resources(tmp_path: Path) -> None:
    """Unmounting with active in-flight reads terminates streams and tears down the pool."""
    _RecordingPool.reset()
    read_pause = threading.Event()
    read_started = threading.Event()

    with _range_server() as (url, state):
        with state.lock:
            state.pause_hook = lambda s, e: read_pause if s == 0 else None
            state.on_request = lambda s, e: read_started.set() if s == 0 else None

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with _mounted_vfs(tmp_path, url) as (vfs, path):
                assert vfs.http_pool is not None
                pool = vfs.http_pool

                # Start active read in background
                _ = executor.submit(_read_range, path, 0, 65_536)

                _wait_for(read_started.is_set, "Active read did not start upstream")
                _wait_for(lambda: pool.active_leases >= 1, "Active read did not acquire lease")
        finally:
            read_pause.set()
            executor.shutdown(wait=False)

        assert vfs.http_pool is None
        _assert_pool_clean(pool)
