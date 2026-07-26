import hmac
import json
from typing import Any, cast

from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Request, status
from kink import di
from loguru import logger
from pydantic import BaseModel

from program.apis.trakt_api import TraktAPI
from program.media.item import MediaItem
from program.program import Program
from program.services.content.overseerr import Overseerr
from program.settings import settings_manager
from program.utils.plex_trakt_metrics import record_history_result
from program.utils.plex_webhook import (
    build_trakt_history_payload,
    history_idempotency_key,
    plex_media_kind,
    sanitize_plex_guids,
)

from ..models.overseerr import OverseerrWebhook

router = APIRouter(
    prefix="/webhook",
    responses={404: {"description": "Not found"}},
)

WEBHOOK_SECRET_HEADER = "x-webhook-secret"
PLEX_SCROBBLE_EVENT = "media.scrobble"

# Dedup repeated Plex/Tautulli deliveries of the same scrobble (24h TTL).
_HISTORY_IDEMPOTENCY: TTLCache[str, bool] = TTLCache(maxsize=4096, ttl=86400)


class OverseerrWebhookResponse(BaseModel):
    success: bool
    message: str | None = None


class PlexWebhookResponse(BaseModel):
    success: bool
    event: str | None = None
    guids: list[str] | None = None
    message: str | None = None


def verify_overseerr_webhook_secret(request: Request) -> None:
    """When configured, require X-Webhook-Secret to match Overseerr webhook_secret."""

    expected = (
        settings_manager.settings.content.overseerr.webhook_secret or ""
    ).strip()
    if not expected:
        return

    provided = request.headers.get(WEBHOOK_SECRET_HEADER)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


def verify_plex_webhook_secret(request: Request) -> None:
    """When configured, require secret via header or ``webhook_secret`` query."""

    expected = (
        settings_manager.settings.content.plex_webhook.webhook_secret or ""
    ).strip()
    if not expected:
        return

    provided = (request.headers.get(WEBHOOK_SECRET_HEADER) or "").strip()
    if not provided:
        provided = (request.query_params.get("webhook_secret") or "").strip()

    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


async def _parse_plex_webhook_payload(request: Request) -> dict[str, Any]:
    """Parse Plex webhook body (multipart ``payload`` field or raw JSON)."""

    content_type = (request.headers.get("content-type") or "").lower()

    if (
        "multipart/form-data" in content_type
        or "application/x-www-form-urlencoded" in content_type
    ):
        form = await request.form()
        raw = form.get("payload")
        if raw is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing multipart payload field",
            )
        text: str
        if hasattr(raw, "read"):
            raw_bytes = cast(bytes, await raw.read())  # type: ignore[misc]
            text = raw_bytes.decode("utf-8", errors="replace")
        else:
            text = str(raw)
        try:
            parsed = cast(object, json.loads(text))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON in payload field",
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Plex payload must be a JSON object",
            )
        return cast(dict[str, Any], parsed)

    try:
        parsed_body = cast(object, await request.json())
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        ) from exc

    if not isinstance(parsed_body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plex payload must be a JSON object",
        )
    return cast(dict[str, Any], parsed_body)


def _sync_plex_scrobble_to_trakt(
    *,
    metadata: dict[str, Any] | None,
    guids: list[str],
) -> str:
    """Attempt Trakt history POST. Returns a short status message for the response."""

    media_kind = plex_media_kind(metadata)
    idem_key = history_idempotency_key(
        media_kind=media_kind, guids=guids, metadata=metadata
    )
    if idem_key in _HISTORY_IDEMPOTENCY:
        record_history_result("idempotent")
        logger.debug(f"Plex scrobble idempotent skip key={idem_key}")
        return "idempotent (already synced)"

    payload = build_trakt_history_payload(metadata, guids)
    if not payload:
        record_history_result("skipped")
        logger.warning(
            f"Plex scrobble skipped (no Trakt mapping) type={media_kind} guids={guids}"
        )
        return "skipped (no Trakt mapping)"

    try:
        trakt_api = di[TraktAPI]
    except Exception:
        record_history_result("failed")
        logger.error("TraktAPI not available in DI container")
        return "failed (TraktAPI unavailable)"

    if not trakt_api.oauth_connected():
        record_history_result("skipped")
        logger.warning("Plex scrobble skipped: Trakt OAuth not connected")
        return "skipped (Trakt OAuth not connected)"

    ok = trakt_api.add_items_to_watched_history(payload)
    if ok:
        _HISTORY_IDEMPOTENCY[idem_key] = True
        record_history_result("success")
        logger.log(
            "API",
            f"Plex scrobble synced to Trakt type={media_kind} guids={guids}",
        )
        return "synced to Trakt"

    record_history_result("failed")
    return "failed (Trakt history POST)"


