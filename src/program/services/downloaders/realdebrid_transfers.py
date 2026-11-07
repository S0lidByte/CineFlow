"""Real-Debrid transfer monitoring, correlation, and safe management service.

Provides cached access to Real-Debrid transfer lists and active counts, correlates transfers
with CineFlow MediaItems, Streams, and OperationLedger outbox records, distinguishes
Real-Debrid downloaded from CineFlow VFS-ingested status, and provides fenced cancellation
and retry for CineFlow-managed operations.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from RTN import Torrent, parse
from sqlalchemy import func, select, update

from program.contracts.dispatcher import (
    notify_outbox_dispatcher,
    publish_outbox_lifecycle_event,
)
from program.contracts.operation_ledger import OperationLedger, enqueue_operation
from program.db.db import db_session
from program.media.item import MediaItem
from program.media.models import ActiveStream
from program.media.state import States
from program.media.stream import Stream
from program.services.downloaders.realdebrid import (
    RealDebridDownloader,
    RealDebridError,
    RealDebridTransientError,
)
from program.settings import settings_manager
from program.types import Event
from program.utils.request import CircuitBreakerOpen
from program.utils.torrent import extract_infohash


class RealDebridTransferFile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    path: str
    bytes: int
    selected: int


class RealDebridTransferItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    hash: str
    bytes: int
    progress: float
    status: str
    added: str
    ended: str | None = None
    speed: int | None = None
    seeders: int | None = None
    links: list[str] = Field(default_factory=lambda: cast(list[str], []))

    # CineFlow correlation fields
    is_cineflow_managed: bool = False
    media_item_id: int | None = None
    media_title: str | None = None
    media_type: str | None = None
    media_state: str | None = None
    ready_in_cineflow: bool = False
    operation_id: str | None = None
    can_cancel: bool = False
    can_retry: bool = False


class RealDebridTransferDetail(RealDebridTransferItem):
    original_filename: str = ""
    split: int = 0
    files: list[RealDebridTransferFile] = Field(
        default_factory=lambda: cast(list[RealDebridTransferFile], [])
    )


class RealDebridTransferStats(BaseModel):
    total: int = 0
    active: int = 0
    queued: int = 0
    downloaded: int = 0
    failed: int = 0
    active_slots_used: int = 0
    active_slots_limit: int = 20


class RealDebridTransfersResponse(BaseModel):
    transfers: list[RealDebridTransferItem] = Field(
        default_factory=lambda: cast(list[RealDebridTransferItem], [])
    )
    stats: RealDebridTransferStats = Field(default_factory=RealDebridTransferStats)
    rd_enabled: bool = True
    error: str | None = None
    cached_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TransferActionResponse(BaseModel):
    success: bool
    message: str
    transfer_id: str


def _get_rd_downloader() -> RealDebridDownloader | None:
    """Retrieve initialized RealDebridDownloader from DI or settings."""
    rd_settings = settings_manager.settings.downloaders.real_debrid
    if not rd_settings.enabled or not rd_settings.api_key:
        return None

    try:
        from kink import di

        from program.program import Program

        if Program in di:
            program = di[Program]
            if (
                program
                and program.services
                and program.services.downloader
                and program.services.downloader.services
            ):
                rd = program.services.downloader.services.get(RealDebridDownloader)
                if isinstance(rd, RealDebridDownloader) and rd.initialized:
                    return rd
    except Exception as e:
        logger.debug(f"DI lookup for RealDebridDownloader failed: {e}")

    # Fallback to standalone validated instance
    try:
        rd = RealDebridDownloader()
        if rd.initialized:
            return rd
    except Exception as e:
        logger.debug(f"Fallback RealDebridDownloader creation failed: {e}")

    return None


class RealDebridTransfersService:
    """Service providing cached, correlated monitoring and management of Real-Debrid transfers."""

    CACHE_TTL_SECONDS: float = 5.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: (
            tuple[
                list[RealDebridTransferItem],
                RealDebridTransferStats,
                str | None,
                datetime,
            ]
            | None
        ) = None
        self._cache_timestamp: float = 0.0

    def invalidate_cache(self) -> None:
        """Invalidate the in-memory cache to force an immediate refresh on next call."""
        with self._lock:
            self._cache = None
            self._cache_timestamp = 0.0

    def get_transfers(
        self,
        force_refresh: bool = False,
        status: str | None = None,
        search: str | None = None,
    ) -> RealDebridTransfersResponse:
        """
        Fetch transfer items and summary statistics, respecting short TTL cache and rate limits.
        """
        rd_settings = settings_manager.settings.downloaders.real_debrid
        if not rd_settings.enabled:
            return RealDebridTransfersResponse(
                transfers=[],
                stats=RealDebridTransferStats(),
                rd_enabled=False,
                error="Real-Debrid downloader is not enabled in settings.",
                cached_at=datetime.now(UTC),
            )
        if not rd_settings.api_key:
            return RealDebridTransfersResponse(
                transfers=[],
                stats=RealDebridTransferStats(),
                rd_enabled=False,
                error="Real-Debrid API key is not configured.",
                cached_at=datetime.now(UTC),
            )

        cached_err: str | None = None
        cached_at: datetime = datetime.now(UTC)

        with self._lock:
            now_mono = time.monotonic()
            if (
                not force_refresh
                and self._cache is not None
                and (now_mono - self._cache_timestamp) < self.CACHE_TTL_SECONDS
            ):
                all_items, stats, cached_err, cached_at = self._cache
            else:
                rd = _get_rd_downloader()
                if not rd:
                    return RealDebridTransfersResponse(
                        transfers=[],
                        stats=RealDebridTransferStats(),
                        rd_enabled=True,
                        error="Real-Debrid downloader is not initialized or invalid credentials.",
                        cached_at=datetime.now(UTC),
                    )

                try:
                    raw_torrents = rd.get_torrents(limit=100)
                    try:
                        active_info = rd.get_active_count()
                    except Exception as e:
                        logger.debug(f"Failed to fetch activeCount: {e}")
                        active_info = {"nb": 0, "limit": 20}

                    all_items, stats = self._correlate_and_build(
                        raw_torrents, active_info
                    )
                    cached_err = None
                    cached_at = datetime.now(UTC)
                    self._cache = (all_items, stats, cached_err, cached_at)
                    self._cache_timestamp = now_mono
                except CircuitBreakerOpen:
                    if self._cache is not None:
                        all_items, stats, _, cached_at = self._cache
                        cached_err = "Real-Debrid circuit breaker is OPEN; serving cached transfers."
                    else:
                        return RealDebridTransfersResponse(
                            transfers=[],
                            stats=RealDebridTransferStats(),
                            rd_enabled=True,
                            error="Real-Debrid circuit breaker is OPEN. Please retry in a few moments.",
                            cached_at=datetime.now(UTC),
                        )
                except RealDebridTransientError as e:
                    if self._cache is not None:
                        all_items, stats, _, cached_at = self._cache
                        cached_err = f"Real-Debrid transient failure ({e}); serving cached transfers."
                    else:
                        return RealDebridTransfersResponse(
                            transfers=[],
                            stats=RealDebridTransferStats(),
                            rd_enabled=True,
                            error=f"Real-Debrid service temporarily unavailable: {e}",
                            cached_at=datetime.now(UTC),
                        )
                except Exception as e:
                    logger.error(f"Error fetching Real-Debrid transfers: {e}")
                    if self._cache is not None:
                        all_items, stats, _, cached_at = self._cache
                        cached_err = f"Failed to refresh transfers ({e}); serving cached transfers."
                    else:
                        return RealDebridTransfersResponse(
                            transfers=[],
                            stats=RealDebridTransferStats(),
                            rd_enabled=True,
                            error=f"Failed to fetch Real-Debrid transfers: {e}",
                            cached_at=datetime.now(UTC),
                        )

        # Apply status filtering in-memory
        filtered = all_items
        if status:
            s = status.lower().strip()
            if s in ("active", "downloading"):
                filtered = [
                    i
                    for i in filtered
                    if i.status in ("downloading", "compressing", "uploading")
                    or (i.speed is not None and i.speed > 0)
                ]
            elif s == "queued":
                filtered = [
                    i
                    for i in filtered
                    if i.status in ("queued", "waiting_files_selection")
                ]
            elif s in ("downloaded", "completed"):
                filtered = [i for i in filtered if i.status == "downloaded"]
            elif s in ("failed", "error"):
                filtered = [
                    i
                    for i in filtered
                    if i.status in ("error", "magnet_error", "virus", "dead")
                ]

        # Apply search filtering in-memory
        if search:
            q = search.lower().strip()
            filtered = [
                i
                for i in filtered
                if (q in i.filename.lower())
                or (i.hash and q in i.hash.lower())
                or (i.media_title and q in i.media_title.lower())
                or (q in i.id.lower())
            ]

        return RealDebridTransfersResponse(
            transfers=filtered,
            stats=stats,
            rd_enabled=True,
            error=cached_err,
            cached_at=cached_at,
        )

    def _correlate_and_build(
        self,
        raw_torrents: list[dict[str, Any]],
        active_info: dict[str, int],
    ) -> tuple[list[RealDebridTransferItem], RealDebridTransferStats]:
        """Correlate Real-Debrid torrent records with database entities and build DTOs."""
        hashes = {str(t.get("hash", "")).lower() for t in raw_torrents if t.get("hash")}
        tids = {str(t.get("id", "")) for t in raw_torrents if t.get("id")}

        item_map_by_hash: dict[str, MediaItem] = {}
        item_map_by_tid: dict[str, MediaItem] = {}
        op_map: dict[int, OperationLedger] = {}

        with db_session() as session:
            # Query Stream records matching these torrent infohashes
            if hashes:
                streams_stmt = select(Stream).filter(
                    func.lower(Stream.infohash).in_(hashes)
                )
                db_streams = session.execute(streams_stmt).scalars().all()
                for s in db_streams:
                    shash = s.infohash.lower()
                    if s.parents:
                        for p in s.parents:
                            if (
                                p.active_stream
                                and p.active_stream.infohash.lower() == shash
                            ):
                                item_map_by_hash[shash] = p
                                if p.active_stream.id:
                                    item_map_by_tid[str(p.active_stream.id)] = p
                                break
                        if shash not in item_map_by_hash and s.parents:
                            item_map_by_hash[shash] = s.parents[0]

            # Also query MediaItems directly with active streams to correlate by active_stream ID or hash
            active_items = (
                session.execute(
                    select(MediaItem).filter(MediaItem.active_stream.is_not(None))
                )
                .scalars()
                .all()
            )
            for m_item in active_items:
                if m_item.active_stream:
                    ashash = str(getattr(m_item.active_stream, "infohash", "")).lower()
                    asid = str(getattr(m_item.active_stream, "id", ""))
                    if ashash and (ashash in hashes or ashash not in item_map_by_hash):
                        item_map_by_hash[ashash] = m_item
                    if asid and (asid in tids or asid not in item_map_by_tid):
                        item_map_by_tid[asid] = m_item

            # Query operation ledger for any linked media item IDs
            media_item_ids = {
                item.id
                for item in set(item_map_by_hash.values())
                | set(item_map_by_tid.values())
            }
            if media_item_ids:
                ops_stmt = (
                    select(OperationLedger)
                    .filter(OperationLedger.media_item_id.in_(media_item_ids))
                    .order_by(OperationLedger.created_at.desc())
                )
                ops = session.execute(ops_stmt).scalars().all()
                for op in ops:
                    if op.media_item_id is not None and op.media_item_id not in op_map:
                        op_map[op.media_item_id] = op

            items: list[RealDebridTransferItem] = []
            for t in raw_torrents:
                tid = str(t.get("id", ""))
                thash = str(t.get("hash", "")).lower()
                status = str(t.get("status", "")).lower()
                speed = t.get("speed")
                seeders = t.get("seeders")

                matched_item = item_map_by_tid.get(tid) or item_map_by_hash.get(thash)
                is_managed = matched_item is not None
                media_id = matched_item.id if matched_item else None

                title = None
                m_type = None
                m_state = None
                ready_in_cf = False

                if matched_item:
                    title = (
                        getattr(matched_item, "title", None)
                        or getattr(matched_item, "parsed_title", None)
                        or getattr(matched_item, "name", None)
                        or f"Media #{matched_item.id}"
                    )
                    m_type = getattr(matched_item, "type", None)
                    raw_state = getattr(matched_item, "state", None)
                    m_state = None
                    if raw_state is not None:
                        state_val = (
                            raw_state.value
                            if hasattr(raw_state, "value")
                            else str(raw_state)
                        )
                        m_state = state_val
                        # Distinguish RD Downloaded vs Ready in CineFlow:
                        # An item is Ready in CineFlow ONLY when RD download is complete AND
                        # CineFlow has ingested it into VFS (Downloaded, Completed, or Symlinked state)
                        if status == "downloaded":
                            if (
                                state_val
                                in (
                                    States.Downloaded.value,
                                    States.Completed.value,
                                    States.Symlinked.value,
                                )
                                or len(getattr(matched_item, "filesystem_entries", []))
                                > 0
                            ):
                                ready_in_cf = True

                linked_op = op_map.get(media_id) if media_id else None
                op_id = linked_op.id if linked_op else None

                # Safe controls:
                # 1. Cancellation: active CineFlow-managed transfers only, NOT completed/downloaded
                can_cancel = is_managed and status not in (
                    "downloaded",
                    "error",
                    "magnet_error",
                    "virus",
                    "dead",
                )
                # 2. Retry: failed CineFlow-managed transfers or failed operation outbox
                can_retry = is_managed and (
                    status in ("error", "magnet_error", "virus", "dead")
                    or (linked_op is not None and linked_op.status == "failed")
                )

                item_dto = RealDebridTransferItem(
                    id=tid,
                    filename=str(t.get("filename", "") or "Unknown"),
                    hash=thash,
                    bytes=int(t.get("bytes", 0) or 0),
                    progress=float(t.get("progress", 0.0) or 0.0),
                    status=status,
                    added=str(t.get("added", "") or ""),
                    ended=t.get("ended"),
                    speed=speed,
                    seeders=seeders,
                    links=list(t.get("links", []) or []),
                    is_cineflow_managed=is_managed,
                    media_item_id=media_id,
                    media_title=title,
                    media_type=m_type,
                    media_state=m_state,
                    ready_in_cineflow=ready_in_cf,
                    operation_id=op_id,
                    can_cancel=can_cancel,
                    can_retry=can_retry,
                )
                items.append(item_dto)

        # Calculate statistics
        total = len(items)
        active = sum(
            1
            for i in items
            if i.status in ("downloading", "compressing", "uploading")
            or (i.speed is not None and i.speed > 0)
        )
        queued = sum(
            1 for i in items if i.status in ("queued", "waiting_files_selection")
        )
        downloaded = sum(1 for i in items if i.status == "downloaded")
        failed = sum(
            1 for i in items if i.status in ("error", "magnet_error", "virus", "dead")
        )

        slots_used = active_info.get("nb", active)
        slots_limit = active_info.get("limit", 20)

        stats = RealDebridTransferStats(
            total=total,
            active=active,
            queued=queued,
            downloaded=downloaded,
            failed=failed,
            active_slots_used=slots_used,
            active_slots_limit=slots_limit,
        )

        return items, stats

    def get_transfer_detail(self, transfer_id: str) -> RealDebridTransferDetail:
        """Fetch complete details for a single transfer including file list."""
        rd = _get_rd_downloader()
        if not rd:
            raise HTTPException(
                status_code=503,
                detail="Real-Debrid downloader is not configured or unavailable",
            )

        try:
            info = rd.get_torrent_info(transfer_id)
        except RealDebridError as e:
            raise HTTPException(
                status_code=404, detail=f"Transfer not found on Real-Debrid: {e}"
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch transfer info: {e}"
            )

        # Correlate with database
        files = [
            RealDebridTransferFile(
                id=f.id,
                path=f.path,
                bytes=f.bytes,
                selected=f.selected,
            )
            for f in info.files.values()
        ]

        # Use transfers list correlation
        summary = self.get_transfers()
        item_match = next((t for t in summary.transfers if t.id == transfer_id), None)

        return RealDebridTransferDetail(
            id=str(info.id),
            filename=str(info.name or "Unknown"),
            original_filename=str(info.alternative_filename or ""),
            hash=str(info.infohash or ""),
            bytes=int(info.bytes or 0),
            progress=float(info.progress or 0.0),
            status=str(info.status or "unknown"),
            added=info.created_at.isoformat() if info.created_at else "",
            ended=item_match.ended if item_match else None,
            speed=item_match.speed if item_match else None,
            seeders=item_match.seeders if item_match else None,
            links=list(info.links or []),
            files=files,
            is_cineflow_managed=item_match.is_cineflow_managed if item_match else False,
            media_item_id=item_match.media_item_id if item_match else None,
            media_title=item_match.media_title if item_match else None,
            media_type=item_match.media_type if item_match else None,
            media_state=item_match.media_state if item_match else None,
            ready_in_cineflow=item_match.ready_in_cineflow if item_match else False,
            operation_id=item_match.operation_id if item_match else None,
            can_cancel=item_match.can_cancel if item_match else False,
            can_retry=item_match.can_retry if item_match else False,
        )

    def cancel_transfer(self, transfer_id: str) -> TransferActionResponse:
        """
        Safely cancel an active CineFlow-managed transfer.

        Deletes the torrent from Real-Debrid, marks associated outbox operations cancelled,
        cleans up MediaItem active stream reference, and cancels in-flight jobs.
        """
        # 1. Fetch current status & correlation
        detail = self.get_transfer_detail(transfer_id)

        # 2. Safety: only CineFlow-managed transfers may be cancelled
        if not detail.is_cineflow_managed:
            raise HTTPException(
                status_code=403,
                detail="Cannot cancel unmanaged or external Real-Debrid transfer.",
            )

        # 3. Safety: do not automatically delete completed Real-Debrid torrents
        if detail.status == "downloaded":
            raise HTTPException(
                status_code=400,
                detail="Cannot cancel a completed Real-Debrid transfer. Deletion of completed torrents is prohibited.",
            )

        rd = _get_rd_downloader()
        if not rd:
            raise HTTPException(
                status_code=503, detail="Real-Debrid downloader unavailable."
            )

        try:
            rd.delete_torrent(transfer_id)
        except Exception as e:
            logger.warning(f"Real-Debrid delete_torrent failed for {transfer_id}: {e}")
            raise HTTPException(
                status_code=502,
                detail=f"Real-Debrid could not delete this transfer: {e}",
            ) from e

        # Fence outbox operations and reset media state
        now = datetime.now(UTC)
        media_item_id = detail.media_item_id
        with db_session() as session:
            if media_item_id:
                query = select(OperationLedger).where(
                    OperationLedger.media_item_id == media_item_id,
                    OperationLedger.status.in_(["pending", "processing"]),
                )
                if detail.operation_id:
                    query = query.where(OperationLedger.id == detail.operation_id)
                active_operations = session.execute(query).scalars().all()

                for operation in active_operations:
                    operation.status = "failed"
                    operation.error_classification = "CANCELLED"
                    operation.error_message = f"Transfer {transfer_id} cancelled by user via Transfers Activity"
                    operation.completed_at = now
                    operation.updated_at = now

                # Reset media active stream only if it still references this transfer.
                item = session.get(MediaItem, media_item_id)
                if item and item.active_stream:
                    if (
                        str(getattr(item.active_stream, "id", "")) == str(transfer_id)
                        or str(getattr(item.active_stream, "infohash", "")).lower()
                        == detail.hash.lower()
                    ):
                        item.active_stream = None
                        if item.last_state in (States.Scraped, States.Failed):
                            item.last_state = States.Indexed

            session.commit()

        # 6. Cancel in-flight jobs in EventManager
        try:
            from kink import di

            from program.program import Program

            if Program in di and media_item_id:
                di[Program].em.cancel_job(media_item_id)
        except Exception as e:
            logger.debug(f"Failed to cancel em job for item {media_item_id}: {e}")

        # 7. Notify dispatcher and publish lifecycle event
        notify_outbox_dispatcher()
        publish_outbox_lifecycle_event(
            "operation_cancelled",
            {
                "transfer_id": transfer_id,
                "media_item_id": media_item_id,
                "reason": "Cancelled by user via Transfers Activity",
            },
        )

        # 8. Invalidate cache
        self.invalidate_cache()

        return TransferActionResponse(
            success=True,
            message=f"Transfer {transfer_id} cancelled successfully.",
            transfer_id=transfer_id,
        )

    def retry_transfer(self, transfer_id: str) -> TransferActionResponse:
        """
        Safely retry a failed CineFlow-managed transfer.
        """
        detail = self.get_transfer_detail(transfer_id)

        if not detail.is_cineflow_managed:
            raise HTTPException(
                status_code=403,
                detail="Cannot retry unmanaged or external Real-Debrid transfer.",
            )

        if detail.status == "downloaded":
            raise HTTPException(
                status_code=400,
                detail="Transfer has already completed.",
            )

        media_item_id = detail.media_item_id
        now = datetime.now(UTC)

        # 1. If an outbox operation failed, retry it
        with db_session() as session:
            if media_item_id:
                retry_stmt = (
                    update(OperationLedger)
                    .where(
                        OperationLedger.media_item_id == media_item_id,
                        OperationLedger.status == "failed",
                    )
                    .values(
                        status="pending",
                        scheduled_at=now,
                        lease_expires_at=None,
                        worker_id=None,
                        claim_token=None,
                        started_at=None,
                        completed_at=None,
                        error_classification=None,
                        error_message=None,
                        updated_at=now,
                    )
                )
                session.execute(retry_stmt)
                session.commit()

        # 2. Trigger EventManager retry for media item
        try:
            from kink import di

            from program.program import Program

            if Program in di and media_item_id:
                di[Program].em.add_event(Event("RetryItem", media_item_id))
        except Exception as e:
            logger.debug(f"Failed to emit RetryItem for #{media_item_id}: {e}")

        notify_outbox_dispatcher()
        publish_outbox_lifecycle_event(
            "operation_retrying",
            {"transfer_id": transfer_id, "media_item_id": media_item_id},
        )

        self.invalidate_cache()

        return TransferActionResponse(
            success=True,
            message=f"Transfer {transfer_id} re-queued for processing.",
            transfer_id=transfer_id,
        )

    def submit_transfer(
        self,
        magnet_or_infohash: str,
        media_item_id: int | None = None,
    ) -> TransferActionResponse:
        """
        Submit a magnet or infohash to Real-Debrid for cloud caching, and associate with CineFlow.
        """
        rd = _get_rd_downloader()
        if not rd:
            raise HTTPException(
                status_code=503,
                detail="Real-Debrid downloader is not configured or unavailable.",
            )

        clean_hash = extract_infohash(magnet_or_infohash)
        if not clean_hash:
            raw = magnet_or_infohash.strip().lower()
            if len(raw) == 40 and all(c in "0123456789abcdef" for c in raw):
                clean_hash = raw

        if not clean_hash:
            raise HTTPException(
                status_code=400,
                detail="Invalid magnet URI or infohash provided.",
            )

        target_magnet = (
            magnet_or_infohash.strip()
            if magnet_or_infohash.strip().startswith("magnet:")
            else clean_hash
        )

        try:
            tid = rd.add_torrent(target_magnet)
            info = rd.get_torrent_info(tid)
            if info.status == "waiting_files_selection":
                video_ids = [
                    file_id
                    for file_id, metadata in info.files.items()
                    if metadata.filename.lower().endswith(
                        tuple(
                            ext.lower()
                            for ext in __import__(
                                "program.services.downloaders.models",
                                fromlist=["VALID_VIDEO_EXTENSIONS"],
                            ).VALID_VIDEO_EXTENSIONS
                        )
                    )
                ]
                if not video_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="No supported video files were found in this torrent.",
                    )
                rd.select_files(tid, video_ids)
                info = rd.get_torrent_info(tid)
        except RealDebridError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to submit transfer to Real-Debrid: {e}"
            )

        if media_item_id:
            with db_session() as session:
                item = session.get(MediaItem, media_item_id)
                if item:
                    item.active_stream = ActiveStream(infohash=clean_hash, id=tid)
                    if item.last_state in (
                        States.Unknown,
                        States.Requested,
                        States.Indexed,
                    ):
                        item.last_state = States.Scraped

                    # Ensure Stream record exists
                    stream = (
                        session.execute(
                            select(Stream).filter(
                                func.lower(Stream.infohash) == clean_hash.lower()
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if not stream:
                        parsed_data = parse(info.name)
                        stream = Stream(
                            Torrent(
                                raw_title=info.name,
                                infohash=clean_hash,
                                data=parsed_data,
                                rank=0,
                                lev_ratio=1.0,
                            )
                        )
                        session.add(stream)
                        session.flush()

                    if stream not in item.streams:
                        item.streams.append(stream)

                    enqueue_operation(
                        session=session,
                        operation_type="realdebrid.poll_transfer",
                        media_item_id=media_item_id,
                        payload={"torrent_id": tid, "infohash": clean_hash},
                        idempotency_key=f"rd_poll_{tid}",
                    )
                    session.commit()
                    notify_outbox_dispatcher()
                    publish_outbox_lifecycle_event(
                        "operation_enqueued",
                        {
                            "transfer_id": tid,
                            "media_item_id": media_item_id,
                            "infohash": clean_hash,
                        },
                    )

        # If already downloaded on Real-Debrid, immediately trigger ingestion
        if info.status == "downloaded" and media_item_id:
            self.poll_and_ingest_transfers(rd)

        self.invalidate_cache()
        return TransferActionResponse(
            success=True,
            message=f"Transfer {tid} successfully submitted to Real-Debrid (status: {info.status}).",
            transfer_id=tid,
        )

    def poll_and_ingest_transfers(
        self, rd: RealDebridDownloader | None = None
    ) -> dict[str, int]:
        """
        Poll active Real-Debrid transfers and automatically ingest completed items into CineFlow.
        """
        if rd is None:
            rd = _get_rd_downloader()
        if not rd:
            return {"polled": 0, "ingested": 0}

        try:
            raw_torrents = rd.get_torrents(limit=100)
        except Exception as e:
            logger.debug(f"Failed to fetch torrents for polling: {e}")
            return {"polled": 0, "ingested": 0}

        polled_count = len(raw_torrents)
        ingested_count = 0

        # Find any CineFlow-managed items that are downloaded on RD but not yet ingested
        with db_session() as session:
            active_items = (
                session.execute(
                    select(MediaItem).filter(MediaItem.active_stream.is_not(None))
                )
                .scalars()
                .all()
            )
            item_map: dict[str, MediaItem] = {}
            for item in active_items:
                if item.active_stream:
                    as_id = str(getattr(item.active_stream, "id", ""))
                    as_hash = str(getattr(item.active_stream, "infohash", "")).lower()
                    if as_id:
                        item_map[as_id] = item
                    if as_hash:
                        item_map[as_hash] = item

            for t in raw_torrents:
                tid = str(t.get("id", ""))
                thash = str(t.get("hash", "")).lower()
                status = str(t.get("status", "")).lower()

                matched_item = item_map.get(tid) or item_map.get(thash)
                if not matched_item:
                    continue

                if status == "downloaded":
                    state_val = getattr(
                        matched_item.state, "value", str(matched_item.state)
                    )
                    has_files = len(getattr(matched_item, "filesystem_entries", [])) > 0

                    if (
                        state_val
                        not in (
                            States.Downloaded.value,
                            States.Completed.value,
                            States.Symlinked.value,
                        )
                        and not has_files
                    ):
                        # Ingest into CineFlow
                        stream = (
                            session.execute(
                                select(Stream).filter(
                                    func.lower(Stream.infohash) == thash
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if not stream:
                            raw_name = str(t.get("filename", "") or "Media")
                            parsed_data = parse(raw_name)
                            stream = Stream(
                                Torrent(
                                    raw_title=raw_name,
                                    infohash=thash,
                                    data=parsed_data,
                                    rank=0,
                                    lev_ratio=1.0,
                                )
                            )
                            session.add(stream)
                            session.flush()

                        if stream not in matched_item.streams:
                            matched_item.streams.append(stream)

                        # Attempt ingestion via Downloader
                        downloader = None
                        try:
                            from kink import di

                            from program.program import Program

                            if Program in di:
                                prog = di[Program]
                                if prog and prog.services is not None:
                                    downloader = prog.services.downloader
                        except Exception:
                            pass

                        if downloader:
                            try:
                                ok = downloader.start_manual_download(
                                    item=matched_item,
                                    stream=stream,
                                    service=rd,
                                )
                                if ok:
                                    ingested_count += 1
                                    logger.success(
                                        f"Successfully ingested completed Real-Debrid transfer {tid} for {matched_item.log_string}"
                                    )
                                    now = datetime.now(UTC)
                                    session.execute(
                                        update(OperationLedger)
                                        .where(
                                            OperationLedger.media_item_id
                                            == matched_item.id,
                                            OperationLedger.status.in_(
                                                ["pending", "processing"]
                                            ),
                                        )
                                        .values(
                                            status="completed",
                                            completed_at=now,
                                            updated_at=now,
                                        )
                                    )
                                    session.commit()
                                    publish_outbox_lifecycle_event(
                                        "operation_completed",
                                        {
                                            "transfer_id": tid,
                                            "media_item_id": matched_item.id,
                                        },
                                    )
                            except Exception as err:
                                logger.warning(
                                    f"Ingestion failed for transfer {tid} and item {matched_item.id}: {err}"
                                )
                elif status in ("error", "magnet_error", "virus", "dead"):
                    # Mark active operations failed
                    now = datetime.now(UTC)
                    session.execute(
                        update(OperationLedger)
                        .where(
                            OperationLedger.media_item_id == matched_item.id,
                            OperationLedger.status.in_(["pending", "processing"]),
                        )
                        .values(
                            status="failed",
                            error_classification="TorrentFailed",
                            error_message=f"Real-Debrid torrent {tid} failed with status: {status}",
                            completed_at=now,
                            updated_at=now,
                        )
                    )
                    session.commit()

        if ingested_count > 0:
            self.invalidate_cache()

        return {"polled": polled_count, "ingested": ingested_count}


# Global singleton instance
rd_transfers_service = RealDebridTransfersService()
