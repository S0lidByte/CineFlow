from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from bisect import bisect_right, insort
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, Required, TypedDict

import trio
from loguru import logger


class CacheSnapshot(TypedDict):
    hits: Required[int]
    misses: Required[int]
    bytes_from_cache: Required[int]
    bytes_written: Required[int]
    evictions: Required[int]
    total_bytes: NotRequired[int]
    entries: NotRequired[int]


@dataclass
class CacheConfig:
    cache_dir: Path
    max_size_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB
    ttl_seconds: int = 2 * 60 * 60  # 2 hours
    eviction: Literal["LRU", "TTL"] = "LRU"
    metrics_enabled: bool = True
    # Optional hot tier (typically tmpfs). When set, puts go hot-first and
    # LRU overflow is demoted to cache_dir (warm).
    hot_dir: Path | None = None
    hot_max_size_bytes: int = 0

    @property
    def two_tier(self) -> bool:
        return self.hot_dir is not None and self.hot_max_size_bytes > 0


@dataclass(frozen=True)
class CacheEntry:
    key: str
    cache_key: str
    start: int
    size: int
    mtime: float
    tier: Literal["hot", "warm"] = "warm"


@dataclass(frozen=True)
class ChunkInfo:
    chunk_key: str
    chunk_ts: float
    chunk_file: Path
    copy_start: int
    bytes_to_read: int
    chunk_end: int