@router.post(
    "/overseerr",
    response_model=OverseerrWebhookResponse,
)
async def overseerr(request: Request) -> OverseerrWebhookResponse:
    """Webhook for Overseerr"""

    try:
        verify_overseerr_webhook_secret(request)
        response = await request.json()

        if response.get("subject") == "Test Notification":
            logger.log(
                "API", "Received test notification, Overseerr configured properly"
            )

            return OverseerrWebhookResponse(
                success=True,
            )

        req = OverseerrWebhook.model_validate(response)

        if services := di[Program].services:
            overseerr = services.overseerr
        else:
            logger.error("Overseerr not initialized yet")
            return OverseerrWebhookResponse(
                success=False,
                message="Overseerr not initialized",
            )

        if not overseerr.initialized:
            logger.error("Overseerr not initialized")

            return OverseerrWebhookResponse(
                success=False,
                message="Overseerr not initialized",
            )

        item_type = req.media.media_type

        new_item = None

        if item_type == "movie":
            new_item = MediaItem(
                {
                    "tmdb_id": req.media.tmdbId,
                    "requested_by": "overseerr",
                    "overseerr_id": req.request.request_id if req.request else None,
                }
            )
        elif item_type == "tv":
            new_item = MediaItem(
                {
                    "tvdb_id": req.media.tvdbId,
                    "requested_by": "overseerr",
                    "overseerr_id": req.request.request_id if req.request else None,
                }
            )

        if not new_item:
            logger.error(
                f"Failed to create new item: TMDB ID {req.media.tmdbId}, TVDB ID {req.media.tvdbId}"
            )

            return OverseerrWebhookResponse(
                success=False,
                message="Failed to create new item",
            )

        di[Program].em.add_item(
            new_item,
            service=Overseerr.__class__.__name__,
        )

        return OverseerrWebhookResponse(success=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process request: {e}")

        return OverseerrWebhookResponse(success=False)


@router.post(
    "/plex",
    response_model=PlexWebhookResponse,
)
async def plex_webhook(request: Request) -> PlexWebhookResponse:
    """Plex webhook: parse ``media.scrobble``, map provider GUIDs, optionally sync.

    When ``content.plex_webhook.sync_to_trakt`` is false (default), logs GUIDs only.
    When true and Trakt OAuth is connected, POSTs to Trakt ``/sync/history``.
    """

    try:
        verify_plex_webhook_secret(request)
        payload = await _parse_plex_webhook_payload(request)
        event = str(payload.get("event") or "").strip()

        if event != PLEX_SCROBBLE_EVENT:
            logger.debug(f"Ignoring Plex webhook event={event or 'unknown'}")
            return PlexWebhookResponse(
                success=True,
                event=event or None,
                message="ignored (not media.scrobble)",
            )

        metadata_raw = cast(object, payload.get("Metadata"))
        metadata: dict[str, Any] | None
        if isinstance(metadata_raw, dict):
            metadata = cast(dict[str, Any], metadata_raw)
        else:
            metadata = None

        guids = sanitize_plex_guids(metadata)
        media_type = plex_media_kind(metadata)

        sync_enabled = bool(
            settings_manager.settings.content.plex_webhook.sync_to_trakt
        )

        if not sync_enabled:
            record_history_result("dry_run")
            logger.log(
                "API",
                f"Plex scrobble dry-run type={media_type} guids={guids}",
            )
            return PlexWebhookResponse(
                success=True,
                event=event,
                guids=guids,
                message="dry-run (no Trakt write)",
            )

        message = _sync_plex_scrobble_to_trakt(metadata=metadata, guids=guids)
        success = not message.startswith("failed")
        return PlexWebhookResponse(
            success=success,
            event=event,
            guids=guids,
            message=message,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process Plex webhook: {e}")
        record_history_result("failed")
        return PlexWebhookResponse(success=False, message="processing failed")
