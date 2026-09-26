"""Real Linux kernel FUSE certification for VFS-001 Adaptive Prefetching.

Validates that adaptive prefetch remains correct, bounded, cancellation-safe,
and byte-exact during real Linux pyfuse3 mount operations, sequential streaming,
and rapid seek patterns under kernel page cache interaction.
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

pytestmark = [pytest.mark.fuse, pytest.mark.integration]

if sys.platform != "linux" or not Path("/dev/fuse").exists():
    pytest.skip(
        "requires Linux pyfuse3 and an accessible /dev/fuse device",
        allow_module_level=True,
    )

pyfuse3 = pytest.importorskip("pyfuse3")

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSFile
from program.services.streaming.http_pool import TrioStreamingHttpPool

# 8 MB payload so we have multiple 1 MB chunks to exercise prefetching & seeking
_PAYLOAD = bytes(range(256)) * 32768
_FILENAME = "adaptive-stream-test.bin"
_MOUNT_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class _Entry:
    url: str
    provider: str = "harness"


class _HarnessVFSDatabase:
    def __init__(self, *_: Any, **__: Any) -> None:
        self.entry: _Entry | None = None

    def get_entry_by_original_filename(
        self, *, original_filename: str
    ) -> _Entry | None:
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
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.request_ranges: list[tuple[int, int]] = []
        self.pause_hook: Callable[[int, int], threading.Event | None] | None = None

    def record_range(self, start: int, end: int) -> None:
        with self.lock:
            self.request_ranges.append((start, end))


class _RangeHandler(BaseHTTPRequestHandler):
    payload = _PAYLOAD
    state: _ServerState

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

        self.state.record_range(start, end)

        with self.state.lock:
            hook = self.state.pause_hook
        pause_event = hook(start, end) if hook else None

        body = self.payload[start : end + 1]
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

        if pause_event is not None:
            split = min(512, len(body))
            try:
                if split > 0:
                    self.wfile.write(body[:split])
                    self.wfile.flush()
            except BrokenPipeError:
                return
            pause_event.wait(timeout=5.0)
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
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait_for(predicate: Callable[[], bool], message: str) -> None:
    deadline = time.monotonic() + _MOUNT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _mounted(path: Path) -> bool:
    try:
        return any(
            f" {path} " in line
            for line in Path("/proc/mounts").read_text().splitlines()
        )
    except OSError:
        return False


def _install_file(vfs: RivenVFS) -> str:
    with vfs._tree_lock:
        directory = VFSDirectory(
            name="adaptive_cert", inode=vfs._assign_inode(), parent=vfs._root
        )
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
    return f"adaptive_cert/{_FILENAME}"


@contextmanager
def _mounted_vfs(tmp_path: Path, url: str) -> Iterator[tuple[RivenVFS, Path]]:
    mountpoint = tmp_path / "mount"
    cache_dir = tmp_path / "cache"
    mountpoint.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    from program.settings import settings_manager

    filesystem = settings_manager.settings.filesystem
    original_cache_dir, original_hot_dir = (
        filesystem.cache_dir,
        filesystem.cache_hot_dir,
    )
    filesystem.cache_dir, filesystem.cache_hot_dir = cache_dir, None
    vfs: RivenVFS | None = None
    try:
        _LocalDebridUrl.url = url
        with (
            patch(
                "program.services.filesystem.vfs.rivenvfs.VFSDatabase",
                _HarnessVFSDatabase,
            ),
            patch(
                "program.services.filesystem.vfs.rivenvfs.DebridCDNUrl", _LocalDebridUrl
            ),
            patch.object(RivenVFS, "sync", return_value=None),
        ):
            vfs = RivenVFS(str(mountpoint), MagicMock())
            assert isinstance(vfs.vfs_db, _HarnessVFSDatabase)
            vfs.vfs_db.entry = _Entry(url)
            path = mountpoint / _install_file(vfs)
            _wait_for(
                lambda: vfs.mounted and _mounted(mountpoint),
                "FUSE mount did not appear",
            )
            _wait_for(path.exists, "kernel did not resolve harness file")
            yield vfs, path
    finally:
        filesystem.cache_dir, filesystem.cache_hot_dir = (
            original_cache_dir,
            original_hot_dir,
        )
        if vfs is not None:
            vfs.close()
            _wait_for(
                lambda: not _mounted(mountpoint), "FUSE mount remained after close"
            )


def test_real_kernel_adaptive_prefetch_sequential_playback(tmp_path: Path) -> None:
    """Verifies real POSIX reads through pyfuse3 receive exact payload bytes sequentially."""
    with _range_server() as (url, server_state):
        with _mounted_vfs(tmp_path, url) as (vfs, path):
            # Open file via direct OS open and read 3 MB in 256 KB increments
            fd = os.open(str(path), os.O_RDONLY)
            try:
                chunk_size = 256 * 1024
                total_to_read = 3 * 1024 * 1024
                bytes_read = bytearray()
                while len(bytes_read) < total_to_read:
                    data = os.read(fd, min(chunk_size, total_to_read - len(bytes_read)))
                    if not data:
                        break
                    bytes_read.extend(data)

                assert len(bytes_read) == total_to_read
                assert bytes(bytes_read) == _PAYLOAD[:total_to_read]
            finally:
                os.close(fd)

            # Check that requests arrived at range server
            with server_state.lock:
                assert len(server_state.request_ranges) > 0


def test_real_kernel_adaptive_prefetch_rapid_seek_cancellation(tmp_path: Path) -> None:
    """Verifies rapid kernel seeking across disparate offsets returns byte-exact data without desynchronization."""
    with _range_server() as (url, server_state):
        with _mounted_vfs(tmp_path, url) as (vfs, path):
            fd = os.open(str(path), os.O_RDONLY)
            try:
                # Seek targets across the 8 MB payload
                seek_offsets = [
                    0,
                    2 * 1024 * 1024 + 128,
                    5 * 1024 * 1024 + 4096,
                    1 * 1024 * 1024 + 512,
                    6 * 1024 * 1024 + 8192,
                    256 * 1024,
                ]
                read_len = 128 * 1024  # 128 KB each

                for offset in seek_offsets:
                    actual_offset = os.lseek(fd, offset, os.SEEK_SET)
                    assert actual_offset == offset
                    data = os.read(fd, read_len)
                    assert len(data) == read_len
                    expected = _PAYLOAD[offset : offset + read_len]
                    assert data == expected, f"Data mismatch after seek to {offset}!"
            finally:
                os.close(fd)


def test_real_kernel_adaptive_prefetch_concurrent_readers(tmp_path: Path) -> None:
    """Verifies 3 concurrent POSIX reader threads reading different file regions maintain exact correctness."""
    with _range_server() as (url, server_state):
        with _mounted_vfs(tmp_path, url) as (vfs, path):

            def read_worker(offset: int, size: int) -> bytes:
                with path.open("rb", buffering=0) as handle:
                    handle.seek(offset)
                    return handle.read(size)

            ranges = [
                (0, 1024 * 1024),
                (2 * 1024 * 1024, 1024 * 1024),
                (4 * 1024 * 1024, 1024 * 1024),
            ]

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(read_worker, off, sz) for off, sz in ranges]
                results = [f.result() for f in futures]

            for (off, sz), res in zip(ranges, results, strict=True):
                assert len(res) == sz
                assert res == _PAYLOAD[off : off + sz]


def test_real_kernel_adaptive_prefetch_seek_during_in_flight_download(
    tmp_path: Path,
) -> None:
    """Verifies seeking while a prefetch chunk is in-flight cleanly recovers without socket desynchronization."""
    pause_event = threading.Event()

    def pause_hook(start: int, end: int) -> threading.Event | None:
        if 1024 * 1024 <= start < 2 * 1024 * 1024:
            return pause_event
        return None

    with _range_server() as (url, server_state):
        server_state.pause_hook = pause_hook
        with _mounted_vfs(tmp_path, url) as (vfs, path):
            fd = os.open(str(path), os.O_RDONLY)
            try:
                # Read first 128 KB of chunk 0
                data0 = os.read(fd, 128 * 1024)
                assert data0 == _PAYLOAD[: 128 * 1024]

                # Give background prefetch a moment to trigger request for chunk 1
                time.sleep(0.1)

                # Now perform a sudden seek to 4 MB while chunk 1 is paused in flight
                seek_offset = 4 * 1024 * 1024
                os.lseek(fd, seek_offset, os.SEEK_SET)

                # Unblock paused event so the server socket finishes or gets aborted
                pause_event.set()

                # Read at new seek location - must return exact bytes
                read_len = 256 * 1024
                data_seek = os.read(fd, read_len)
                assert len(data_seek) == read_len
                assert data_seek == _PAYLOAD[seek_offset : seek_offset + read_len]
            finally:
                pause_event.set()
                os.close(fd)


def test_real_kernel_adaptive_prefetch_edge_cases(tmp_path: Path) -> None:
    """Verifies 0-byte reads, seek to EOF, and read past EOF return safely."""
    with _range_server() as (url, server_state):
        with _mounted_vfs(tmp_path, url) as (vfs, path):
            fd = os.open(str(path), os.O_RDONLY)
            try:
                # 0-byte read
                assert os.read(fd, 0) == b""

                # Seek directly to EOF
                eof_offset = len(_PAYLOAD)
                assert os.lseek(fd, eof_offset, os.SEEK_SET) == eof_offset
                assert os.read(fd, 1024) == b""

                # Seek past EOF
                past_eof = eof_offset + 1024
                assert os.lseek(fd, past_eof, os.SEEK_SET) == past_eof
                assert os.read(fd, 1024) == b""

                # Seek back to beginning and verify valid read
                assert os.lseek(fd, 0, os.SEEK_SET) == 0
                assert os.read(fd, 100) == _PAYLOAD[:100]
            finally:
                os.close(fd)


def test_real_kernel_sustained_playback_and_resource_cleanup(tmp_path: Path) -> None:
    """Verifies sustained reading across full file and clean resource reclamation upon close."""
    with _range_server() as (url, server_state):
        with _mounted_vfs(tmp_path, url) as (vfs, path):
            fd = os.open(str(path), os.O_RDONLY)
            try:
                total_read = 0
                block_size = 512 * 1024
                while total_read < len(_PAYLOAD):
                    buf = os.read(fd, block_size)
                    if not buf:
                        break
                    assert buf == _PAYLOAD[total_read : total_read + len(buf)]
                    total_read += len(buf)
                assert total_read == len(_PAYLOAD)
            finally:
                os.close(fd)

            if vfs.http_pool is not None:
                _wait_for(
                    lambda: vfs.http_pool.active_leases == 0,
                    "active leases did not drain after file close",
                )
                assert vfs.http_pool.active_leases == 0
