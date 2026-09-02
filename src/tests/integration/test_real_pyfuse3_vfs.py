"""Linux-only real pyfuse3 certification smoke test for the mount-scoped VFS pool.

This is intentionally a test harness rather than production code.  It mounts the
actual ``RivenVFS`` implementation and uses POSIX ``stat``/``open``/``read`` calls
against the kernel mount.  A deterministic, range-aware localhost upstream avoids
external debrid services.  The two narrow seams below exist only because the
production VFS database and debrid-link validator are integration dependencies:
``_HarnessVFSDatabase`` supplies the one entry lookup and ``_LocalDebridUrl``
validates the local URL.  The FUSE callbacks, MediaStream, HTTP pool, and HTTPX
transport remain the production implementations.

Run from Linux/WSL only::

    uv run --python 3.13 pytest -q src/tests/integration/test_real_pyfuse3_vfs.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import sniffio

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/dev/fuse").exists(),
    reason="requires Linux pyfuse3 and an accessible /dev/fuse device",
)

import pyfuse3  # noqa: I001 - Linux-only FUSE extension import must remain after platform guard.
from kink import di

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSFile
from program.services.streaming.http_pool import TrioStreamingHttpPool


_PAYLOAD = bytes(range(256)) * 8192  # 2 MiB, deterministic at every offset.
_FILENAME = "real-fuse-certification.bin"
_MOUNT_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class _Entry:
    url: str
    provider: str = "harness"


class _HarnessVFSDatabase:
    """The smallest VFS DB contract used by ``RivenVFS._get_stream``."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self.entry: _Entry | None = None

    def get_entry_by_original_filename(self, *, original_filename: str) -> _Entry | None:
        return self.entry if original_filename == _FILENAME else None


class _LocalDebridUrl:
    """Harness-only replacement for the synchronous debrid URL preflight."""

    url = ""

    @classmethod
    def from_filename(cls, _: str) -> "_LocalDebridUrl":
        return cls()

    def validate(self) -> str:
        # Exercise the same preflight shape: a blocking HTTPX request that must
        # accept the deterministic upstream before FUSE assigns a file handle.
        with httpx.Client(timeout=5.0) as client:
            response = client.get(self.url, headers={"Range": "bytes=0-0"})
            response.raise_for_status()
        return self.url


class _RangeHandler(BaseHTTPRequestHandler):
    payload = _PAYLOAD

    def do_GET(self) -> None:
        total = len(self.payload)
        header = self.headers.get("Range")
        if not header:
            start, end, status = 0, total - 1, HTTPStatus.OK
        else:
            try:
                unit, raw_range = header.split("=", 1)
                raw_start, raw_end = raw_range.split("-", 1)
                if unit != "bytes":
                    raise ValueError("unsupported range unit")
                start = int(raw_start or 0)
                end = min(int(raw_end) if raw_end else total - 1, total - 1)
                if start < 0 or end < start or start >= total:
                    raise ValueError("invalid range")
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return

        body = self.payload[start : end + 1]
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        pass


