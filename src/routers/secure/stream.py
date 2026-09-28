import asyncio
import json
import logging
import math
import mimetypes
import re
import subprocess
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from kink import di
from loguru import logger
from pydantic import BaseModel

from program.db.db import db_session
from program.managers.sse_manager import sse_manager
from program.media.item import MediaItem
from program.services.streaming.streaming_constants import PROXY_REQUIRED_PROVIDERS
from program.settings import settings_manager
from program.utils.async_client import AsyncClient
from program.utils.hls_params import (
    FFPROBE_TIMEOUT_SECONDS,
    scale_filter_for_resolution,
    validate_hls_params,
)
from program.utils.proxy_client import ProxyClient

router = APIRouter(
    responses={404: {"description": "Not found"}},
    prefix="/stream",
    tags=["stream"],
)

DEFAULT_STREAM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADER_NAME_REGEX = re.compile(r"^[A-Za-z0-9_-]+$")
_DISALLOWED_FFMPEG_FORWARD_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "x-api-key",
    }
)


class SSELogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        log_entry = {
            "time": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": record.msg,
        }
        sse_manager.publish_event("logging", json.dumps(log_entry))


logger.add(SSELogHandler())


class EventTypesResponse(BaseModel):
    event_types: list[str]


@router.get(
    "/event_types",
    response_model=EventTypesResponse,
)
async def get_event_types():
    return EventTypesResponse(
        event_types=list(sse_manager.subscribers.keys()),
    )


@router.get("/{event_type}")
async def stream_events(
    event_type: Annotated[
        str,
        Path(
            description="The type of event to stream",
            min_length=1,
        ),
    ],
) -> StreamingResponse:
    return StreamingResponse(
        sse_manager.subscribe(event_type),
        media_type="text/event-stream",
    )


def _get_media_info(item_id: int) -> tuple[str, str, str, int]:
    """
    Retrieve media information for the given item ID.

    Returns:
        Tuple of (url, provider, filename, file_size).

    Raises:
        HTTPException: If item not found or has no valid media.
    """
    with db_session() as session:
        item = session.get(MediaItem, item_id)

        if not item:
            raise HTTPException(status_code=404, detail="Item not found")

        if not item.media_entry:
            raise HTTPException(status_code=404, detail="Item has no media file")

        url = item.media_entry.url

        if not url:
            raise HTTPException(status_code=404, detail="Item has no valid stream URL")

        file_size = int(item.media_entry.file_size or 0)
        return (
            url,
            item.media_entry.provider or "",
            item.media_entry.original_filename,
            file_size,
        )


def _get_client(provider: str) -> httpx.AsyncClient:
    """Get the appropriate HTTP client based on provider requirements."""
    use_proxy = (
        provider in PROXY_REQUIRED_PROVIDERS
        and settings_manager.settings.downloaders.proxy_url
    )
    return di[ProxyClient] if use_proxy else di[AsyncClient]


async def _probe_file_size(client: httpx.AsyncClient, url: str) -> int | None:
    """Attempt to discover representation length if not populated in the database."""
    try:
        resp = await client.head(url, timeout=5.0)
        if resp.status_code == 200 and "content-length" in resp.headers:
            return int(resp.headers["content-length"])
    except Exception:
        pass
    try:
        resp = await client.get(url, headers={"Range": "bytes=0-0"}, timeout=5.0)
        if resp.status_code == 206 and "content-range" in resp.headers:
            match = re.search(r"/(\d+)$", resp.headers["content-range"])
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None


