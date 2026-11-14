"""Jellyfin Updater module"""

from loguru import logger
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
)
from requests.exceptions import (
    Timeout as RequestsTimeout,
)

from program.services.updaters.base import BaseUpdater
from program.settings import settings_manager
from program.utils.request import SmartSession
from program.utils.url_sanitizer import sanitize_url_for_logs


class JellyfinUpdater(BaseUpdater):
    """Jellyfin media server updater implementation"""

    def __init__(self):
        super().__init__("jellyfin")

        self.settings = settings_manager.settings.updaters.jellyfin
        self.session = SmartSession(retries=3, backoff_factor=0.3)
        self._initialize()

    def validate(self) -> bool:
        """Validate Jellyfin configuration and connectivity"""

        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.error("Jellyfin API key is not set!")
            return False

        if not self.settings.url:
            logger.error("Jellyfin URL is not set!")
            return False

        try:
            url = self.settings.url.rstrip("/")
            response = self.session.get(
                f"{url}/Users", params={"api_key": self.settings.api_key}
            )

            if response.ok:
                return True
            logger.error(
                f"Jellyfin validation returned HTTP {response.status_code} at {sanitize_url_for_logs(url)}"
            )
        except (RequestsConnectionError, RequestsTimeout) as e:
            logger.error(
                f"Failed to connect to Jellyfin at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except Exception as e:
            logger.exception(
                f"Unexpected error validating Jellyfin at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )

        return False

    def refresh_path(self, path: str) -> bool:
        """
        Refresh Jellyfin library.

        Note: Jellyfin's API refreshes the entire library, not individual paths.
        The path parameter is ignored.
        """

        try:
            response = self.session.post(
                f"{self.settings.url}/Library/Refresh",
                params={"api_key": self.settings.api_key},
            )

            return response.ok
        except Exception as e:
            logger.error(f"Failed to refresh Jellyfin library: {e}")
            return False