@contextmanager
def _range_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/media.bin"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Records the naturally selected async runtime at the real HTTPX boundary."""

    runtimes: list[str] = []

    def __init__(self) -> None:
        self._delegate = httpx.AsyncHTTPTransport(http2=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.runtimes.append(sniffio.current_async_library())
        return await self._delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self._delegate.aclose()


class _RecordingPool(TrioStreamingHttpPool):
    instances: list["_RecordingPool"] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        type(self).instances.append(self)

    def _create_client(self, *, proxy_url: str | None = None) -> httpx.AsyncClient:
        # The test transport delegates to real TCP HTTPX transport; it only records
        # the current runtime at the transport boundary.
        return httpx.AsyncClient(
            transport=_RecordingTransport(),
            follow_redirects=True,
            timeout=httpx.Timeout(10.0),
        )


def _wait_for(predicate: Any, message: str) -> None:
    deadline = time.monotonic() + _MOUNT_WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _install_file(vfs: RivenVFS) -> str:
    """Populate the real VFS tree directly; discovery/naming is not under test."""
    with vfs._tree_lock:
        media_dir = VFSDirectory(
            name="certification",
            inode=vfs._assign_inode(),
            parent=vfs._root,
        )
        vfs._root.add_child(media_dir)
        vfs._inode_to_node[media_dir.inode] = media_dir
        file_node = VFSFile(
            name=_FILENAME,
            inode=vfs._assign_inode(),
            parent=media_dir,
            original_filename=_FILENAME,
            file_size=len(_PAYLOAD),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            entry_type="media",
        )
        media_dir.add_child(file_node)
        vfs._inode_to_node[file_node.inode] = file_node
    return f"certification/{_FILENAME}"


def _mount_is_present(path: Path) -> bool:
    try:
        return any(f" {path} " in line for line in Path("/proc/mounts").read_text().splitlines())
    except OSError:
        return False


def test_kernel_read_uses_one_mount_scoped_trio_pool(tmp_path: Path) -> None:
    """Certify kernel→pyfuse3→RivenVFS→MediaStream→pool→HTTPX for one mount."""
    mountpoint = tmp_path / "mount"
    cache_dir = tmp_path / "cache"
    mountpoint.mkdir()
    cache_dir.mkdir()

    # The VFS owns its mount-local clients; capture non-VFS DI registrations only
    # if this isolated test process has initialized them.
    from program.utils.async_client import AsyncClient
    from program.utils.proxy_client import ProxyClient

    global_async = di._services.get(AsyncClient)
    global_proxy = di._services.get(ProxyClient)
    async_closed_before = getattr(global_async, "is_closed", None)
    proxy_closed_before = getattr(global_proxy, "is_closed", None)

    _RecordingPool.instances.clear()
    _RecordingTransport.runtimes.clear()

    with _range_server() as url:
        _LocalDebridUrl.url = url
        with (
            patch("program.services.filesystem.vfs.rivenvfs.VFSDatabase", _HarnessVFSDatabase),
            patch("program.services.filesystem.vfs.rivenvfs.DebridCDNUrl", _LocalDebridUrl),
            patch("program.services.streaming.http_pool.TrioStreamingHttpPool", _RecordingPool),
            patch.object(RivenVFS, "sync", return_value=None),
        ):
            # Keep cache settings local to the disposable mount.
            from program.settings import settings_manager

            filesystem = settings_manager.settings.filesystem
            original_cache_dir = filesystem.cache_dir
            original_hot_dir = filesystem.cache_hot_dir
            filesystem.cache_dir = cache_dir
            filesystem.cache_hot_dir = None
            vfs = RivenVFS(str(mountpoint), MagicMock())
            try:
                assert isinstance(vfs.vfs_db, _HarnessVFSDatabase)
                vfs.vfs_db.entry = _Entry(url)
                relative_path = _install_file(vfs)

                _wait_for(lambda: vfs.mounted and _mount_is_present(mountpoint), "FUSE mount did not become visible")
                target = mountpoint / relative_path
                _wait_for(target.exists, "kernel did not resolve the real VFS test file")

                stat_result = target.stat()
                assert stat_result.st_size == len(_PAYLOAD)

                # Distinct range reads travel through the mounted kernel path;
                # exact byte comparison proves Content-Range handling end-to-end.
                with target.open("rb", buffering=0) as handle:
                    assert handle.read(4096) == _PAYLOAD[:4096]
                    handle.seek(123_457)
                    assert handle.read(8192) == _PAYLOAD[123_457 : 123_457 + 8192]

                assert len(_RecordingPool.instances) == 1
                assert vfs.http_pool is _RecordingPool.instances[0]
                assert _RecordingTransport.runtimes
                assert set(_RecordingTransport.runtimes) == {"trio"}
            finally:
                filesystem.cache_dir = original_cache_dir
                filesystem.cache_hot_dir = original_hot_dir
                vfs.close()

    _wait_for(lambda: not _mount_is_present(mountpoint), "FUSE mount remained after close")
    assert vfs.http_pool is None
    assert len(_RecordingPool.instances) == 1
    assert _RecordingPool.instances[0]._closed
    assert _RecordingPool.instances[0].active_leases == 0
    assert not _RecordingPool.instances[0]._retired_generations
    assert di._services.get(AsyncClient) is global_async
    assert di._services.get(ProxyClient) is global_proxy
    assert getattr(global_async, "is_closed", None) is async_closed_before
    assert getattr(global_proxy, "is_closed", None) is proxy_closed_before