class Metrics:
    def __init__(self, *, prom_enabled: bool = True) -> None:
        self.hits = 0
        self.misses = 0
        self.bytes_from_cache = 0
        self.bytes_written = 0
        self.evictions = 0
        self.prom_enabled = prom_enabled
        self.lock = threading.Lock()

    def snapshot(self) -> CacheSnapshot:
        with self.lock:
            return CacheSnapshot(
                hits=self.hits,
                misses=self.misses,
                bytes_from_cache=self.bytes_from_cache,
                bytes_written=self.bytes_written,
                evictions=self.evictions,
            )

    def record_hit(self, nbytes: int) -> None:
        with self.lock:
            self.hits += 1
            self.bytes_from_cache += nbytes
        if self.prom_enabled:
            from program.services.streaming import prom_cache_metrics as prom

            prom.record_hit(nbytes)

    def record_miss(self) -> None:
        with self.lock:
            self.misses += 1
        if self.prom_enabled:
            from program.services.streaming import prom_cache_metrics as prom

            prom.record_miss()

    def record_bytes_written(self, nbytes: int) -> None:
        with self.lock:
            self.bytes_written += nbytes
        if self.prom_enabled:
            from program.services.streaming import prom_cache_metrics as prom

            prom.record_bytes_written(nbytes)

    def record_evictions(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self.lock:
            self.evictions += count
        if self.prom_enabled:
            from program.services.streaming import prom_cache_metrics as prom

            prom.record_evictions(count)


class Cache:
    """
    Simple file-based block cache on disk with cross-chunk boundary support.
    We maintain a small in-memory LRU index for eviction decisions.

    Concurrency model (multi-title playback):
    - Brief global index lock for ``_index`` / ``_by_path`` / ``_total_bytes`` only.
    - Per-``cache_key`` shard locks serialize writers (``put``) for the same title.
    - ``get`` never holds a shard across disk I/O so duplicate-path opens overlap.
    - Disk I/O runs in ``trio.to_thread`` so the FUSE/trio loop is never blocked
      on ``open``/``read``/``write`` (critical with disk-backed ``cache_dir``).
    """

    _SHARD_COUNT = 32

    def __init__(self, cfg: CacheConfig) -> None:
        self.cfg = cfg
        self._index = OrderedDict[str, CacheEntry]()
        self._by_path = dict[str, list[int]]()
        self._total_bytes = 0
        self._hot_bytes = 0
        # Brief global lock for index / eviction accounting only — never across I/O.
        self._index_lock = trio.Lock()
        # Thread lock for synchronizing _index/_by_path with sync has() callers.
        self._thread_lock = threading.Lock()
        # Per-cache_key shards so concurrent titles do not wait on each other.
        self._shard_locks = [trio.Lock() for _ in range(self._SHARD_COUNT)]
        self._metrics = Metrics(prom_enabled=cfg.metrics_enabled)
        self._last_log = 0.0  # Initialize last log timestamp

        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
        except Exception as e:
            # Do not raise here; CacheManager may have attempted to validate and fall back.
            logger.warning(
                f"Disk cache directory init warning for {self.cfg.cache_dir}: {e}"
            )

        if self.cfg.two_tier and self.cfg.hot_dir is not None:
            try:
                os.makedirs(self.cfg.hot_dir, exist_ok=True)
            except Exception as e:
                logger.warning(
                    f"Hot cache directory init warning for {self.cfg.hot_dir}: {e}"
                )

        trio.run(self._initialize)

    def _shard_for(self, cache_key: str) -> trio.Lock:
        # Stable across process lifetime; collisions only map unrelated keys together.
        bucket = int(hashlib.sha1(cache_key.encode()).hexdigest(), 16) % self._SHARD_COUNT
        return self._shard_locks[bucket]

    @asynccontextmanager
    async def _shard(self, cache_key: str) -> AsyncGenerator[None, None]:
        """Serialize get/put for one cache_key; other titles use other shards."""
        async with self._shard_for(cache_key):
            yield

    @asynccontextmanager
    async def locks(self) -> AsyncGenerator[None, None]:
        """Async index lock for LRU mutations. Never hold across disk I/O.

        _thread_lock is decoupled: sync callers (has, sync_size_snapshot) use it
        directly without blocking on this trio lock. Index writes (put, _initial_scan)
        acquire _thread_lock explicitly inside this context.
        """

        async with self._index_lock:
            yield

    @staticmethod
    def _read_file_slice(path: Path, offset: int, size: int) -> bytes:
        with path.open("rb") as f:
            f.seek(offset)
            return f.read(size)

    @staticmethod
    def _read_file_all(path: Path) -> bytes | None:
        try:
            with path.open("rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    @staticmethod
    def _write_file_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            f.write(data)

    async def _initialize(self) -> None:
        # Lazy-rebuild index for any pre-existing files so size limits apply after restart
        try:
            await self._initial_scan()
        except Exception as e:
            logger.debug(f"Disk cache initial scan skipped: {e}")

    async def _initial_scan(self) -> None:
        # Build index from on-disk files, ordered by mtime ascending for LRU correctness
        entries: list[CacheEntry] = []

        roots: list[tuple[Path, Literal["hot", "warm"]]] = [
            (self.cfg.cache_dir, "warm"),
        ]
        if self.cfg.two_tier and self.cfg.hot_dir is not None:
            roots.insert(0, (self.cfg.hot_dir, "hot"))

        try:
            for root, tier in roots:
                try:
                    if not root.exists():
                        continue
                    for sub in root.iterdir():
                        try:
                            if sub.is_dir():
                                for fp in sub.iterdir():
                                    try:
                                        if not fp.is_file() or fp.suffix == ".meta":
                                            continue

                                        key = fp.name
                                        st = fp.stat()
                                        metadata = self._read_metadata(key, tier=tier)

                                        if metadata:
                                            cache_key, start = metadata
                                            entries.append(
                                                CacheEntry(
                                                    key=key,
                                                    cache_key=cache_key,
                                                    start=start,
                                                    size=int(st.st_size),
                                                    mtime=float(st.st_mtime),
                                                    tier=tier,
                                                )
                                            )
                                        else:
                                            logger.warning(
                                                f"Removing orphaned cache file without metadata: {fp}"
                                            )
                                            try:
                                                fp.unlink()
                                                self._remove_metadata(key, tier=tier)
                                            except Exception as e:
                                                logger.warning(
                                                    f"Failed to remove orphaned cache file {fp}: {e}"
                                                )
                                    except Exception:
                                        continue
                            elif sub.is_file() and sub.suffix != ".meta":
                                key = sub.name
                                st = sub.stat()
                                metadata = self._read_metadata(key, tier=tier)
                                if metadata:
                                    cache_key, start = metadata
                                    entries.append(
                                        CacheEntry(
                                            key=key,
                                            cache_key=cache_key,
                                            start=start,
                                            size=int(st.st_size),
                                            mtime=float(st.st_mtime),
                                            tier=tier,
                                        )
                                    )
                                else:
                                    logger.warning(
                                        f"Removing orphaned cache file without metadata: {sub}"
                                    )
                                    try:
                                        sub.unlink()
                                        self._remove_metadata(key, tier=tier)
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to remove orphaned cache file {sub}: {e}"
                                        )
                        except Exception:
                            continue
                except Exception:
                    continue
        finally:
            entries.sort(key=lambda t: t.mtime)  # by mtime asc

            async with self.locks():
                with self._thread_lock:
                    self._index.clear()
                    self._by_path.clear()
                    self._total_bytes = 0
                    self._hot_bytes = 0

                    for cache_entry in entries:
                        # Prefer hot if the same key appears in both (shouldn't normally)
                        existing = self._index.get(cache_entry.key)
                        if existing and existing.tier == "hot" and cache_entry.tier == "warm":
                            continue
                        if existing:
                            self._total_bytes -= existing.size
                            if existing.tier == "hot":
                                self._hot_bytes -= existing.size

                        self._index[cache_entry.key] = cache_entry
                        self._total_bytes += cache_entry.size
                        if cache_entry.tier == "hot":
                            self._hot_bytes += cache_entry.size

                        lst = self._by_path.setdefault(cache_entry.cache_key, [])
                        if cache_entry.start not in lst:
                            insort(lst, cache_entry.start)

            try:
                await self.trim()
            except Exception:
                pass

    def _key(self, path: str, start: int) -> str:
        h = hashlib.sha1(f"{path}|{start}".encode()).hexdigest()
        return h

    def _file_for(
        self, key: str, *, tier: Literal["hot", "warm"] = "warm"
    ) -> Path:
        """Return cache file path without creating directories.

        Read paths (``get`` / ``has``) must not mkdir under the global lock —
        fanout dirs are created only on write via ``_ensure_parent``.
        """
        sub = key[:2]
        if tier == "hot" and self.cfg.hot_dir is not None:
            return self.cfg.hot_dir / sub / key
        return self.cfg.cache_dir / sub / key

    def _ensure_parent(self, path: Path) -> None:
        """Create the two-level fanout directory for a cache file (writes only)."""
        path.parent.mkdir(parents=True, exist_ok=True)

    def _metadata_file_for(
        self, key: str, *, tier: Literal["hot", "warm"] = "warm"
    ) -> Path:
        """Get the metadata sidecar file path for a cache entry."""

        return self._file_for(key, tier=tier).with_suffix(".meta")

    def _write_metadata(
        self,
        key: str,
        cache_key: str,
        start: int,
        *,
        tier: Literal["hot", "warm"] = "warm",
    ) -> None:
        """Write metadata for a cache entry to a sidecar file."""

        metadata = {"cache_key": cache_key, "start": start}

        try:
            meta_path = self._metadata_file_for(key, tier=tier)
            self._ensure_parent(meta_path)
            with meta_path.open("w") as f:
                json.dump(metadata, f)
        except Exception as e:
            logger.warning(f"Failed to write cache metadata for {key}: {e}")

    def _read_metadata(
        self, key: str, *, tier: Literal["hot", "warm"] = "warm"
    ) -> tuple[str, int] | None:
        """Read metadata for a cache entry from its sidecar file."""

        metadata_file = self._metadata_file_for(key, tier=tier)

        if not metadata_file.exists():
            return None

        try:
            with metadata_file.open("r") as f:
                metadata = json.load(f)
                return metadata["cache_key"], metadata["start"]
        except Exception as e:
            logger.warning(f"Failed to read cache metadata for {key}: {e}")
            return None

    def _remove_metadata(
        self, key: str, *, tier: Literal["hot", "warm"] = "warm"
    ) -> None:
        """Remove metadata file for a cache entry."""

        try:
            metadata_file = self._metadata_file_for(key, tier=tier)

            if metadata_file.exists():
                metadata_file.unlink()
        except Exception as e:
            logger.warning(f"Failed to remove cache metadata for {key}: {e}")

    def _unlink_cache_files(
        self,
        keys: list[str],
        *,
        tiers: dict[str, Literal["hot", "warm"]] | None = None,
    ) -> None:
        """Delete cache payload + metadata files outside the index lock."""
        for k in keys:
            tier = (tiers or {}).get(k, "warm")
            fp = self._file_for(k, tier=tier)
            try:
                if fp.exists():
                    fp.unlink()
            except Exception:
                pass
            self._remove_metadata(k, tier=tier)

    @staticmethod
    def _rename_or_copy(src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(src, dst)
        except OSError:
            # Cross-device (tmpfs → disk): copy to .tmp then replace atomically
            tmp_dst = dst.with_suffix(dst.suffix + ".tmp")
            with src.open("rb") as rf, tmp_dst.open("wb") as wf:
                wf.write(rf.read())
            os.replace(tmp_dst, dst)
            src.unlink(missing_ok=True)


    def _demote_files_to_warm(self, key: str) -> None:
        """Move payload + metadata from hot to warm on disk."""
        hot_fp = self._file_for(key, tier="hot")
        warm_fp = self._file_for(key, tier="warm")
        hot_meta = self._metadata_file_for(key, tier="hot")
        warm_meta = self._metadata_file_for(key, tier="warm")
        if hot_fp.exists():
            self._rename_or_copy(hot_fp, warm_fp)
        if hot_meta.exists():
            self._rename_or_copy(hot_meta, warm_meta)

    async def _ensure_hot_capacity(self, need_bytes: int) -> None:
        """Demote LRU hot entries to warm until hot tier can accept need_bytes."""
        if not self.cfg.two_tier:
            return

        to_demote: list[CacheEntry] = []

        async with self.locks():
            target = max(0, self._hot_bytes + need_bytes - self.cfg.hot_max_size_bytes)
            if target <= 0:
                return

            for k, cache_entry in list(self._index.items()):
                if target <= 0:
                    break
                if cache_entry.tier != "hot":
                    continue
                # Re-insert at end temporarily? Better: collect and remove from hot accounting
                to_demote.append(cache_entry)
                self._hot_bytes -= cache_entry.size
                target -= cache_entry.size
                # Mark as warm in index before disk move
                self._index[k] = CacheEntry(
                    key=cache_entry.key,
                    cache_key=cache_entry.cache_key,
                    start=cache_entry.start,
                    size=cache_entry.size,
                    mtime=cache_entry.mtime,
                    tier="warm",
                )
                self._index.move_to_end(k, last=False)  # demoted = coldest warm

        for entry in to_demote:
            try:
                await trio.to_thread.run_sync(self._demote_files_to_warm, entry.key)
            except Exception as e:
                logger.warning(f"Failed to demote hot cache entry {entry.key}: {e}")

        # Warm may now be over budget
        if to_demote:
            await self._evict_lru(0)

    async def _evict_lru(self, need_bytes: int = 0) -> None:
        # Index updates under both _index_lock (via locks()) AND _thread_lock
        # so that sync callers (has(), sync_size_snapshot()) never observe an
        # entry that is mid-eviction.  _thread_lock is held only around the
        # brief in-memory mutations; no I/O takes place under it (disk unlink
        # happens after both locks are released).
        # This mirrors the pattern used by put() which also takes _thread_lock
        # around _index writes to coordinate with has().
        to_unlink: list[str] = []
        tiers: dict[str, Literal["hot", "warm"]] = {}
        evicted = 0

        async with self.locks():
            # Prefer evicting warm; only evict hot if single-tier or still over.
            target = max(0, self._total_bytes + need_bytes - self.cfg.max_size_bytes)

            with self._thread_lock:
                while target > 0 and self._index:
                    # Prefer warm LRU first when two-tier
                    victim_key = None
                    victim_entry = None
                    if self.cfg.two_tier:
                        for k, entry in self._index.items():
                            if entry.tier == "warm":
                                victim_key, victim_entry = k, entry
                                break
                    if victim_key is None:
                        victim_key, victim_entry = next(iter(self._index.items()))

                    assert victim_entry is not None
                    self._index.pop(victim_key)

                    lst = self._by_path.get(victim_entry.cache_key)
                    if lst:
                        idx = bisect_right(lst, victim_entry.start) - 1
                        if idx >= 0 and lst[idx] == victim_entry.start:
                            del lst[idx]
                        if not lst:
                            self._by_path.pop(victim_entry.cache_key, None)

                    to_unlink.append(victim_key)
                    tiers[victim_key] = victim_entry.tier
                    self._total_bytes -= victim_entry.size
                    if victim_entry.tier == "hot":
                        self._hot_bytes -= victim_entry.size
                    target -= victim_entry.size
                    evicted += 1

        if to_unlink:
            await trio.to_thread.run_sync(
                lambda: self._unlink_cache_files(to_unlink, tiers=tiers)
            )
        if evicted:
            self._metrics.record_evictions(evicted)

    async def _evict_ttl(self) -> None:
        ttl = self.cfg.ttl_seconds
        now = time.time()
        to_unlink: list[str] = []
        tiers: dict[str, Literal["hot", "warm"]] = {}

        async with self.locks():
            for k in list(self._index.keys()):
                cache_entry = self._index.get(k)

                if not cache_entry:
                    continue

                if now - cache_entry.mtime > ttl:
                    self._index.pop(k, None)
                    lst = self._by_path.get(cache_entry.cache_key)

                    if lst:
                        idx = bisect_right(lst, cache_entry.start) - 1

                        if idx >= 0 and lst[idx] == cache_entry.start:
                            del lst[idx]

                        if not lst:
                            self._by_path.pop(cache_entry.cache_key, None)

                    self._total_bytes -= cache_entry.size
                    if cache_entry.tier == "hot":
                        self._hot_bytes -= cache_entry.size
                    to_unlink.append(k)
                    tiers[k] = cache_entry.tier

        if to_unlink:
            await trio.to_thread.run_sync(
                lambda: self._unlink_cache_files(to_unlink, tiers=tiers)
            )
            self._metrics.record_evictions(len(to_unlink))

    async def get(self, cache_key: str, start: int, end: int) -> bytes:
        needed_len = max(0, end - start + 1)

        if needed_len == 0:
            return b""

        get_start_time = time.time()
        lock_wait_s = 0.0

        # Do not hold the per-key shard across disk I/O — same title may be
        # opened via multiple VFS paths (library profiles); readers must overlap.
        # Writers serialize via ``put``'s shard lock.

        # Fast path: Try to find a single chunk that contains the entire request
        # This avoids holding the lock during file I/O for the common case
        chunk_key = None
        chunk_file = None
        chunk_start_offset = 0
        chunk_tier: Literal["hot", "warm"] = "warm"

        lock_acquire = time.time()
        async with self.locks():
            lock_wait_s += time.time() - lock_acquire
            s_list = self._by_path.get(cache_key)

            if s_list:
                # Find chunk that might contain start position
                idx = bisect_right(s_list, start) - 1

                if idx >= 0:
                    chunk_start = s_list[idx]
                    cache_entry = self._index.get(self._key(cache_key, chunk_start))

                    if cache_entry:
                        chunk_end = chunk_start + cache_entry.size - 1

                        # Check if this single chunk covers the entire request
                        if start >= chunk_start and end <= chunk_end:
                            # Fast path: single chunk covers entire request
                            chunk_key = self._key(cache_key, chunk_start)
                            chunk_tier = cache_entry.tier
                            chunk_file = self._file_for(chunk_key, tier=chunk_tier)
                            chunk_start_offset = chunk_start
                            # Don't update timestamps yet - do it after successful read

        # Fast path: read single chunk outside the index lock (off trio thread)
        if chunk_key and chunk_file:
            try:
                read_start = time.time()

                # Calculate slice within chunk
                copy_start = start - chunk_start_offset
                copy_end = end - chunk_start_offset
                bytes_to_read = copy_end - copy_start + 1

                result = await trio.to_thread.run_sync(
                    self._read_file_slice,
                    chunk_file,
                    copy_start,
                    bytes_to_read,
                )

                read_time = time.time() - read_start

                if read_time > 0.05:  # Log slow reads (>50ms)
                    logger.warning(
                        f"Slow cache read: {len(result) / (1024 * 1024):.2f}MB in {read_time * 1000:.0f}ms from {chunk_file}"
                    )

                if len(result) == needed_len:
                    # Priority 2: Probabilistic LRU update.
                    # Acquiring the global index lock on *every* cache hit serialises
                    # all concurrent stream reads. Under 6+ simultaneous titles this
                    # causes 100–500ms lock_wait even for /dev/shm reads.
                    # We skip the LRU bookkeeping 90% of the time — LRU ordering
                    # degrades gracefully and the 10s mtime gate already limits
                    # write pressure. Hot entries remain hot; cold entries still
                    # age out on the LRU pass.
                    if random.random() < 0.1:
                        lock_acquire = time.time()
                        async with self.locks():
                            lock_wait_s += time.time() - lock_acquire
                            with self._thread_lock:
                                if chunk_key in self._index:
                                    cache_entry = self._index[chunk_key]
                                    self._index.move_to_end(chunk_key, last=True)

                                    now = time.time()
                                    if now - cache_entry.mtime > 10.0:
                                        self._index[chunk_key] = CacheEntry(
                                            key=cache_entry.key,
                                            cache_key=cache_entry.cache_key,
                                            mtime=now,
                                            start=cache_entry.start,
                                            size=cache_entry.size,
                                            tier=cache_entry.tier,
                                        )

                    self._metrics.record_hit(needed_len)

                    total_time = time.time() - get_start_time

                    if total_time > 0.1:  # Log if cache.get() takes >100ms
                        logger.warning(
                            f"Slow cache.get(): {total_time * 1000:.0f}ms for "
                            f"{needed_len / (1024 * 1024):.2f}MB "
                            f"(read: {read_time * 1000:.0f}ms, "
                            f"lock_wait: {lock_wait_s * 1000:.0f}ms "
                            f"[sampled ~10%])"
                        )

                    return result
            except FileNotFoundError:
                # Chunk file missing, fall through to slow path
                pass

        # Slow path: multi-chunk stitching for cross-chunk boundary requests
        # Plan the read operations while holding the lock, then release it for I/O
        chunks_to_read = list[ChunkInfo]()

        lock_acquire = time.time()
        async with self.locks():
            lock_wait_s += time.time() - lock_acquire
            s_list = self._by_path.get(cache_key)

            if s_list:
                current_pos = start

                while current_pos <= end:
                    # Find chunk that contains current_pos
                    idx = bisect_right(s_list, current_pos) - 1
                    if idx < 0:
                        break  # No chunk starts at or before current_pos

                    chunk_start = s_list[idx]
                    chunk_key = self._key(cache_key, chunk_start)
                    cache_entry = self._index.get(chunk_key)

                    if not cache_entry:
                        break  # Chunk not in index

                    chunk_end = chunk_start + cache_entry.size - 1

                    # Check if this chunk covers current_pos
                    if current_pos < chunk_start or current_pos > chunk_end:
                        break  # Gap in coverage

                    # Calculate what portion of this chunk we need
                    copy_start = max(current_pos, chunk_start) - chunk_start
                    copy_end = min(end, chunk_end) - chunk_start
                    bytes_to_read = copy_end - copy_start + 1

                    # Plan this read operation
                    chunk_file = self._file_for(chunk_key, tier=cache_entry.tier)
                    chunks_to_read.append(
                        ChunkInfo(
                            chunk_key=chunk_key,
                            chunk_ts=cache_entry.mtime,
                            chunk_file=chunk_file,
                            copy_start=copy_start,
                            bytes_to_read=bytes_to_read,
                            chunk_end=chunk_end,
                        )
                    )

                    current_pos = chunk_end + 1

        # Execute reads outside the index lock (off trio thread)
        if chunks_to_read:
            result_data = bytearray()
            chunks_used = list[tuple[str, float]]()

            for chunk_info in chunks_to_read:
                try:
                    chunk_slice = await trio.to_thread.run_sync(
                        self._read_file_slice,
                        chunk_info.chunk_file,
                        chunk_info.copy_start,
                        chunk_info.bytes_to_read,
                    )
                except FileNotFoundError:
                    # Chunk file missing, abort slow path
                    break

                if len(chunk_slice) == chunk_info.bytes_to_read:
                    result_data.extend(chunk_slice)
                    chunks_used.append((chunk_info.chunk_key, chunk_info.chunk_ts))
                else:
                    # Incomplete read, abort slow path
                    break
            else:
                # All chunks read successfully (no break occurred)
                if len(result_data) == needed_len:
                    # Probabilistic LRU: same 10% policy as fast path.
                    if random.random() < 0.1:
                        async with self.locks():
                            with self._thread_lock:
                                now = time.time()

                                for chunk_key, chunk_ts in chunks_used:
                                    if chunk_key in self._index:
                                        self._index.move_to_end(chunk_key, last=True)

                                        if now - chunk_ts > 10.0:
                                            cache_entry = self._index[chunk_key]
                                            self._index[chunk_key] = CacheEntry(
                                                key=cache_entry.key,
                                                mtime=now,
                                                cache_key=cache_entry.cache_key,
                                                start=cache_entry.start,
                                                size=cache_entry.size,
                                                tier=cache_entry.tier,
                                            )

                    self._metrics.record_hit(needed_len)

                    return bytes(result_data)

        # Fallback: Direct probe for exact key on filesystem and rebuild index
        k = self._key(cache_key, start)
        data = None
        found_tier: Literal["hot", "warm"] = "warm"
        for probe_tier in (("hot", "warm") if self.cfg.two_tier else ("warm",)):
            fp = self._file_for(k, tier=probe_tier)  # type: ignore[arg-type]
            data = await trio.to_thread.run_sync(self._read_file_all, fp)
            if data is not None:
                found_tier = probe_tier  # type: ignore[assignment]
                break

        if data is None:
            async with self.locks():
                prev = self._index.pop(k, None)
                if prev and prev.tier == "hot":
                    self._hot_bytes = max(0, self._hot_bytes - prev.size)
                if prev:
                    self._total_bytes = max(0, self._total_bytes - prev.size)

            self._metrics.record_miss()
            # No log for cache misses - reduces noise (misses are expected and normal)
            return b""

        # If we got here but entry was missing in index, rebuild it
        async with self.locks():
            if k not in self._index:
                sz = len(data)
                self._index[k] = CacheEntry(
                    key=k,
                    cache_key=cache_key,
                    start=start,
                    size=sz,
                    mtime=time.time(),
                    tier=found_tier,
                )
                lst = self._by_path.setdefault(cache_key, [])
                insort(lst, start)
                self._total_bytes += sz
                if found_tier == "hot":
                    self._hot_bytes += sz

        if end < start:
            return b""

        length = end - start + 1

        if len(data) >= length:
            self._metrics.record_hit(length)
            return data[:length]

        self._metrics.record_miss()

        return b""

    async def put(self, cache_key: str, start: int, data: bytes) -> None:
        if not data:
            return

        k = self._key(cache_key, start)
        need = len(data)
        write_tier: Literal["hot", "warm"] = (
            "hot" if self.cfg.two_tier else "warm"
        )

        # Shard serializes writers for the same title; index lock stays brief.
        async with self._shard(cache_key):
            if write_tier == "hot":
                await self._ensure_hot_capacity(need)

            if self.cfg.eviction == "TTL":
                await self._evict_ttl()
            else:
                await self._evict_lru(need)

            fp = self._file_for(k, tier=write_tier)

            try:
                await trio.to_thread.run_sync(self._write_file_bytes, fp, data)
                # Write metadata after successful data write (also disk I/O)
                await trio.to_thread.run_sync(
                    lambda: self._write_metadata(k, cache_key, start, tier=write_tier)
                )
            except Exception as e:
                logger.warning(f"Disk cache write failed: {e}")
                return

            # Priority 3: _thread_lock guards _index writes so sync readers
            # (has(), sync_size_snapshot()) see a consistent snapshot without
            # having to wait on the async _index_lock.
            async with self.locks():
                with self._thread_lock:
                    prev = self._index.pop(k, None)

                    if prev:
                        self._total_bytes -= prev.size
                        if prev.tier == "hot":
                            self._hot_bytes -= prev.size
                        lst_prev = self._by_path.get(cache_key)

                        if lst_prev:
                            idx_prev = bisect_right(lst_prev, start) - 1

                            if idx_prev >= 0 and lst_prev[idx_prev] == start:
                                del lst_prev[idx_prev]

                            if not lst_prev:
                                self._by_path.pop(cache_key, None)

                    self._index[k] = CacheEntry(
                        key=k,
                        cache_key=cache_key,
                        start=start,
                        size=need,
                        mtime=time.time(),
                        tier=write_tier,
                    )
                    lst = self._by_path.setdefault(cache_key, [])
                    insort(lst, start)
                    self._total_bytes += need
                    if write_tier == "hot":
                        self._hot_bytes += need
                    self._metrics.record_bytes_written(need)

    def has(self, cache_key: str, start: int, end: int) -> bool:
        """
        Check if the cache contains the full range [start, end] for the given cache_key.

        This uses a thread-safe approach to prevent data races with concurrent writers.
        """

        k = self._key(cache_key, start)

        # Use a separate thread lock to protect _index reads from async writers
        # This avoids the need to make this method async
        with self._thread_lock:
            cache_entry = self._index.get(k)

            if not cache_entry:
                return False

            chunk_end = cache_entry.start + cache_entry.size - 1

            if end > chunk_end:
                return False

            tier = cache_entry.tier

        # Check file existence outside the lock
        fp = self._file_for(k, tier=tier)

        return fp.exists()

    async def trim(self) -> None:
        # Primary policy-based trimming
        if self.cfg.eviction == "TTL":
            await self._evict_ttl()
        else:
            await self._evict_lru()

        # Hard safety net: if our accounting drifted (e.g., external files), rebuild and prune
        try:
            async with self.locks():
                over = self._total_bytes > self.cfg.max_size_bytes

            if over:
                await self._initial_scan()
        except Exception:
            pass

    def sync_size_snapshot(self) -> tuple[int, int]:
        """Thread-safe size/entry snapshot for asyncio callers (e.g. /metrics).

        Must not use ``trio.Lock`` — FastAPI runs under asyncio, while VFS
        cache ops run under trio. Reading under ``_thread_lock`` alone is safe
        for metrics (same inner critical section as ``locks()``).
        """
        with self._thread_lock:
            return int(self._total_bytes), int(len(self._index))

    async def stats(self) -> CacheSnapshot:
        s = self._metrics.snapshot()

        async with self.locks():
            s["total_bytes"] = self._total_bytes
            s["entries"] = len(self._index)

        return s

    async def maybe_log_stats(self) -> None:
        now = time.time()

        if not self.cfg.metrics_enabled:
            return

        if now - self._last_log < 30:  # log at most every 30s
            return

        # Proactive safe trim before logging to keep within caps
        try:
            await self.trim()
        except Exception:
            pass

        self._last_log = now
        stats = await self.stats()
        if self.cfg.metrics_enabled:
            from program.services.streaming import prom_cache_metrics as prom

            prom.set_size_gauges(
                total_bytes=int(stats.get("total_bytes") or 0),
                entries=int(stats.get("entries") or 0),
            )

        logger.log("VFS", f"Cache stats: {stats}")