def _normalize_range_header(
    range_header: str | None,
    file_size: int | None,
) -> tuple[str | None, tuple[int, int] | None, bool]:
    """Parse and normalize an HTTP Range header per RFC 9110 Section 14.

    Args:
        range_header: Raw 'Range' header string from client, e.g. 'bytes=0-100'.
        file_size: Total representation length in bytes if known, or None.

    Returns:
        A tuple of (normalized_range_str, (start, end), is_unsatisfiable):
        - normalized_range_str: Sanitized Range string to forward upstream ('bytes=START-END'),
          or None if the header is invalid/unsupported and should be ignored (serve 200).
        - (start, end): Resolved 0-based inclusive byte offsets if satisfiable.
        - is_unsatisfiable: True if the range is syntactically well-formed but out-of-bounds
          relative to file_size (must trigger HTTP 416).
    """
    if not range_header:
        return None, None, False

    range_header = range_header.strip()
    if not range_header.lower().startswith("bytes="):
        # Case 14: Unsupported range unit -> ignore Range header (RFC 9110 §14.2)
        return None, None, False

    range_spec = range_header[len("bytes=") :].strip()
    if not range_spec:
        # Case 13: Malformed Range -> ignore
        return None, None, False

    # Case 16: Handle multiple ranges by selecting the first valid range set
    first_range = range_spec.split(",")[0].strip()
    if not first_range:
        return None, None, False

    # Suffix range: bytes=-<suffix-length> (Cases 6, 7, 8, 15)
    if first_range.startswith("-"):
        suffix_str = first_range[1:].strip()
        if not suffix_str.isdigit():
            # Case 15: Empty/invalid suffix -> ignore (RFC 9110 §14.2)
            return None, None, False
        suffix_len = int(suffix_str)
        if suffix_len == 0:
            # Suffix length of 0 is syntactically invalid (RFC 9110 §14.1.2) -> ignore
            return None, None, False

        if file_size is not None and file_size >= 0:
            if file_size == 0:
                # Representation length is 0 bytes -> unsatisfiable
                return None, None, True
            # RFC 9110: bytes=-N resolves to start = max(0, L - N), end = L - 1
            start = max(0, file_size - suffix_len)
            end = file_size - 1
            return f"bytes={start}-{end}", (start, end), False
        else:
            return f"bytes=-{suffix_len}", None, False

    # Standard range: bytes=<start>-[<end>]
    if "-" not in first_range:
        # Case 13: Malformed -> ignore
        return None, None, False

    start_str, end_str = first_range.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    if not start_str.isdigit():
        # Case 13: Malformed -> ignore
        return None, None, False

    start = int(start_str)

    if file_size is not None and file_size >= 0:
        if start >= file_size:
            # Case 12: First-byte-pos >= representation length -> Unsatisfiable (RFC 9110 §14.4)
            return None, None, True

    if not end_str:
        # Open-ended: bytes=<start>- (Cases 4, 9)
        if file_size is not None and file_size > 0:
            end = file_size - 1
            return f"bytes={start}-{end}", (start, end), False
        else:
            return f"bytes={start}-", None, False
    else:
        # Closed range: bytes=<start>-<end> (Cases 1, 2, 3, 5, 10, 11)
        if not end_str.isdigit():
            # Case 13: Malformed -> ignore
            return None, None, False
        end = int(end_str)
        if end < start:
            # Syntactically invalid (last-byte-pos < first-byte-pos) -> ignore
            return None, None, False

        if file_size is not None and file_size > 0:
            # Case 11: If end exceeds representation length, clamp to file_size - 1
            if end >= file_size:
                end = file_size - 1
            return f"bytes={start}-{end}", (start, end), False
        else:
            return f"bytes={start}-{end}", (start, end), False


def _build_forward_headers(
    request: Request, normalized_range: str | None = None
) -> dict[str, str]:
    """Build headers to forward to upstream."""
    headers: dict[str, str] = {}
    if normalized_range:
        headers["Range"] = normalized_range
    return headers


def sanitize_header_value(value: str) -> str:
    """Remove line breaks so a value cannot inject extra HTTP headers."""
    return value.replace("\r", "").replace("\n", "")


def build_ffmpeg_headers(headers: Mapping[str, str] | None = None) -> str:
    """Build a safe libavformat HTTP header block for CDN input requests.

    FFmpeg expects CRLF-delimited header lines and this value must be passed with
    ``-headers`` immediately before the relevant ``-i`` URL. Only non-sensitive,
    end-to-end request headers can be forwarded.
    """
    forwarded_headers: dict[str, str] = {"User-Agent": DEFAULT_STREAM_USER_AGENT}

    for name, value in (headers or {}).items():
        normalized_name = name.lower()
        if (
            not _HEADER_NAME_REGEX.fullmatch(name)
            or normalized_name in _DISALLOWED_FFMPEG_FORWARD_HEADERS
        ):
            continue

        sanitized_value = sanitize_header_value(value)
        if sanitized_value:
            forwarded_headers[name] = sanitized_value

    return "".join(f"{name}: {value}\r\n" for name, value in forwarded_headers.items())


