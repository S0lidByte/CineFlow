from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import uuid
from bisect import bisect_right, insort
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, Required, TypedDict

import trio
from kink import di
from loguru import logger


class CacheSnapshot(TypedDict):
    hits: Required[int]
    misses: Required[int]
    bytes_from_cache: Required[int]
    bytes_written: Required[int]
    evictions: Required[int]
    total_bytes: NotRequired[int]
    entries: NotRequired[int]


class CacheIndexInconsistencyError(RuntimeError):
    """Raised when internal cache index encounters an inconsistent or missing entry during eviction."""


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

    @property
    def chunk_state(self) -> ChunkState:
        return ChunkState.HOT if self.tier == "hot" else ChunkState.WARM


class ChunkState(str, Enum):
    """Lifecycle state of a media chunk in cache."""

    HOT = "hot"  # In fast RAM/tmpfs tier
    WARM = "warm"  # In persistent disk tier
    EVICTED = "evicted"  # Removed from disk and index


@dataclass
class StreamLease:
    """Active playback lease protecting a chunk from eviction."""

    stream_id: str
    cache_key: str
    start: int
    size: int
    expires_at: float  # monotonic timestamp
    acquired_at: float
    last_touched_at: float


@dataclass(frozen=True)
class ChunkInfo:
    chunk_key: str
    chunk_ts: float
    chunk_tier: Literal["hot", "warm"]
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
        else:
            try:
                from program.services.streaming.telemetry import (
                    playback_telemetry_collector,
                )

                playback_telemetry_collector.record_cache_hit(nbytes=nbytes)
            except Exception:
                pass

    def record_miss(self) -> None:
        with self.lock:
            self.misses += 1
        if self.prom_enabled:
            from program.services.streaming import prom_cache_metrics as prom

            prom.record_miss()
        else:
            try:
                from program.services.streaming.telemetry import (
                    playback_telemetry_collector,
                )

                playback_telemetry_collector.record_cache_miss()
            except Exception:
                pass

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
    _HOT_RESERVATION_TIMEOUT_SECONDS = 5.0

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
        # Hybrid-cache capacity decisions are serialized, but payload writes are
        # not. Pending reservations keep concurrent titles within the hot budget.
        self._hot_write_lock = trio.Lock()
        self._hot_reserved_bytes = 0
        self._hot_capacity_changed = trio.Event()
        self._metrics = Metrics(prom_enabled=cfg.metrics_enabled)
        self._last_log = 0.0  # Initialize last log timestamp
        # FUSE reads may arrive concurrently at the 30-second maintenance
        # boundary. Only one may trim and collect metrics; the rest must keep
        # serving reads rather than queueing behind eviction I/O.
        self._metrics_maintenance_lock = trio.Lock()

        # Active Playback Protection:
        # key (composite chunk_key e.g. "hash_start") -> dict[stream_id, StreamLease]
        self._leases_by_key: dict[str, dict[str, StreamLease]] = {}
        # stream_id -> dict[key, StreamLease]
        self._leases_by_stream: dict[str, dict[str, StreamLease]] = {}
        # key -> active reader refcount (incremented in reading_chunk context manager)
        self._active_readers: dict[str, int] = {}
        # Count of refused evictions when all candidates are protected
        self.eviction_refusals: int = 0

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

        try:
            self._sync_initial_scan()
        except Exception as e:
            logger.debug(f"Disk cache initial scan skipped: {e}")

    def _shard_for(self, cache_key: str) -> trio.Lock:
        # Stable across process lifetime; collisions only map unrelated keys together.
        bucket = (
            int(hashlib.sha1(cache_key.encode()).hexdigest(), 16) % self._SHARD_COUNT
        )
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
        except OSError:
            return None

    @staticmethod
    def _file_has_size(path: Path, expected_size: int) -> bool:
        try:
            return path.stat().st_size >= expected_size
        except OSError:
            return False

    @staticmethod
    def _write_file_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as f:
                f.write(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _is_cache_payload_file(root: Path, path: Path) -> bool:
        """Only identify payloads written in Riven's SHA-1 fanout layout."""
        key = path.name
        return (
            len(key) == 40
            and all(character in "0123456789abcdef" for character in key)
            and path.parent == root / key[:2]
        )

    def _sync_initial_scan(self) -> None:
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

                                        if not self._is_cache_payload_file(root, fp):
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
                            elif (
                                sub.is_file()
                                and sub.suffix != ".meta"
                                and self._is_cache_payload_file(root, sub)
                            ):
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

            with self._thread_lock:
                self._index.clear()
                self._by_path.clear()
                self._total_bytes = 0
                self._hot_bytes = 0

                for cache_entry in entries:
                    # Prefer hot if the same key appears in both (shouldn't normally)
                    existing = self._index.get(cache_entry.key)
                    if (
                        existing
                        and existing.tier == "hot"
                        and cache_entry.tier == "warm"
                    ):
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

    async def _initialize(self) -> None:
        try:
            await self._initial_scan()
        except Exception as e:
            logger.debug(f"Disk cache initial scan skipped: {e}")

    async def _initial_scan(self) -> None:
        async with self.locks():
            self._sync_initial_scan()
        try:
            await self.trim()
        except Exception:
            pass

    def _key(self, path: str, start: int) -> str:
        h = hashlib.sha1(f"{path}|{start}".encode()).hexdigest()
        return h

    def _file_for(self, key: str, *, tier: Literal["hot", "warm"] = "warm") -> Path:
        """Return cache file path without creating directories.

        Read paths (``get`` / ``has``) must not mkdir under the global lock —
        fanout dirs are created only on write via ``_ensure_parent``.
        """
        sub = key[:2]
        if tier == "hot" and self.cfg.hot_dir is not None:
            return self.cfg.hot_dir / sub / key
        return self.cfg.cache_dir / sub / key

    def _tier_probe_order(
        self, preferred: Literal["hot", "warm"]
    ) -> tuple[Literal["hot", "warm"], ...]:
        """Probe both tiers during a hot/warm handoff, retrying the preferred tier."""
        if not self.cfg.two_tier:
            return (preferred,)
        alternate: Literal["hot", "warm"] = "warm" if preferred == "hot" else "hot"
        return (preferred, alternate, preferred)

    async def _read_slice_from_tiers(
        self,
        key: str,
        *,
        preferred: Literal["hot", "warm"],
        offset: int,
        size: int,
    ) -> bytes:
        """Read a complete slice while an entry may be moving between tiers."""
        for tier in self._tier_probe_order(preferred):
            try:
                data = await trio.to_thread.run_sync(
                    self._read_file_slice,
                    self._file_for(key, tier=tier),
                    offset,
                    size,
                )
            except FileNotFoundError:
                continue
            if len(data) == size:
                return data
        return b""

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

    @contextmanager
    def reading_chunk(self, chunk_key: str):
        """Context manager tracking active readers to prevent eviction races."""
        with self._thread_lock:
            self._active_readers[chunk_key] = self._active_readers.get(chunk_key, 0) + 1
        try:
            yield
        finally:
            with self._thread_lock:
                cnt = self._active_readers.get(chunk_key, 1) - 1
                if cnt <= 0:
                    self._active_readers.pop(chunk_key, None)
                else:
                    self._active_readers[chunk_key] = cnt

    def _is_entry_protected(self, key: str, entry: CacheEntry | None = None) -> bool:
        """Check if cache entry is protected by active readers or valid stream leases.

        Must be called while holding self._thread_lock.
        """
        if self._active_readers.get(key, 0) > 0:
            return True

        leases = self._leases_by_key.get(key)
        if not leases:
            return False

        now = time.monotonic()
        active = False
        expired_streams: list[str] = []
        for stream_id, lease in leases.items():
            if lease.expires_at > now:
                active = True
            else:
                expired_streams.append(stream_id)

        # Cleanup expired leases
        for stream_id in expired_streams:
            leases.pop(stream_id, None)
            if stream_id in self._leases_by_stream:
                self._leases_by_stream[stream_id].pop(key, None)
                if not self._leases_by_stream[stream_id]:
                    self._leases_by_stream.pop(stream_id, None)

        if not leases:
            self._leases_by_key.pop(key, None)

        return active

    def acquire_lease(
        self,
        *,
        stream_id: str,
        cache_key: str,
        start: int,
        size: int,
        lease_seconds: float = 60.0,
    ) -> StreamLease:
        """Acquire or renew an active playback lease on a chunk."""
        k = self._key(cache_key, start)
        now = time.monotonic()
        expires_at = now + lease_seconds

        with self._thread_lock:
            lease_dict = self._leases_by_key.setdefault(k, {})
            existing = lease_dict.get(stream_id)
            if existing:
                lease = StreamLease(
                    stream_id=stream_id,
                    cache_key=cache_key,
                    start=start,
                    size=size,
                    expires_at=expires_at,
                    acquired_at=existing.acquired_at,
                    last_touched_at=now,
                )
            else:
                lease = StreamLease(
                    stream_id=stream_id,
                    cache_key=cache_key,
                    start=start,
                    size=size,
                    expires_at=expires_at,
                    acquired_at=now,
                    last_touched_at=now,
                )

            lease_dict[stream_id] = lease
            stream_dict = self._leases_by_stream.setdefault(stream_id, {})
            stream_dict[k] = lease

            if k in self._index:
                self._index.move_to_end(k, last=True)

            return lease

    def release_lease(
        self,
        *,
        stream_id: str,
        cache_key: str,
        start: int,
    ) -> None:
        """Release an active playback lease on a chunk."""
        k = self._key(cache_key, start)
        with self._thread_lock:
            if k in self._leases_by_key:
                self._leases_by_key[k].pop(stream_id, None)
                if not self._leases_by_key[k]:
                    self._leases_by_key.pop(k, None)

            if stream_id in self._leases_by_stream:
                self._leases_by_stream[stream_id].pop(k, None)
                if not self._leases_by_stream[stream_id]:
                    self._leases_by_stream.pop(stream_id, None)

    def release_stream(self, stream_id: str) -> int:
        """Release all active leases held by stream_id. Returns count of released leases."""
        with self._thread_lock:
            leases = self._leases_by_stream.pop(stream_id, {})
            count = len(leases)
            for k in leases:
                if k in self._leases_by_key:
                    self._leases_by_key[k].pop(stream_id, None)
                    if not self._leases_by_key[k]:
                        self._leases_by_key.pop(k, None)
            return count

    def reconcile_stream_playhead(
        self,
        *,
        stream_id: str,
        cache_key: str,
        playhead_byte: int,
        lookback_bytes: int = 16 * 1024 * 1024,
        lookahead_bytes: int = 384 * 1024 * 1024,
        lease_seconds: float = 60.0,
        header_bytes: int = 0,
    ) -> int:
        """Protect chunks within [playhead - lookback, playhead + lookahead] for stream_id.

        If header_bytes > 0, cached container header chunks with start < header_bytes
        are also protected from eviction for the active stream.
        Releases obsolete leases for chunks outside this window.
        Returns count of protected chunks for this stream.
        """
        min_pos = max(0, playhead_byte - lookback_bytes)
        max_pos = playhead_byte + lookahead_bytes
        now = time.monotonic()

        with self._thread_lock:
            # 1. Release existing leases for this stream that are outside [min_pos, max_pos]
            # (preserving header chunks within [0, header_bytes) when header_bytes > 0)
            active_leases = self._leases_by_stream.get(stream_id, {})
            obsolete_keys = [
                k
                for k, lease in active_leases.items()
                if lease.cache_key == cache_key
                and (lease.start < min_pos or lease.start > max_pos)
                and (header_bytes <= 0 or lease.start >= header_bytes)
            ]
            for k in obsolete_keys:
                active_leases.pop(k, None)
                if k in self._leases_by_key:
                    self._leases_by_key[k].pop(stream_id, None)
                    if not self._leases_by_key[k]:
                        self._leases_by_key.pop(k, None)

            # 2. Acquire/renew leases for all chunks within the window for this cache_key
            cached_starts = self._by_path.get(cache_key, [])
            if not cached_starts:
                return len(self._leases_by_stream.get(stream_id, {}))

            def _lease_chunk(chunk_start: int) -> None:
                chunk_key = self._key(cache_key, chunk_start)
                entry = self._index.get(chunk_key)
                if not entry:
                    return
                lease = StreamLease(
                    stream_id=stream_id,
                    cache_key=cache_key,
                    start=chunk_start,
                    size=entry.size,
                    expires_at=now + lease_seconds,
                    acquired_at=now,
                    last_touched_at=now,
                )
                self._leases_by_key.setdefault(chunk_key, {})[stream_id] = lease
                self._leases_by_stream.setdefault(stream_id, {})[chunk_key] = lease
                self._index.move_to_end(chunk_key, last=True)

            # Protect container header chunks if configured
            if header_bytes > 0:
                header_end_idx = bisect_right(cached_starts, header_bytes - 1)
                for i in range(header_end_idx):
                    _lease_chunk(cached_starts[i])

            start_idx = bisect_right(cached_starts, min_pos) - 1
            start_idx = max(start_idx, 0)
            end_idx = bisect_right(cached_starts, max_pos)

            for i in range(start_idx, min(end_idx + 1, len(cached_starts))):
                chunk_start = cached_starts[i]
                chunk_key = self._key(cache_key, chunk_start)
                entry = self._index.get(chunk_key)
                if not entry:
                    continue
                chunk_end = chunk_start + entry.size - 1
                if chunk_end >= min_pos and chunk_start <= max_pos:
                    _lease_chunk(chunk_start)

            return len(self._leases_by_stream.get(stream_id, {}))

    def is_protected(self, cache_key: str, start: int) -> bool:
        """Inspect if a chunk at cache_key/start is currently protected."""
        k = self._key(cache_key, start)
        with self._thread_lock:
            return self._is_entry_protected(k)

    def is_chunk_lease_protected(self, cache_key: str, start: int) -> bool:
        """Alias for is_protected checking if chunk is lease or reader protected."""
        return self.is_protected(cache_key, start)

    @property
    def eviction_refusal_counter(self) -> int:
        """Alias for eviction_refusals counter."""
        return self.eviction_refusals

    def protected_bytes(self) -> int:
        """Return total bytes of currently protected chunks."""
        with self._thread_lock:
            now = time.monotonic()
            protected = 0
            for k, entry in self._index.items():
                if self._active_readers.get(k, 0) > 0:
                    protected += entry.size
                    continue
                leases = self._leases_by_key.get(k)
                if leases and any(l.expires_at > now for l in leases.values()):
                    protected += entry.size
            return protected

    def protected_usage_percentage(self) -> float:
        """Return percentage of total cache capacity currently protected by active playback (0.0 to 100.0)."""
        if self.cfg.max_size_bytes <= 0:
            return 0.0
        return (self.protected_bytes() / self.cfg.max_size_bytes) * 100.0

    async def _ensure_hot_capacity(self, need_bytes: int) -> None:
        """Demote LRU hot entries to warm until hot tier can accept need_bytes."""
        if not self.cfg.two_tier:
            return

        to_demote: list[CacheEntry] = []

        async with self.locks():
            target = max(
                0,
                self._hot_bytes
                + self._hot_reserved_bytes
                + need_bytes
                - self.cfg.hot_max_size_bytes,
            )
            if target <= 0:
                return

            with self._thread_lock:
                # Prefer demoting unprotected hot entries first to preserve active playback in RAM
                unprotected_hot: list[CacheEntry] = []
                protected_hot: list[CacheEntry] = []
                for cache_entry in self._index.values():
                    if cache_entry.tier != "hot":
                        continue
                    if not self._is_entry_protected(cache_entry.key, cache_entry):
                        unprotected_hot.append(cache_entry)
                    else:
                        protected_hot.append(cache_entry)

                for cache_entry in unprotected_hot + protected_hot:
                    if target <= 0:
                        break
                    to_demote.append(cache_entry)
                    target -= cache_entry.size

        for entry in to_demote:
            try:
                await trio.to_thread.run_sync(self._demote_files_to_warm, entry.key)
            except Exception as e:
                logger.warning(f"Failed to demote hot cache entry {entry.key}: {e}")
                continue

            # Publish the tier change only after the warm payload exists. Readers
            # probe both paths around this handoff, so no zero-byte window leaks.
            async with self.locks():
                with self._thread_lock:
                    current = self._index.get(entry.key)
                    if current is None or current.tier != "hot":
                        continue
                    self._index[entry.key] = CacheEntry(
                        key=current.key,
                        cache_key=current.cache_key,
                        start=current.start,
                        size=current.size,
                        mtime=current.mtime,
                        tier="warm",
                    )
                    # NOTE: Do NOT do self._index.move_to_end(entry.key, last=False)!
                    # Demoted chunks must NOT be pushed to the head of the LRU queue,
                    # which caused active playback chunks to be immediately evicted.
                    self._hot_bytes = max(0, self._hot_bytes - current.size)

        # Warm may now be over budget
        if to_demote:
            await self._evict_lru(0)

    async def _reserve_hot_capacity(self, need_bytes: int) -> bool:
        """Reserve hot-tier bytes, returning false when disk fallback is safer."""
        with trio.move_on_after(self._HOT_RESERVATION_TIMEOUT_SECONDS):
            while True:
                async with self._hot_write_lock:
                    await self._ensure_hot_capacity(need_bytes)
                    available = (
                        self.cfg.hot_max_size_bytes
                        - self._hot_bytes
                        - self._hot_reserved_bytes
                    )
                    if need_bytes <= available:
                        self._hot_reserved_bytes += need_bytes
                        return True

                    # With no pending writer to wake us, failure to free enough
                    # space cannot resolve by waiting. Use the warm tier instead.
                    if self._hot_reserved_bytes == 0:
                        return False
                    capacity_changed = self._hot_capacity_changed

                await capacity_changed.wait()

        return False

    async def _release_hot_capacity(self, reserved_bytes: int) -> None:
        """Release a pending hot-tier reservation and wake blocked writers."""
        async with self._hot_write_lock:
            self._hot_reserved_bytes = max(0, self._hot_reserved_bytes - reserved_bytes)
            self._hot_capacity_changed.set()
            self._hot_capacity_changed = trio.Event()

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
        evicted_entries: list[tuple[str, int]] = []

        try:
            async with self.locks():
                # Prefer evicting warm; only evict hot if single-tier or still over.
                target = max(
                    0, self._total_bytes + need_bytes - self.cfg.max_size_bytes
                )

                with self._thread_lock:
                    while target > 0 and self._index:
                        # Prefer oldest unprotected warm entry first when two-tier
                        victim_key: str | None = None
                        if self.cfg.two_tier:
                            for k, entry in self._index.items():
                                if getattr(
                                    entry, "tier", None
                                ) == "warm" and not self._is_entry_protected(k, entry):
                                    victim_key = k
                                    break
                        if victim_key is None:
                            for k, entry in self._index.items():
                                if not self._is_entry_protected(k, entry):
                                    victim_key = k
                                    break

                        if victim_key is None:
                            # ALL entries in cache are currently protected by active playback leases/readers.
                            # ACTIVE PLAYBACK DATA > CACHE RETENTION: Refuse eviction to prevent buffer drops.
                            self.eviction_refusals += 1
                            logger.warning(
                                "Cache LRU eviction refused: all {} entries ({:.1f} MB) are protected by active playback leases/readers. Target was {} bytes.",
                                len(self._index),
                                self._total_bytes / (1024 * 1024),
                                target,
                            )
                            break

                        victim_entry = self._index.get(victim_key)
                        if victim_entry is None:
                            logger.error(
                                "Cache index inconsistency during eviction: "
                                "victim_key={}, victim_entry={}, "
                                "index_len={}, target={}",
                                victim_key,
                                victim_entry,
                                len(self._index),
                                target,
                            )
                            self._index.pop(victim_key, None)
                            raise CacheIndexInconsistencyError(
                                f"Cache index inconsistency during eviction: "
                                f"victim_key={victim_key}, victim_entry={victim_entry}"
                            )

                        self._index.pop(victim_key, None)
                        self._leases_by_key.pop(victim_key, None)
                        self._active_readers.pop(victim_key, None)

                        lst = self._by_path.get(victim_entry.cache_key)
                        if lst:
                            idx = bisect_right(lst, victim_entry.start) - 1
                            if idx >= 0 and lst[idx] == victim_entry.start:
                                del lst[idx]
                            if not lst:
                                self._by_path.pop(victim_entry.cache_key, None)

                        to_unlink.append(victim_key)
                        tiers[victim_key] = victim_entry.tier
                        evicted_entries.append(
                            (victim_entry.cache_key, victim_entry.start)
                        )
                        self._total_bytes -= victim_entry.size
                        if victim_entry.tier == "hot":
                            self._hot_bytes -= victim_entry.size
                        target -= victim_entry.size
                        evicted += 1
        finally:
            if to_unlink:
                await trio.to_thread.run_sync(
                    lambda: self._unlink_cache_files(to_unlink, tiers=tiers)
                )
            if evicted:
                self._metrics.record_evictions(evicted)
            for ck, st in evicted_entries:
                try:
                    from .chunker import ChunkCacheNotifier

                    if ChunkCacheNotifier in di:
                        di[ChunkCacheNotifier].on_chunk_evicted(cache_key=ck, start=st)
                except Exception:
                    pass

    async def _evict_ttl(self) -> None:
        ttl = self.cfg.ttl_seconds
        now = time.time()
        to_unlink: list[str] = []
        tiers: dict[str, Literal["hot", "warm"]] = {}
        evicted_entries: list[tuple[str, int]] = []

        async with self.locks():
            for k in list(self._index.keys()):
                cache_entry = self._index.get(k)

                if not cache_entry:
                    continue

                if now - cache_entry.mtime > ttl:
                    with self._thread_lock:
                        if self._is_entry_protected(k, cache_entry):
                            continue

                        self._index.pop(k, None)
                        self._leases_by_key.pop(k, None)
                        self._active_readers.pop(k, None)
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
                    evicted_entries.append((cache_entry.cache_key, cache_entry.start))

        if to_unlink:
            await trio.to_thread.run_sync(
                lambda: self._unlink_cache_files(to_unlink, tiers=tiers)
            )
            self._metrics.record_evictions(len(to_unlink))
            for ck, st in evicted_entries:
                try:
                    from .chunker import ChunkCacheNotifier

                    if ChunkCacheNotifier in di:
                        di[ChunkCacheNotifier].on_chunk_evicted(cache_key=ck, start=st)
                except Exception:
                    pass

    async def get(
        self,
        cache_key: str,
        start: int,
        end: int,
        *,
        stream_id: str | None = None,
    ) -> bytes:
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
                    # Overlapping discrete fallback ranges may start after a
                    # larger media chunk without covering this request. Walk
                    # backwards until an entry that actually covers the full
                    # request is found instead of letting the newest short
                    # range shadow an older complete chunk.
                    for candidate_idx in range(idx, -1, -1):
                        chunk_start = s_list[candidate_idx]
                        cache_entry = self._index.get(self._key(cache_key, chunk_start))
                        if not cache_entry:
                            continue
                        chunk_end = chunk_start + cache_entry.size - 1

                        # Check if this single chunk covers the entire request
                        if start >= chunk_start and end <= chunk_end:
                            # Fast path: single chunk covers entire request
                            chunk_key = self._key(cache_key, chunk_start)
                            chunk_tier = cache_entry.tier
                            chunk_start_offset = chunk_start
                            # Don't update timestamps yet - do it after successful read
                            break

        # Fast path: read single chunk outside the index lock (off trio thread)
        if chunk_key:
            try:
                read_start = time.time()

                # Calculate slice within chunk
                copy_start = start - chunk_start_offset
                copy_end = end - chunk_start_offset
                bytes_to_read = copy_end - copy_start + 1

                with self.reading_chunk(chunk_key):
                    result = await self._read_slice_from_tiers(
                        chunk_key,
                        preferred=chunk_tier,
                        offset=copy_start,
                        size=bytes_to_read,
                    )

                read_time = time.time() - read_start

                if read_time > 0.05:  # Log slow reads (>50ms)
                    logger.warning(
                        f"Slow cache read: {len(result) / (1024 * 1024):.2f}MB in "
                        f"{read_time * 1000:.0f}ms for {chunk_key} ({chunk_tier} preferred)"
                    )

                if len(result) == needed_len:
                    if stream_id is not None:
                        self.acquire_lease(
                            stream_id=stream_id,
                            cache_key=cache_key,
                            start=chunk_start_offset,
                            size=needed_len,
                        )

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

                    covering: tuple[int, str, CacheEntry, int] | None = None
                    for candidate_idx in range(idx, -1, -1):
                        chunk_start = s_list[candidate_idx]
                        chunk_key = self._key(cache_key, chunk_start)
                        cache_entry = self._index.get(chunk_key)
                        if not cache_entry:
                            continue
                        chunk_end = chunk_start + cache_entry.size - 1
                        if chunk_start <= current_pos <= chunk_end and (
                            covering is None or chunk_end > covering[3]
                        ):
                            covering = (
                                chunk_start,
                                chunk_key,
                                cache_entry,
                                chunk_end,
                            )

                    if covering is None:
                        break  # Gap in coverage

                    chunk_start, chunk_key, cache_entry, chunk_end = covering

                    # Calculate what portion of this chunk we need
                    copy_start = max(current_pos, chunk_start) - chunk_start
                    copy_end = min(end, chunk_end) - chunk_start
                    bytes_to_read = copy_end - copy_start + 1

                    # Plan this read operation
                    chunks_to_read.append(
                        ChunkInfo(
                            chunk_key=chunk_key,
                            chunk_ts=cache_entry.mtime,
                            chunk_tier=cache_entry.tier,
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
                with self.reading_chunk(chunk_info.chunk_key):
                    chunk_slice = await self._read_slice_from_tiers(
                        chunk_info.chunk_key,
                        preferred=chunk_info.chunk_tier,
                        offset=chunk_info.copy_start,
                        size=chunk_info.bytes_to_read,
                    )

                if len(chunk_slice) == chunk_info.bytes_to_read:
                    result_data.extend(chunk_slice)
                    chunks_used.append((chunk_info.chunk_key, chunk_info.chunk_ts))
                    if stream_id is not None:
                        entry = self._index.get(chunk_info.chunk_key)
                        if entry:
                            self.acquire_lease(
                                stream_id=stream_id,
                                cache_key=cache_key,
                                start=entry.start,
                                size=entry.size,
                            )
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

        # Fallback: Direct probe for chunk files on filesystem and rebuild index
        found_data: bytes | None = None
        found_tier: Literal["hot", "warm"] = "warm"
        found_key: str = ""
        found_start: int = start

        # Probe candidate chunk start boundaries: exact start, common alignments, or header (0)
        candidate_starts = [start]
        if start > 0:
            for cand_chunk_sz in (
                8 * 1024 * 1024,
                16 * 1024 * 1024,
                4 * 1024 * 1024,
                32 * 1024 * 1024,
                1 * 1024 * 1024,
                2 * 1024 * 1024,
            ):
                aligned_start = start - (start % cand_chunk_sz)
                if aligned_start not in candidate_starts:
                    candidate_starts.append(aligned_start)
            if 0 not in candidate_starts and start < 64 * 1024 * 1024:
                candidate_starts.append(0)

        for cand_start in candidate_starts:
            cand_k = self._key(cache_key, cand_start)
            for probe_tier in ("hot", "warm") if self.cfg.two_tier else ("warm",):
                fp = self._file_for(cand_k, tier=probe_tier)  # type: ignore[arg-type]
                cand_data = await trio.to_thread.run_sync(self._read_file_all, fp)
                if cand_data is not None:
                    # Verify metadata sidecar if present
                    meta = self._read_metadata(cand_k, tier=probe_tier)
                    if meta is not None:
                        meta_ck, meta_start = meta
                        if meta_ck != cache_key or meta_start != cand_start:
                            continue
                    # Check if this candidate chunk covers the requested read range [start, end]
                    cand_end = cand_start + len(cand_data) - 1
                    if cand_start <= start and cand_end >= end:
                        found_data = cand_data
                        found_tier = probe_tier
                        found_key = cand_k
                        found_start = cand_start
                        break
            if found_data is not None:
                break

        if found_data is None:
            async with self.locks():
                prev = self._index.pop(self._key(cache_key, start), None)
                if prev and prev.tier == "hot":
                    self._hot_bytes = max(0, self._hot_bytes - prev.size)
                if prev:
                    self._total_bytes = max(0, self._total_bytes - prev.size)

            self._metrics.record_miss()
            return b""

        # Rebuild index entry from discovered chunk
        async with self.locks():
            if found_key not in self._index:
                sz = len(found_data)
                self._index[found_key] = CacheEntry(
                    key=found_key,
                    cache_key=cache_key,
                    start=found_start,
                    size=sz,
                    mtime=time.time(),
                    tier=found_tier,
                )
                lst = self._by_path.setdefault(cache_key, [])
                if found_start not in lst:
                    insort(lst, found_start)
                self._total_bytes += sz
                if found_tier == "hot":
                    self._hot_bytes += sz

        if stream_id is not None:
            self.acquire_lease(
                stream_id=stream_id,
                cache_key=cache_key,
                start=found_start,
                size=len(found_data),
            )

        if end < start:
            return b""

        chunk_end = found_start + len(found_data) - 1
        copy_start = start - found_start
        copy_end = min(end, chunk_end) - found_start
        slice_len = copy_end - copy_start + 1

        if slice_len == needed_len and len(found_data) >= copy_start + slice_len:
            self._metrics.record_hit(slice_len)
            return found_data[copy_start : copy_start + slice_len]

        self._metrics.record_miss()

        return b""

    async def put(
        self,
        cache_key: str,
        start: int,
        data: bytes,
        *,
        stream_id: str | None = None,
        lease_seconds: float = 60.0,
    ) -> None:
        if not data:
            return

        k = self._key(cache_key, start)
        need = len(data)
        write_tier: Literal["hot", "warm"] = (
            "hot"
            if self.cfg.two_tier and need <= self.cfg.hot_max_size_bytes
            else "warm"
        )

        # Shard serializes writers for the same title; index lock stays brief.
        async with self._shard(cache_key):
            # A discrete scan/fallback can begin at the same offset as a full
            # media chunk. Never replace a complete payload with a shorter
            # overlapping payload: that would turn a ready chunk into a miss.
            with self._thread_lock:
                existing = self._index.get(k)
                existing_size = existing.size if existing else 0
                existing_tier = existing.tier if existing else write_tier

            existing_is_complete = False
            if existing_size >= need:
                for probe_tier in self._tier_probe_order(existing_tier):
                    if await trio.to_thread.run_sync(
                        self._file_has_size,
                        self._file_for(k, tier=probe_tier),
                        existing_size,
                    ):
                        existing_is_complete = True
                        break

            if existing_is_complete:
                if stream_id is not None:
                    self.acquire_lease(
                        stream_id=stream_id,
                        cache_key=cache_key,
                        start=start,
                        size=existing_size,
                        lease_seconds=lease_seconds,
                    )
                return

            hot_reservation = 0
            if write_tier == "hot":
                if await self._reserve_hot_capacity(need):
                    hot_reservation = need
                else:
                    write_tier = "warm"

            try:
                if self.cfg.eviction == "TTL":
                    await self._evict_ttl()
                else:
                    await self._evict_lru(need)

                fp = self._file_for(k, tier=write_tier)

                try:
                    await trio.to_thread.run_sync(self._write_file_bytes, fp, data)
                    # Write metadata after successful data write (also disk I/O)
                    await trio.to_thread.run_sync(
                        lambda: self._write_metadata(
                            k, cache_key, start, tier=write_tier
                        )
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

                if stream_id is not None:
                    self.acquire_lease(
                        stream_id=stream_id,
                        cache_key=cache_key,
                        start=start,
                        size=need,
                        lease_seconds=lease_seconds,
                    )
            finally:
                if hot_reservation:
                    await self._release_hot_capacity(hot_reservation)

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

        # Check complete payload visibility outside the lock. Existence alone
        # is insufficient while recovering old non-atomic writes or external
        # cache damage; readers must never classify a truncated chunk as ready.
        return any(
            self._file_has_size(self._file_for(k, tier=probe_tier), cache_entry.size)
            for probe_tier in self._tier_probe_order(tier)
        )

    async def trim(self) -> None:
        # Primary policy-based trimming
        if self.cfg.eviction == "TTL":
            await self._evict_ttl()
        else:
            await self._evict_lru()

    def sync_size_snapshot(self) -> tuple[int, int]:
        """Thread-safe size/entry snapshot for asyncio callers (e.g. /metrics).

        Must not use ``trio.Lock`` — FastAPI runs under asyncio, while VFS
        cache ops run under trio. Reading under ``_thread_lock`` alone is safe
        for metrics (same inner critical section as ``locks()``).
        """
        with self._thread_lock:
            return int(self._total_bytes), int(len(self._index))

    @property
    def usage_percentage(self) -> float:
        """Percentage of cache capacity currently used (0.0 to 100.0)."""
        if not self.cfg or self.cfg.max_size_bytes <= 0:
            return 0.0
        total_bytes, _ = self.sync_size_snapshot()
        return min(100.0, (total_bytes / self.cfg.max_size_bytes) * 100.0)

    async def stats(self) -> CacheSnapshot:
        s = self._metrics.snapshot()

        async with self.locks():
            s["total_bytes"] = self._total_bytes
            s["entries"] = len(self._index)

        return s

    async def maybe_log_stats(self) -> None:
        if not self.cfg.metrics_enabled:
            return

        now = time.time()
        if now - self._last_log < 30:  # log at most every 30s
            return

        # Do not make ordinary FUSE reads wait for periodic eviction or stats.
        # ``acquire_nowait`` has no checkpoint, so concurrent callers either
        # become the sole maintenance worker or return immediately.
        try:
            self._metrics_maintenance_lock.acquire_nowait()
        except trio.WouldBlock:
            return

        try:
            # A prior caller may have completed while this task was scheduled.
            if now - self._last_log < 30:
                return

            # Proactive safe trim before logging to keep within caps.
            try:
                await self.trim()
            except Exception:
                pass

            self._last_log = now
            stats = await self.stats()
            from program.services.streaming import prom_cache_metrics as prom

            prom.set_size_gauges(
                total_bytes=int(stats.get("total_bytes") or 0),
                entries=int(stats.get("entries") or 0),
            )

            logger.log("VFS", f"Cache stats: {stats}")
        finally:
            self._metrics_maintenance_lock.release()
