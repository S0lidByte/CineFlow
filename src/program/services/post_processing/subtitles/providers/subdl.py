"""
SubDL subtitle provider.

Ports the riven-ts ``plugin-subdl`` search + ZIP download flow into CineFlow's
``SubtitleProvider`` interface. SubDL prefers TMDB IDs and returns ISO 639-1
language codes; downloads are ZIP archives containing ``.srt`` files.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any, cast
from urllib.parse import urljoin

import httpx
from babelfish import Error as BabelfishError
from babelfish import Language
from loguru import logger

from .base import SubtitleItem, SubtitleProvider
from .opensubtitles import normalize_language_to_alpha3

SUBDL_API_BASE = "https://api.subdl.com/api/v1/"
SUBDL_DOWNLOAD_BASE = "https://dl.subdl.com"
SUBDL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def alpha3_to_alpha2(language: str) -> str:
    """Convert ISO 639-3 (or similar) to ISO 639-1 for SubDL queries."""
    try:
        language_str = str(language).strip().lower()
        if not language_str:
            return "en"
        if len(language_str) == 2:
            return language_str
        lang_obj = Language(language_str)
        alpha2 = getattr(lang_obj, "alpha2", None)
        if alpha2:
            return cast(str, alpha2)
    except (BabelfishError, ValueError, KeyError, AttributeError):
        pass
    return "en"


def extract_srt_from_zip(buffer: bytes) -> str | None:
    """Extract the first ``.srt`` file from a ZIP archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(buffer)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".srt") and not name.endswith("/"):
                    raw = zf.read(name)
                    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
                        try:
                            return raw.decode(encoding)
                        except UnicodeDecodeError:
                            continue
                    return raw.decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        logger.warning(f"SubDL ZIP extract failed: {exc}")
    return None


class SubDLProvider(SubtitleProvider):
    """SubDL REST provider (search + ZIP download)."""

    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self._client = httpx.Client(
            base_url=SUBDL_API_BASE,
            timeout=SUBDL_TIMEOUT,
            follow_redirects=True,
        )

    @property
    def name(self) -> str:
        return "subdl"

    def initialize(self) -> None:
        if not self.api_key:
            raise ValueError("SubDL API key is required")

    def close(self) -> None:
        self._client.close()

    def search_subtitles(
        self,
        imdb_id: str,
        video_hash: str | None = None,
        file_size: int | None = None,
        filename: str | None = None,
        search_tags: str | None = None,
        season: int | None = None,
        episode: int | None = None,
        language: str = "en",
        tmdb_id: str | None = None,
    ) -> list[SubtitleItem]:
        del video_hash, file_size, filename, search_tags  # unused by SubDL

        if not tmdb_id and not imdb_id:
            logger.debug("SubDL search skipped: no TMDB or IMDB ID")
            return []

        media_type = "tv" if season is not None and episode is not None else "movie"
        lang_alpha2 = alpha3_to_alpha2(language)
        target_alpha3 = normalize_language_to_alpha3(language)

        params: dict[str, Any] = {
            "api_key": self.api_key,
            "type": media_type,
            "subs_per_page": "30",
            "languages": lang_alpha2,
        }
        if tmdb_id:
            params["tmdb_id"] = str(tmdb_id).removeprefix("tmdb:")
        elif imdb_id:
            raw = str(imdb_id).strip()
            if not raw.startswith("tt"):
                raw = f"tt{raw}" if raw.isdigit() else raw
            params["imdb_id"] = raw

        if media_type == "tv":
            params["season_number"] = str(season)
            params["episode_number"] = str(episode)

        try:
            response = self._client.get("subtitles", params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.error(f"SubDL search failed: {exc}")
            return []

        if not payload.get("status"):
            logger.warning(
                f"SubDL search error: {payload.get('error', 'unknown error')}"
            )
            return []

        results: list[SubtitleItem] = []
        for sub in payload.get("subtitles") or []:
            if not isinstance(sub, dict):
                continue
            if media_type == "tv":
                if sub.get("season") != season or sub.get("episode") != episode:
                    continue
            sub_lang = str(sub.get("lang") or "").lower()
            if sub_lang and alpha3_to_alpha2(sub_lang) != lang_alpha2:
                # API may still return other langs; keep exact match when present
                if normalize_language_to_alpha3(sub_lang) != target_alpha3:
                    continue

            url = str(sub.get("url") or "")
            if not url:
                continue
            release = str(sub.get("release_name") or sub.get("name") or "subtitle.srt")
            results.append(
                SubtitleItem(
                    id=url,
                    language=target_alpha3,
                    filename=release if release.lower().endswith(".srt") else f"{release}.srt",
                    download_count=0,
                    rating=0.0,
                    matched_by="tmdb" if tmdb_id else "imdb",
                    movie_hash=None,
                    movie_name=str(sub.get("name") or "") or None,
                    provider=self.name,
                    score=50.0,
                )
            )

        return results

    def download_subtitle(self, subtitle_info: SubtitleItem) -> str | None:
        url = subtitle_info.id
        if not url:
            return None
        download_url = (
            url if url.startswith("http") else urljoin(SUBDL_DOWNLOAD_BASE + "/", url.lstrip("/"))
        )
        try:
            response = self._client.get(download_url, timeout=httpx.Timeout(60.0))
            response.raise_for_status()
            content = extract_srt_from_zip(response.content)
            if content is None:
                logger.warning(f"SubDL download had no .srt in ZIP: {download_url}")
            return content
        except Exception as exc:
            logger.error(f"SubDL download failed ({download_url}): {exc}")
            return None
