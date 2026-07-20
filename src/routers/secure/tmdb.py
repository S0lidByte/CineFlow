from collections.abc import Iterable

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status

from program.apis.tmdb_api import TMDB_READ_ACCESS_TOKEN

router = APIRouter(prefix="/tmdb", tags=["TMDB"])

TMDB_BASE_URL = "https://api.themoviedb.org"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
FORWARDED_RESPONSE_HEADERS = {
    "cache-control",
    "content-type",
    "etag",
    "expires",
    "last-modified",
}


def _validate_tmdb_path(tmdb_path: str) -> str:
    normalized_path = tmdb_path.strip("/")

    if not normalized_path or normalized_path.startswith(("http:", "https:", "//")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid TMDB path",
        )

    if not normalized_path.startswith("3/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TMDB path must start with /3/",
        )

    path_parts = normalized_path.split("/")
    if any(part in {"", ".", ".."} for part in path_parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid TMDB path",
        )

    return normalized_path


def _copy_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in FORWARDED_RESPONSE_HEADERS
        and key.lower() not in HOP_BY_HOP_HEADERS
    }


def _query_items(request: Request) -> Iterable[tuple[str, str]]:
    return request.query_params.multi_items()


@router.get(
    "/{tmdb_path:path}",
    operation_id="proxy_tmdb_get",
    summary="Proxy TMDB GET requests",
)
async def proxy_tmdb_get(tmdb_path: str, request: Request) -> Response:
    upstream_path = _validate_tmdb_path(tmdb_path)

    async with httpx.AsyncClient(base_url=TMDB_BASE_URL, timeout=20.0) as client:
        upstream_response = await client.get(
            f"/{upstream_path}",
            params=list(_query_items(request)),
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {TMDB_READ_ACCESS_TOKEN}",
            },
        )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_copy_response_headers(upstream_response.headers),
        media_type=upstream_response.headers.get("content-type"),
    )
