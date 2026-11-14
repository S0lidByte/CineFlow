"""Plex Updater module"""

from kink import di
from loguru import logger
from plexapi.exceptions import BadRequest, Unauthorized
from plexapi.library import LibrarySection
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
)
from requests.exceptions import (
    RequestException,
)
from requests.exceptions import (
    Timeout as RequestsTimeout,
)
from urllib3.exceptions import MaxRetryError, NewConnectionError

from program.apis.plex_api import PlexAPI
from program.services.updaters.base import BaseUpdater
from program.settings import settings_manager
from program.utils.url_sanitizer import sanitize_url_for_logs


class PlexUpdater(BaseUpdater):
    def __init__(self):
        super().__init__("plexupdater")
        self.library_path = settings_manager.settings.updaters.library_path
        self.settings = settings_manager.settings.updaters.plex
        self.api = None
        self.sections = dict[LibrarySection, list[str]]()
        self._initialize()

    def validate(self) -> bool:
        """Validate Plex library"""

        if not self.settings.enabled:
            return False

        if not self.settings.token:
            logger.error("Plex token is not set!")
            return False

        if not self.settings.url:
            logger.error("Plex URL is not set!")
            return False

        if not self.library_path:
            logger.error("Library path is not set!")
            return False

        try:
            url = self.settings.url.rstrip("/") if self.settings.url else ""
            token = self.settings.token.strip() if self.settings.token else ""

            try:
                self.api = di[PlexAPI]
                self.api.BASE_URL = url
                self.api.token = token
            except Exception:
                self.api = PlexAPI(token, url)
                di[PlexAPI] = self.api

            self.api.validate_server()
            self.sections = self.api.map_sections_with_paths()
            self.initialized = True

            return True
        except Unauthorized as e:
            logger.error(
                f"Plex is not authorized at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except BadRequest as e:
            logger.error(
                f"Plex server rejected request (check configuration) at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except (RequestsConnectionError, NewConnectionError) as e:
            logger.error(
                f"Failed to connect to Plex at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except (RequestsTimeout, TimeoutError) as e:
            logger.error(
                f"Connection to Plex timed out at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except MaxRetryError as e:
            logger.error(
                f"Plex connection max retries exceeded at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except RequestException as e:
            logger.error(
                f"Plex network request error at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        except Exception as e:
            logger.exception(
                f"Unexpected error validating Plex server at {sanitize_url_for_logs(self.settings.url)}: {e}"
            )
        return False

    def refresh_path(self, path: str) -> bool:
        """Refresh a specific path in Plex by finding the matching section"""

        assert self.api is not None, "Plex API is not initialized"

        for section, section_paths in self.sections.items():
            for section_path in section_paths:
                sp_dir = section_path.rstrip("/") + "/"
                if path == section_path or path.startswith(sp_dir):
                    return self.api.update_section(section, path)

        return False

    def empty_trash(self, path: str) -> bool:
        """Empty trash for the Plex section matching the given path"""

        assert self.api is not None, "Plex API is not initialized"

        for section, section_paths in self.sections.items():
            for section_path in section_paths:
                sp_dir = section_path.rstrip("/") + "/"
                if path == section_path or path.startswith(sp_dir):
                    return self.api.empty_trash_section(section)

        return False
