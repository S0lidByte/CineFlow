"""Operational timeline and outbox management API routes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from auth import require_role
from program.contracts.dispatcher import notify_outbox_dispatcher
from program.contracts.operation_ledger import OperationLedger
from program.contracts.operation_timeline import serialize_operation_timeline_item
from program.db.db import db_session
from program.managers.sse_manager import sse_manager
from program.services.downloaders.realdebrid_transfers import (
    RealDebridTransferDetail,
    RealDebridTransfersResponse,
    TransferActionResponse,
    rd_transfers_service,
)

router = APIRouter(
    prefix="/operations",
    tags=["operations"],
    responses={404: {"description": "Not found"}},
)


class OperationTimelineItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    correlation_id: str
    media_item_id: int | None = None
    operation_type: str
    schema_version: int = 1
    status: str
    attempt_count: int = 0
    idempotency_key: str | None = None
    scheduled_at: datetime
    lease_expires_at: datetime | None = None
    worker_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_classification: str | None = None
    error_message: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class OperationTimelineListResponse(BaseModel):
    items: list[OperationTimelineItem]
    total: int
    limit: int
    offset: int


class OperationRetryResponse(BaseModel):
    success: bool
    message: str
    operation: OperationTimelineItem | None = None


def _to_redacted_item(op: OperationLedger) -> OperationTimelineItem:
    """Convert an OperationLedger ORM instance to the canonical redacted API item."""
    return OperationTimelineItem.model_validate(serialize_operation_timeline_item(op))


@router.get(
    "/timeline",
    operation_id="list_operation_timeline",
    response_model=OperationTimelineListResponse,
)
async def list_operation_timeline(
    status: Annotated[
        str | None,
        Query(
            description="Filter by operation status (pending, processing, completed, failed)"
        ),
    ] = None,
    operation_type: Annotated[
        str | None, Query(description="Filter by operation type")
    ] = None,
    correlation_id: Annotated[
        str | None, Query(description="Filter by correlation ID")
    ] = None,
    media_item_id: Annotated[
        int | None, Query(description="Filter by media item ID")
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=200, description="Max number of items to return")
    ] = 50,
    offset: Annotated[int, Query(ge=0, description="Pagination offset")] = 0,
) -> OperationTimelineListResponse:
    """List historical and active operations with redaction and filtering."""
    with db_session() as session:
        query = select(OperationLedger)
        count_query = select(func.count(OperationLedger.id))

        if status:
            query = query.filter(OperationLedger.status == status)
            count_query = count_query.filter(OperationLedger.status == status)
        if operation_type:
            query = query.filter(OperationLedger.operation_type == operation_type)
            count_query = count_query.filter(
                OperationLedger.operation_type == operation_type
            )
        if correlation_id:
            query = query.filter(OperationLedger.correlation_id == correlation_id)
            count_query = count_query.filter(
                OperationLedger.correlation_id == correlation_id
            )
        if media_item_id is not None:
            query = query.filter(OperationLedger.media_item_id == media_item_id)
            count_query = count_query.filter(
                OperationLedger.media_item_id == media_item_id
            )

        total = session.execute(count_query).scalar() or 0
        records = (
            session.execute(
                query.order_by(OperationLedger.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )

        items = [_to_redacted_item(rec) for rec in records]
        return OperationTimelineListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get(
    "/timeline/stream",
    operation_id="stream_operation_timeline",
)
async def stream_operation_timeline() -> StreamingResponse:
    """Stream live operational updates via Server-Sent Events (SSE)."""
    return StreamingResponse(
        sse_manager.subscribe("operation_timeline"),
        media_type="text/event-stream",
    )


@router.get(
    "/timeline/{operation_id}",
    operation_id="get_operation_timeline_item",
    response_model=OperationTimelineItem,
)
async def get_operation_timeline_item(
    operation_id: Annotated[
        str, Path(description="The UUID of the operation to retrieve")
    ],
) -> OperationTimelineItem:
    """Get details for a single operation ledger item."""
    with db_session() as session:
        op = session.get(OperationLedger, operation_id)
        if not op:
            raise HTTPException(status_code=404, detail="Operation not found")
        return _to_redacted_item(op)


@router.post(
    "/timeline/{operation_id}/retry",
    operation_id="retry_operation_timeline_item",
    response_model=OperationRetryResponse,
    dependencies=[Depends(require_role("platform:admin"))],
)
async def retry_operation_timeline_item(
    operation_id: Annotated[
        str, Path(description="The UUID of the operation to retry")
    ],
) -> OperationRetryResponse:
    """Re-schedule a failed or dead-lettered operation for immediate retry."""
    with db_session() as session:
        op = session.get(OperationLedger, operation_id)
        if not op:
            raise HTTPException(status_code=404, detail="Operation not found")

        now = datetime.now(UTC)
        retry_stmt = (
            update(OperationLedger)
            .where(
                OperationLedger.id == operation_id,
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
            .execution_options(synchronize_session=False)
        )
        result = session.execute(retry_stmt)
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            session.rollback()
            from program.contracts.outbox_metrics import record_retry_request

            record_retry_request("rejected")
            raise HTTPException(
                status_code=400,
                detail=f"Operation is currently '{op.status}' and cannot be retried.",
            )

        session.commit()
        session.expire_all()
        retried = session.get(OperationLedger, operation_id)
        if retried is None:
            raise HTTPException(status_code=404, detail="Operation not found")

        from program.contracts.outbox_metrics import record_retry_request

        record_retry_request("success")
        notify_outbox_dispatcher()
        from program.contracts.dispatcher import publish_outbox_lifecycle_event

        publish_outbox_lifecycle_event(
            "operation_retrying", _to_redacted_item(retried).model_dump(mode="json")
        )

        return OperationRetryResponse(
            success=True,
            message="Operation re-queued for execution.",
            operation=_to_redacted_item(retried),
        )


@router.get(
    "/realdebrid/transfers",
    operation_id="list_realdebrid_transfers",
    response_model=RealDebridTransfersResponse,
)
async def list_realdebrid_transfers(
    status: Annotated[
        str | None,
        Query(
            description="Filter by transfer status (all, active, downloading, queued, downloaded, failed)"
        ),
    ] = None,
    search: Annotated[
        str | None,
        Query(description="Search transfers by filename, infohash, or media title"),
    ] = None,
    force_refresh: Annotated[
        bool,
        Query(
            description="Bypass short-lived cache and fetch fresh state from Real-Debrid"
        ),
    ] = False,
) -> RealDebridTransfersResponse:
    """List Real-Debrid transfers with CineFlow correlation and summary statistics."""
    return await asyncio.to_thread(
        rd_transfers_service.get_transfers,
        force_refresh=force_refresh,
        status=status,
        search=search,
    )


@router.get(
    "/realdebrid/transfers/{transfer_id}",
    operation_id="get_realdebrid_transfer_detail",
    response_model=RealDebridTransferDetail,
)
async def get_realdebrid_transfer_detail(
    transfer_id: Annotated[str, Path(description="The Real-Debrid torrent ID")],
) -> RealDebridTransferDetail:
    """Get detailed information and file breakdown for a specific Real-Debrid transfer."""
    return await asyncio.to_thread(
        rd_transfers_service.get_transfer_detail, transfer_id
    )


@router.post(
    "/realdebrid/transfers/{transfer_id}/cancel",
    operation_id="cancel_realdebrid_transfer",
    response_model=TransferActionResponse,
    dependencies=[Depends(require_role("platform:admin"))],
)
async def cancel_realdebrid_transfer(
    transfer_id: Annotated[
        str, Path(description="The Real-Debrid torrent ID to cancel")
    ],
) -> TransferActionResponse:
    """Safely cancel an active CineFlow-managed Real-Debrid transfer and update outbox ledger."""
    return await asyncio.to_thread(rd_transfers_service.cancel_transfer, transfer_id)


@router.post(
    "/realdebrid/transfers/{transfer_id}/retry",
    operation_id="retry_realdebrid_transfer",
    response_model=TransferActionResponse,
    dependencies=[Depends(require_role("platform:admin"))],
)
async def retry_realdebrid_transfer(
    transfer_id: Annotated[
        str, Path(description="The Real-Debrid torrent ID to retry")
    ],
) -> TransferActionResponse:
    """Retry a failed CineFlow-managed transfer and re-queue corresponding operation."""
    return await asyncio.to_thread(rd_transfers_service.retry_transfer, transfer_id)


class AttachTransferRequest(BaseModel):
    media_item_id: int = Field(
        description="The MediaItem ID to attach this transfer to"
    )


@router.post(
    "/realdebrid/transfers/{transfer_id}/attach",
    operation_id="attach_realdebrid_transfer",
    response_model=TransferActionResponse,
    dependencies=[Depends(require_role("platform:admin"))],
)
async def attach_realdebrid_transfer(
    transfer_id: Annotated[
        str, Path(description="The Real-Debrid torrent ID to attach")
    ],
    request: AttachTransferRequest,
) -> TransferActionResponse:
    """Attach an existing Real-Debrid transfer to a MediaItem and trigger ingestion or monitoring."""
    return await asyncio.to_thread(
        rd_transfers_service.attach_transfer,
        transfer_id=transfer_id,
        media_item_id=request.media_item_id,
    )


class SubmitTransferRequest(BaseModel):
    magnet: str
    media_item_id: int | None = None


class PollTransfersResponse(BaseModel):
    polled: int
    ingested: int


@router.post(
    "/realdebrid/transfers/submit",
    operation_id="submit_realdebrid_transfer",
    response_model=TransferActionResponse,
    dependencies=[Depends(require_role("platform:admin"))],
)
async def submit_realdebrid_transfer(
    request: SubmitTransferRequest,
) -> TransferActionResponse:
    """Submit a magnet or infohash to Real-Debrid and track it in CineFlow."""
    return await asyncio.to_thread(
        rd_transfers_service.submit_transfer,
        magnet_or_infohash=request.magnet,
        media_item_id=request.media_item_id,
    )


@router.post(
    "/realdebrid/transfers/poll",
    operation_id="poll_realdebrid_transfers",
    response_model=PollTransfersResponse,
    dependencies=[Depends(require_role("platform:admin"))],
)
async def poll_realdebrid_transfers() -> PollTransfersResponse:
    """Trigger an immediate poll and completion ingestion sweep for active transfers."""
    result = await asyncio.to_thread(rd_transfers_service.poll_and_ingest_transfers)
    return PollTransfersResponse(
        polled=result.get("polled", 0),
        ingested=result.get("ingested", 0),
    )
