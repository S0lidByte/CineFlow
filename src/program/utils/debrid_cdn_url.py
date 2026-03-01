from typing import Self
import time
import httpx
from loguru import logger

from http import HTTPStatus
from kink import di

from program.settings import settings_manager
from program.services.streaming.media_stream import PROXY_REQUIRED_PROVIDERS
from program.services.streaming.exceptions import (
    DebridServiceLinkUnavailable,
)
from program.media.media_entry import MediaEntry
from program.db.db import db_session


class RefreshedURLIdenticalException(Exception):
    """Exception raised when a refreshed URL is identical to the previous URL."""

    pass


class DebridCDNUrl:
    """DebridCDNUrl class"""

    def __init__(self, entry: MediaEntry) -> None:
        self.filename = entry.original_filename
        self.entry = entry

        self.max_validation_attempts = 3
        self.url = entry.unrestricted_url
        self.provider = entry.provider or "Unknown provider"
        self._refresh_cooldown_until: float | None = None

    def _set_refresh_cooldown(self, retry_after_seconds: float | None) -> None:
        if retry_after_seconds is None:
            return

        retry_after = max(0.0, float(retry_after_seconds))

        if retry_after == 0:
            return

        self._refresh_cooldown_until = time.monotonic() + retry_after

    def _get_refresh_cooldown_remaining(self) -> float:
        if self._refresh_cooldown_until is None:
            return 0.0

        remaining = self._refresh_cooldown_until - time.monotonic()

        if remaining <= 0:
            self._refresh_cooldown_until = None
            return 0.0

        return remaining

    def _refresh_with_cooldown(self) -> str | None:
        cooldown_remaining = self._get_refresh_cooldown_remaining()

        if cooldown_remaining > 0:
            logger.warning(
                f"Skipping CDN URL refresh due to active cooldown ({cooldown_remaining:.1f}s remaining)"
            )
            return None

        try:
            return self._refresh()
        except Exception as e:
            retry_after = getattr(e, "retry_after_seconds", None)

            if isinstance(retry_after, (int, float)):
                self._set_refresh_cooldown(float(retry_after))
                cooldown = self._get_refresh_cooldown_remaining()

                logger.warning(
                    f"CDN URL refresh deferred due to upstream circuit breaker ({cooldown:.1f}s cooldown)"
                )

                return None

            raise

    @classmethod
    def from_filename(cls, filename: str) -> Self:
        """Create DebridCDNUrl from filename."""

        with db_session() as session:
            entry = (
                session.query(MediaEntry)
                .filter(MediaEntry.original_filename == filename)
                .first()
            )

            if not entry:
                raise ValueError("Could not find entry info for CDN URL validation")

            return cls(entry)

    def validate(
        self,
        attempt_refresh: bool = True,
        attempt: int = 1,
    ) -> str | None:
        """Get a validated CDN URL, refreshing if requested."""

        try:
            # Assert URL availability by opening a stream, using a proxy if needed
            proxy = (
                self.provider in PROXY_REQUIRED_PROVIDERS
                and settings_manager.settings.downloaders.proxy_url
                or None
            )

            try:
                # If no URL is set, attempt to refresh it first if requested,
                # otherwise return as an invalid URL
                if not self.url:
                    if attempt_refresh:
                        if url := self._refresh_with_cooldown():
                            self.url = url
                        else:
                            return None
                    else:
                        return None

                with httpx.Client(proxy=proxy) as client:
                    with client.stream(method="GET", url=self.url) as response:
                        response.raise_for_status()

                        return self.url
            except httpx.TimeoutException as e:
                logger.error(f"Timeout while validating CDN URL {self.url}: {e}")
            except httpx.ConnectError as e:
                logger.error(
                    f"Connection error while validating CDN URL {self.url}: {e}"
                )
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code

                if (
                    status_code in (HTTPStatus.NOT_FOUND, HTTPStatus.GONE)
                    and attempt == 1
                ):
                    # Only attempt to refresh the URL on the first failure
                    if attempt_refresh:
                        if url := self._refresh_with_cooldown():
                            self.url = url
                    else:
                        return None
            except RefreshedURLIdenticalException:
                raise
            except Exception as e:
                logger.error(
                    f"Unexpected error while validating CDN URL {self.url}: {e}"
                )

                return None

            if self._get_refresh_cooldown_remaining() > 0:
                return None

            if attempt < self.max_validation_attempts:
                return self.validate(
                    attempt_refresh=attempt_refresh,
                    attempt=attempt + 1,
                )

            return None
        except RefreshedURLIdenticalException as e:
            # If the URL hasn't changed after refreshing, it is likely dead.
            # Raise an exception to indicate the link is unavailable to trigger a re-scrape.
            raise DebridServiceLinkUnavailable(
                provider=self.provider,
                link=self.url or "Unknown URL",
            ) from e

    def _refresh(self) -> str | None:
        """Refresh the CDN URL."""

        from program.services.filesystem.vfs.db import VFSDatabase

        with db_session() as session:
            entry = session.merge(self.entry)

            url = di[VFSDatabase].refresh_unrestricted_url(
                entry=entry,
                session=session,
            )

            if not url:
                logger.error("Could not refresh CDN URL; no URL returned from refresh")

                return None

            if url == self.url:
                raise RefreshedURLIdenticalException()

            self.url = url

            return self.url
