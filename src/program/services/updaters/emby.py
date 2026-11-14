"""Emby Updater module"""

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


class EmbyUpdater(BaseUpdater):
    """Emby media server updater implementation"""

    def __init__(self):
        super().__init__("emby")

        self.settings = settings_manager.settings.updaters.emby
        self.session = SmartSession(retries=3, backoff_factor=0.3)
        self._initialize()

    def validate(self) -> bool:
        """Validate Emby configuration and connectivity"""

        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.error("Emby API key is not set!")
            return False

        if not self.settings.url:
            logger.error("Emby URL is not set!")
            return False

        try:
            url = self.settings.url.rstrip("/")
            response = self.session.get(
                f"{url}/Users", params={"api_key": self.settings.api_key}
            )
            if response.ok:
                return True
            logger.error(
                f"Emby validation returned HTTP {response.status_code} at {sanitize_url_for_logs(url)}"
            )
        except (RequestsConnectionError, RequestsTimeout) as e:
            logger.error(
                f"Failed to connect to Emby at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except Exception as e:
            logger.exception(
                f"Unexpected error validating Emby at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )

        return False

    def refresh_path(self, path: str) -> bool:
        """Refresh a specific path in Emby"""

        try:
            response = self.session.post(
                f"{self.settings.url}/Library/Media/Updated",
                json={"Updates": [{"Path": path, "UpdateType": "Created"}]},
                params={"api_key": self.settings.api_key},
            )

            return response.ok
        except Exception as e:
            logger.error(f"Failed to refresh Emby path {path}: {e}")
            return False