def _extract_response_headers(
    upstream_response: httpx.Response,
    filename: str,
) -> dict[str, str]:
    """Extract relevant headers from upstream response."""
    headers: dict[str, str] = {}
    for key in ["content-type", "content-length", "content-range", "accept-ranges"]:
        if key in upstream_response.headers:
            headers[key] = upstream_response.headers[key]
    headers["content-disposition"] = f'inline; filename="{filename}"'

    # Enforce RFC 9110 §14.4: For 206 Partial Content, Content-Length must be the size of the range
    if upstream_response.status_code == 206 and "content-range" in headers:
        cr_match = re.match(
            r"^bytes\s+(\d+)-(\d+)/(?:\d+|\*)$", headers["content-range"].strip()
        )
        if cr_match:
            start_b = int(cr_match.group(1))
            end_b = int(cr_match.group(2))
            headers["content-length"] = str(max(0, end_b - start_b + 1))

    return headers


async def _handle_upstream_error(upstream_response: httpx.Response) -> None:
    """Close a failed upstream response without logging its potentially sensitive body."""
    logger.error(f"Upstream returned error {upstream_response.status_code}")
    await upstream_response.aclose()

    raise HTTPException(
        status_code=upstream_response.status_code,
        detail=f"Upstream error: {upstream_response.status_code}",
    )


