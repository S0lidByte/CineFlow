import hmac
import json
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request, status
from kink import di
from loguru import logger
from pydantic import BaseModel

from program.media.item import MediaItem
from program.program import Program
from program.services.content.overseerr import Overseerr
from program.settings import settings_manager
from program.utils.plex_webhook import sanitize_plex_guids

from ..models.overseerr import OverseerrWebhook

router = APIRouter(
    prefix="/webhook",
    responses={404: {"description": "Not found"}},
)

WEBHOOK_SECRET_HEADER = "x-webhook-secret"
PLEX_SCROBBLE_EVENT = "media.scrobble"


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
    """Dry-run Plex webhook: parse ``media.scrobble``, map/log provider GUIDs.

    Does **not** write to Trakt. Configure Plex (Pass) or Tautulli to POST here
    with API key (+ optional ``webhook_secret``).
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
        media_type = ""
        if metadata:
            type_val = cast(
                object,
                metadata.get("type") or metadata.get("librarySectionType") or "",
            )
            media_type = str(type_val)

        logger.log(
            "API",
            f"Plex scrobble dry-run type={media_type or 'unknown'} guids={guids}",
        )

        return PlexWebhookResponse(
            success=True,
            event=event,
            guids=guids,
            message="dry-run (no Trakt write)",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process Plex webhook: {e}")
        return PlexWebhookResponse(success=False, message="processing failed")