@router.get("/file/{item_id}")
async def stream_file(
    item_id: int,
    request: Request,
) -> Response:
    """
    Stream a file directly from the provider.

    Args:
        item_id: The ID of the MediaItem to stream.
        request: The FastAPI request object.

    Returns:
        A StreamingResponse for the file content, or Response on 416 range error.
    """
    media_info = _get_media_info(item_id)
    url = media_info[0]
    provider = media_info[1]
    filename = media_info[2]
    file_size: int | None = (
        int(media_info[3]) if len(media_info) >= 4 and media_info[3] > 0 else None
    )

    client = None

    if file_size is None or file_size <= 0:
        client = _get_client(provider)
        file_size = await _probe_file_size(client, url)

    raw_range = request.headers.get("range")
    normalized_range, _, is_unsatisfiable = _normalize_range_header(
        raw_range, file_size
    )

    if is_unsatisfiable:
        total_len = str(file_size) if file_size is not None and file_size >= 0 else "*"
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{total_len}",
                "Accept-Ranges": "bytes",
                "Content-Type": "text/plain",
            },
            content="Requested Range Not Satisfiable",
        )

    if client is None:
        client = _get_client(provider)

    forward_headers = _build_forward_headers(request, normalized_range=normalized_range)

    upstream_response: httpx.Response | None = None
    try:
        req = client.build_request("GET", url, headers=forward_headers)

        try:
            upstream_response = await client.send(req, stream=True)
        except Exception as e:
            logger.error(f"Failed to connect to upstream: {e}")
            raise HTTPException(status_code=502, detail="Upstream connection failed")

        if upstream_response.status_code == 416:
            total_len = (
                str(file_size) if file_size is not None and file_size >= 0 else "*"
            )
            cr = upstream_response.headers.get("content-range", f"bytes */{total_len}")
            await upstream_response.aclose()
            return Response(
                status_code=416,
                headers={
                    "Content-Range": cr,
                    "Accept-Ranges": "bytes",
                    "Content-Type": "text/plain",
                },
                content="Requested Range Not Satisfiable",
            )

        if upstream_response.status_code >= 400:
            await _handle_upstream_error(upstream_response)

        response_headers = _extract_response_headers(upstream_response, filename)

        # Force correct MIME type based on extension.
        # Firefox fails on application/octet-stream, which many providers send.
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            response_headers["content-type"] = guessed_type

        if "accept-ranges" not in response_headers:
            response_headers["accept-ranges"] = "bytes"

        max_bytes: int | None = None
        if (
            upstream_response.status_code == 206
            and "content-length" in response_headers
        ):
            try:
                max_bytes = int(response_headers["content-length"])
            except ValueError:
                pass

        async def stream_iterator():
            bytes_yielded = 0
            try:
                async for chunk in upstream_response.aiter_bytes():
                    if max_bytes is not None:
                        remaining = max_bytes - bytes_yielded
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            yield chunk[:remaining]
                            bytes_yielded += remaining
                            break
                    yield chunk
                    bytes_yielded += len(chunk)
            except Exception as e:
                logger.error(f"Error during streaming: {e}")
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            stream_iterator(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=response_headers.get("content-type"),
        )
    except HTTPException:
        raise
    except Exception as e:
        if upstream_response is not None and not upstream_response.is_closed:
            await upstream_response.aclose()
        logger.exception(f"Unexpected error in stream_file: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def _cleanup_ffmpeg_process(process: asyncio.subprocess.Process) -> None:
    """Terminate and reap an FFmpeg process without suppressing cancellation."""
    if process.returncode is not None:
        await process.wait()
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        process.kill()
        await process.wait()


def _get_video_duration(path: str, headers: Mapping[str, str] | None = None) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "-headers",
                build_ffmpeg_headers(headers),
                "-i",
                path,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
        return float(result.stdout)
    except Exception:
        return 0.0


# ... imports ...


# 1. Playlist: Defaults are now None (Original Quality)
@router.get("/hls/{item_id}/index.m3u8")
async def get_hls_playlist(
    item_id: int,
    request: Request,
    # Default to None = Keep Original
    pix_fmt: str | None = None,
    video_profile: str | None = Query(None, alias="profile"),
    level: str | None = None,
    resolution: str | None = None,
):
    pix_fmt, video_profile, level, resolution = validate_hls_params(
        pix_fmt=pix_fmt,
        video_profile=video_profile,
        level=level,
        resolution=resolution,
    )
    media_info = _get_media_info(item_id)
    url = media_info[0]
    duration = _get_video_duration(url, request.headers)

    segment_duration = 12
    if duration == 0:
        num_segments = 10
    else:
        num_segments = math.ceil(duration / segment_duration)

    # Build query params ONLY if they exist
    params = list[str]()

    if pix_fmt:
        params.append(f"pix_fmt={pix_fmt}")
    if video_profile:
        params.append(f"profile={video_profile}")
    if level:
        params.append(f"level={level}")
    if resolution:
        params.append(f"resolution={resolution}")

    query_string = f"?{'&'.join(params)}" if params else ""

    m3u8_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:7",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]

    for i in range(num_segments):
        m3u8_lines.append("#EXT-X-DISCONTINUITY")
        m3u8_lines.append(f"#EXTINF:{segment_duration:.6f},")
        # Segments will inherit the params (or lack thereof)
        m3u8_lines.append(f"segment/{i}.ts{query_string}")

    m3u8_lines.append("#EXT-X-ENDLIST")

    return Response(
        content="\n".join(m3u8_lines), media_type="application/vnd.apple.mpegurl"
    )


# 2. Segment: Defaults are None, apply flags only if requested
@router.get("/hls/{item_id}/segment/{seq}.ts")
async def get_hls_segment(
    item_id: int,
    seq: int,
    request: Request,
    pix_fmt: str | None = None,
    video_profile: str | None = Query(None, alias="profile"),
    level: str | None = None,
    resolution: str | None = None,
):
    pix_fmt, video_profile, level, resolution = validate_hls_params(
        pix_fmt=pix_fmt,
        video_profile=video_profile,
        level=level,
        resolution=resolution,
    )
    media_info = _get_media_info(item_id)
    url = media_info[0]

    segment_duration = 12
    start_time = seq * segment_duration

    args = [
        "-analyzeduration",
        "0",
        "-probesize",
        "5000000",
        "-ss",
        str(start_time),
        "-t",
        str(segment_duration),
        "-headers",
        build_ffmpeg_headers(request.headers),
        "-i",
        url,
    ]

    if resolution:
        args.extend(["-vf", scale_filter_for_resolution(resolution)])

    args.extend(["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23"])

    if pix_fmt:
        args.extend(["-pix_fmt", pix_fmt])
    if video_profile:
        args.extend(["-profile:v", video_profile])
    if level:
        args.extend(["-level", level])

    args.extend(["-c:a", "aac", "-b:a", "128k", "-muxdelay", "0", "-f", "mpegts", "-"])

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def stream_output():
        try:
            if not process.stdout or not process.stderr:
                return
            while True:
                chunk = await process.stdout.read(32 * 1024)
                if not chunk:
                    # If no data, check for errors
                    if process.returncode and process.returncode != 0:
                        err = await process.stderr.read()
                        logger.error(f"FFmpeg Error: {err.decode()}")
                    break
                yield chunk
        finally:
            await _cleanup_ffmpeg_process(process)

    return StreamingResponse(stream_output(), media_type="video/mp2t")
