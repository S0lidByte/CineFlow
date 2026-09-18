from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Any, Generic, Literal, TypeVar, cast

from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from program.contracts.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    normalize_provider_error,
)
from program.media.item import ProcessedItemType
from program.services.downloaders.models import (
    DebridFile,
    InvalidDebridFileException,
    TorrentContainer,
    TorrentInfo,
    UnrestrictedLink,
    UserInfo,
)
from program.settings import settings_manager
from program.utils.request import CircuitBreakerOpen, SmartResponse, SmartSession

from .shared import DebridVpnBlockedError, DownloaderBase, premium_days_left


class AllDebridErrorCode(str, Enum):
    """Canonical AllDebrid v4/v4.1 API error codes."""

    # Authentication & Credentials
    AUTH_MISSING_APIKEY = "AUTH_MISSING_APIKEY"
    AUTH_BAD_APIKEY = "AUTH_BAD_APIKEY"
    AUTH_USER_BANNED = "AUTH_USER_BANNED"
    AUTH_USER_NOT_PREMIUM = "AUTH_USER_NOT_PREMIUM"

    # PIN OAuth Authentication
    PIN_ALREADY_GENERATED = "PIN_ALREADY_GENERATED"
    PIN_EXPIRED = "PIN_EXPIRED"
    PIN_INVALID = "PIN_INVALID"
    PIN_ALREADY_AUTHENTIFIED = "PIN_ALREADY_AUTHENTIFIED"

    # Quota & Rate Limits
    FREE_TRIAL_LIMIT_REACHED = "FREE_TRIAL_LIMIT_REACHED"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    MAGNET_TOO_MANY = "MAGNET_TOO_MANY"
    MAGNET_MUST_BE_PREMIUM = "MAGNET_MUST_BE_PREMIUM"
    MAGNET_TOO_LARGE = "MAGNET_TOO_LARGE"
    LINK_TOO_MANY_DOWNLOADS = "LINK_TOO_MANY_DOWNLOADS"

    # Server Availability & Network/VPN Blocking
    MAGNET_NO_SERVER = "MAGNET_NO_SERVER"
    NO_SERVER = "NO_SERVER"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # Magnet / Torrent Processing
    MAGNET_INVALID_ID = "MAGNET_INVALID_ID"
    MAGNET_INVALID_FILE = "MAGNET_INVALID_FILE"
    MAGNET_NO_URI = "MAGNET_NO_URI"
    MAGNET_PROCESSING = "MAGNET_PROCESSING"
    MAGNET_NOT_FOUND = "MAGNET_NOT_FOUND"

    # Link Unrestrict / Hoster Errors
    LINK_IS_MISSING = "LINK_IS_MISSING"
    LINK_HOST_NOT_SUPPORTED = "LINK_HOST_NOT_SUPPORTED"
    LINK_DOWN = "LINK_DOWN"
    LINK_PASS_PROTECTED = "LINK_PASS_PROTECTED"  # noqa: S105
    LINK_HOST_UNAVAILABLE = "LINK_HOST_UNAVAILABLE"
    LINK_HOST_FULL = "LINK_HOST_FULL"
    REDIRECTOR_NOT_SUPPORTED = "REDIRECTOR_NOT_SUPPORTED"
    LINK_ERROR = "LINK_ERROR"

    # Generic Fallback
    GENERIC_ERROR = "GENERIC_ERROR"


class AllDebridMagnetStatusCode(IntEnum):
    """AllDebrid magnet numeric status codes."""

    IN_QUEUE = 0
    DOWNLOADING = 1
    COMPRESSING = 2
    UPLOADING = 3
    READY = 4
    UPLOAD_FAILED = 5
    ERROR = 6
    BAD_TORRENT = 7
    NOT_FOUND = 8
    DELETED = 9
    SERVER_MAINTENANCE = 10
    PAUSED = 11


class AllDebridFile(BaseModel):
    """Represents a file in AllDebrid's torrent structure."""

    n: str  # Name / path
    s: int = 0  # Size in bytes
    l: str = ""  # Download link


def parse_alldebrid_entry(v: Any) -> "AllDebridFile | AllDebridDirectory":
    """Parse a file or directory node from AllDebrid's recursive file tree."""
    if isinstance(v, (AllDebridFile, AllDebridDirectory)):
        return v
    if isinstance(v, dict):
        raw_dict = cast(dict[str, Any], v)
        if "e" in raw_dict and raw_dict["e"] is not None:
            raw_entries = cast(list[Any], raw_dict.get("e", []))
            entries = [parse_alldebrid_entry(item) for item in raw_entries]
            return AllDebridDirectory(n=str(raw_dict.get("n", "")), e=entries)
        return AllDebridFile(
            n=str(raw_dict.get("n", "")),
            s=int(raw_dict.get("s", 0)),
            l=str(raw_dict.get("l", "")),
        )
    raise ValueError(f"Invalid AllDebrid entry: {v}")


class AllDebridDirectory(BaseModel):
    """Represents a directory in AllDebrid's torrent structure."""

    n: str  # Name
    e: list[Any] = Field(default_factory=list)  # Entries (files and subdirectories)

    @field_validator("e", mode="before")
    @classmethod
    def _validate_entries(cls, v: Any) -> list[Any]:
        if isinstance(v, list):
            entry_list = cast(list[Any], v)
            return [parse_alldebrid_entry(item) for item in entry_list]
        return []


class AllDebridErrorDetail(BaseModel):
    """Normalized AllDebrid error details supporting string, dict, or code-only payloads."""

    code: str = AllDebridErrorCode.GENERIC_ERROR.value
    message: str = "An unknown AllDebrid error occurred"

    @model_validator(mode="before")
    @classmethod
    def _validate_detail(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"code": value, "message": value}
        if isinstance(value, dict):
            dict_val = cast(dict[str, Any], value)
            if "error" in dict_val and isinstance(dict_val["error"], dict):
                inner = cast(dict[str, Any], dict_val["error"])
                raw_code: Any = inner.get("code") or "GENERIC_ERROR"
                raw_message: Any = inner.get("message") or str(raw_code)
            else:
                raw_code = (
                    dict_val.get("code") or dict_val.get("error") or "GENERIC_ERROR"
                )
                raw_message = (
                    dict_val.get("message") or dict_val.get("error") or str(raw_code)
                )
            return {"code": str(raw_code), "message": str(raw_message)}
        return value


class AllDebridErrorResponse(BaseModel):
    """Represents an AllDebrid API error response envelope."""

    status: Literal["error"] = "error"
    error: AllDebridErrorDetail

    @model_validator(mode="before")
    @classmethod
    def _validate_envelope(cls, value: Any) -> Any:
        if isinstance(value, dict):
            dict_val = cast(dict[str, Any], value)
            if "error" not in dict_val and (
                "message" in dict_val or "code" in dict_val
            ):
                res: dict[str, Any] = {
                    "status": str(dict_val.get("status", "error")),
                    "error": {
                        "code": str(dict_val.get("code", "GENERIC_ERROR")),
                        "message": str(dict_val.get("message", "Unknown error")),
                    },
                }
                return res
            return dict_val
        return value


T = TypeVar("T", bound=BaseModel | None)


class AllDebridSuccessResponse(BaseModel, Generic[T]):
    """Represents a generic AllDebrid API success response."""

    status: Literal["success"]
    data: T


class AllDebridResponse(BaseModel, Generic[T]):
    """Union of AllDebrid success and error responses."""

    data: AllDebridErrorResponse | AllDebridSuccessResponse[T] = Field(
        discriminator="status"
    )


class AllDebridMagnet(BaseModel):
    """Represents magnet upload/creation information returned by AllDebrid."""

    model_config = ConfigDict(populate_by_name=True)

    class MagnetInfo(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        id: int
        magnet: str = ""
        hash: str = ""
        name: str = ""
        size: int = 0
        ready: bool = False
        filename_original: str = ""

    magnets: list[MagnetInfo]

    @field_validator("magnets", mode="before")
    @classmethod
    def _ensure_list(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [cast(dict[str, Any], value)]
        return value


class AllDebridUserResponse(BaseModel):
    """Represents user information returned by AllDebrid."""

    model_config = ConfigDict(populate_by_name=True)

    class UserData(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        username: str
        email: str = ""
        is_premium: bool = Field(alias="isPremium")
        premium_until: int = Field(default=0, alias="premiumUntil")
        fidelity_points: int = Field(default=0, alias="fidelityPoints")

    user: UserData


class AllDebridLinkUnlockResponse(BaseModel):
    """Represents link unlock response from AllDebrid."""

    model_config = ConfigDict(populate_by_name=True)

    link: str = ""
    filename: str = ""
    filesize: int = 0
    id: str | int | None = None
    host: str | None = None
    p2p: bool | None = None


class AllDebridMagnetStatusResponse(BaseModel):
    """Represents magnet status information returned by AllDebrid v4/v4.1."""

    model_config = ConfigDict(populate_by_name=True)

    class MagnetInfo(BaseModel):
        model_config = ConfigDict(populate_by_name=True)

        id: int
        filename: str = ""
        size: int = 0
        status: str = ""
        status_code: int = Field(default=0, alias="statusCode")
        downloaded: int = 0
        uploaded: int = 0
        seeders: int = 0
        download_speed: int = Field(default=0, alias="downloadSpeed")
        upload_speed: int = Field(default=0, alias="uploadSpeed")
        upload_date: int = Field(default=0, alias="uploadDate")
        completion_date: int = Field(default=0, alias="completionDate")
        links: list[Any] = Field(default_factory=list)
        hash: str | None = None
        type: str | None = None
        notified: bool | None = None
        version: int | None = None
        processing_perc: int | None = Field(default=None, alias="processingPerc")

    class MagnetErrorInfo(BaseModel):
        id: str | int = ""
        error: AllDebridErrorDetail

    magnets: list[MagnetInfo | MagnetErrorInfo]

    @field_validator("magnets", mode="before")
    @classmethod
    def _ensure_list(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [cast(dict[str, Any], value)]
        return value


class AllDebridMagnetFilesResponse(BaseModel):
    """Represents file trees and links returned by AllDebrid's dedicated magnet/files API."""

    class MagnetFiles(BaseModel):
        id: int
        files: list[Any] = Field(default_factory=list)

        @field_validator("files", mode="before")
        @classmethod
        def _validate_files(cls, v: Any) -> list[Any]:
            if isinstance(v, list):
                files_list = cast(list[Any], v)
                return [parse_alldebrid_entry(item) for item in files_list]
            return []

    class MagnetErrorInfo(BaseModel):
        id: str | int = ""
        error: AllDebridErrorDetail

    magnets: list[MagnetFiles | MagnetErrorInfo]

    @field_validator("magnets", mode="before")
    @classmethod
    def _ensure_list(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return [cast(dict[str, Any], value)]
        return value


class AllDebridPinGetResponse(BaseModel):
    """Represents PIN get response from AllDebrid OAuth device flow."""

    model_config = ConfigDict(populate_by_name=True)

    pin: str
    check: str
    expires_in: int = Field(default=600, alias="expiresIn")
    user_url: str = Field(alias="userUrl")
    base_url: str | None = Field(default=None, alias="baseUrl")
    check_url: str | None = Field(default=None, alias="checkUrl")


class AllDebridPinCheckResponse(BaseModel):
    """Represents PIN check response from AllDebrid OAuth device flow."""

    model_config = ConfigDict(populate_by_name=True)

    activated: bool = False
    expires_in: int | None = Field(default=None, alias="expiresIn")
    apikey: str | None = None


class AllDebridError(ProviderError):
    """Base exception for AllDebrid related errors."""

    def __init__(
        self,
        message: str | None = None,
        code: str | None = None,
        raw_error: Any = None,
        status_code: int | None = None,
    ) -> None:
        msg = message or code or "AllDebrid error"
        super().__init__(
            msg,
            provider_name="AllDebrid",
            raw_error=raw_error
            or ({"code": code, "message": message} if code else None),
            status_code=status_code,
        )
        self.code = code


def _extract_retry_after(response: Any) -> float | None:
    """Extract retry-after seconds from response headers."""
    headers = getattr(response, "headers", {}) or {}
    val = headers.get("Retry-After") or headers.get("retry-after")
    if val:
        try:
            return float(val)
        except (ValueError, TypeError):
            pass
    return None


def _maybe_backoff(response: Any) -> None:
    """
    Check if we should back off based on response status and rate limit headers.
    Parses Retry-After and X-RateLimit-* headers to dynamically log rate limits.
    """
    headers = getattr(response, "headers", {}) or {}

    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    ratelimit_limit = headers.get("X-RateLimit-Limit") or headers.get(
        "x-ratelimit-limit"
    )
    ratelimit_remaining = headers.get("X-RateLimit-Remaining") or headers.get(
        "x-ratelimit-remaining"
    )
    ratelimit_reset = headers.get("X-RateLimit-Reset") or headers.get(
        "x-ratelimit-reset"
    )

    retry_seconds: float | None = None
    if retry_after:
        try:
            retry_seconds = float(retry_after)
        except (ValueError, TypeError):
            pass

    status_code = getattr(response, "status_code", 200)
    if status_code == 429:
        if retry_seconds is not None:
            logger.warning(
                f"AllDebrid rate limit hit (429), Retry-After={retry_seconds:.1f}s, "
                f"Reset={ratelimit_reset}, Limit={ratelimit_limit}"
            )
        else:
            logger.warning(
                f"AllDebrid rate limit hit (429), Reset={ratelimit_reset}, Limit={ratelimit_limit}"
            )
    elif ratelimit_remaining is not None:
        try:
            if int(ratelimit_remaining) <= 0:
                logger.warning(
                    f"AllDebrid rate limit quota exhausted (Remaining={ratelimit_remaining}, Reset={ratelimit_reset})"
                )
        except (ValueError, TypeError):
            pass


def _error_from_detail(
    error: AllDebridErrorDetail,
    status_code: int | None = None,
    retry_after: float | None = None,
) -> Exception:
    """Map provider error detail codes to typed availability and normalized ProviderError subclasses."""

    code_upper = error.code.upper()

    if code_upper in {"MAGNET_NO_SERVER", "NO_SERVER"}:
        return DebridVpnBlockedError(error.message)

    if code_upper in {
        "AUTH_MISSING_APIKEY",
        "AUTH_BAD_APIKEY",
        "AUTH_USER_BANNED",
        "AUTH_USER_NOT_PREMIUM",
        "PIN_EXPIRED",
        "PIN_INVALID",
        "PIN_ALREADY_AUTHENTIFIED",
    }:
        return ProviderAuthError(
            message=f"AllDebrid auth failed: {error.message}",
            provider_name="AllDebrid",
            status_code=status_code or 401,
            raw_error={"code": error.code, "message": error.message},
        )

    if code_upper in {
        "FREE_TRIAL_LIMIT_REACHED",
        "MAGNET_TOO_MANY",
        "MAGNET_MUST_BE_PREMIUM",
        "LINK_TOO_MANY_DOWNLOADS",
        "MAGNET_TOO_LARGE",
    }:
        return ProviderQuotaExceededError(
            message=f"AllDebrid quota exceeded: {error.message}",
            provider_name="AllDebrid",
            status_code=status_code,
            raw_error={"code": error.code, "message": error.message},
        )

    if code_upper in {"RATE_LIMIT_EXCEEDED", "TOO_MANY_REQUESTS"}:
        return ProviderRateLimitError(
            message=f"AllDebrid rate limit exceeded: {error.message}",
            provider_name="AllDebrid",
            retry_after_seconds=retry_after,
            status_code=status_code or 429,
            raw_error={"code": error.code, "message": error.message},
        )

    if code_upper in {"SERVICE_UNAVAILABLE", "MAINTENANCE", "SERVER_MAINTENANCE"}:
        return ProviderUnavailableError(
            message=f"AllDebrid service unavailable: {error.message}",
            provider_name="AllDebrid",
            status_code=status_code or 503,
            raw_error={"code": error.code, "message": error.message},
        )

    return AllDebridError(
        message=error.message,
        code=error.code,
        status_code=status_code,
        raw_error={"code": error.code, "message": error.message},
    )


class AllDebridAPI:
    """
    Minimal AllDebrid API client using SmartSession for retries, rate limits, and circuit breaker.
    """

    BASE_URL = "https://api.alldebrid.com/"

    def __init__(self, api_key: str, proxy_url: str | None = None) -> None:
        """
        Args:
            api_key: AllDebrid API key.
            proxy_url: Optional proxy URL used for both HTTP and HTTPS.
        """

        self.api_key = api_key
        self.proxy_url = proxy_url

        # AllDebrid rate limits: 12 req/sec and 600 req/min
        # Using conservative 10 req/sec (600 capacity)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={
                "api.alldebrid.com": {
                    "rate": 10,
                    "capacity": 600,
                },
            },
            proxies=proxies,
            retries=2,
            backoff_factor=0.5,
        )

        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def get_pin(self) -> AllDebridPinGetResponse:
        """Fetch a new PIN code for OAuth device flow."""
        response = self.session.get("v4/pin/get")
        _maybe_backoff(response)

        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            try:
                data = (
                    AllDebridResponse[AllDebridPinGetResponse]
                    .model_validate({"data": payload})
                    .data
                )
                if isinstance(data, AllDebridErrorResponse):
                    raise _error_from_detail(
                        data.error,
                        status_code=response.status_code,
                        retry_after=_extract_retry_after(response),
                    )
                return data.data
            except ValidationError:
                pass

        if not response.ok:
            raise normalize_provider_error(
                provider_name="AllDebrid",
                status_code=response.status_code,
                message=f"Failed to get PIN: HTTP {response.status_code}",
                retry_after=_extract_retry_after(response),
            )
        raise AllDebridError(f"Unexpected response getting PIN: {response.text}")

    def check_pin(self, check: str, pin: str) -> AllDebridPinCheckResponse:
        """Check PIN authentication status."""
        response = self.session.get("v4/pin/check", params={"check": check, "pin": pin})
        _maybe_backoff(response)

        try:
            payload = response.json()
        except Exception:
            payload = None

        if isinstance(payload, dict):
            try:
                data = (
                    AllDebridResponse[AllDebridPinCheckResponse]
                    .model_validate({"data": payload})
                    .data
                )
                if isinstance(data, AllDebridErrorResponse):
                    raise _error_from_detail(
                        data.error,
                        status_code=response.status_code,
                        retry_after=_extract_retry_after(response),
                    )
                return data.data
            except ValidationError:
                pass

        if not response.ok:
            raise normalize_provider_error(
                provider_name="AllDebrid",
                status_code=response.status_code,
                message=f"Failed to check PIN: HTTP {response.status_code}",
                retry_after=_extract_retry_after(response),
            )
        raise AllDebridError(f"Unexpected response checking PIN: {response.text}")


class AllDebridDownloader(DownloaderBase):
    """
    AllDebrid downloader with lean exception handling aligned to AllDebrid v4.1 contracts.

    Notes on failure & breaker behavior:
    - Network/transport failures are retried by SmartSession, then counted against the per-domain
      CircuitBreaker; once OPEN, SmartSession raises CircuitBreakerOpen before the request.
    - HTTP status codes and error envelopes are mapped to normalized ProviderError subclasses.
    """

    def __init__(self) -> None:
        self.key = "alldebrid"
        self.settings = settings_manager.settings.downloaders.all_debrid
        self.api: AllDebridAPI | None = None
        self.initialized = self.validate()

    def validate(self) -> bool:
        """
        Validate settings and current premium status.

        Returns:
            True if ready, else False.
        """

        if not self._validate_settings():
            return False

        proxy_url = self.PROXY_URL or None

        self.api = AllDebridAPI(api_key=self.settings.api_key, proxy_url=proxy_url)

        return self._validate_premium()

    def _validate_settings(self) -> bool:
        """
        Returns:
            True when enabled and API key present; otherwise False.
        """

        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.warning("AllDebrid API key is not set")
            return False

        return True

    def _validate_premium(self) -> bool:
        """
        Returns:
            True if premium is active; otherwise False.
        """
        try:
            user_info = self.get_user_info()

            if not user_info:
                logger.error("Failed to get AllDebrid user info")
                return False

            if user_info.premium_status != "premium":
                logger.error("AllDebrid premium membership required")
                return False

            if user_info.premium_expires_at:
                logger.info(premium_days_left(user_info.premium_expires_at))

            return True
        except Exception as e:
            logger.error(f"Failed to validate AllDebrid premium status: {e}")
            return False

    def _handle_error(self, response: SmartResponse) -> str:
        """
        Map HTTP status codes and AllDebrid error codes to error messages.
        """

        status = response.status_code

        # Attempt to parse structured error detail from response body
        try:
            raw_json = response.json()
            if isinstance(raw_json, dict):
                data = AllDebridResponse[None].model_validate({"data": raw_json}).data
                if isinstance(data, AllDebridErrorResponse):
                    return f"{data.error.code}: {data.error.message}"
        except Exception:
            pass

        match status:
            case 400:
                return "Bad request"
            case 401:
                return "Unauthorized - check API key"
            case 403:
                return "Forbidden"
            case 404:
                return "Not found"
            case 429:
                return "Rate limit exceeded"
            case _ if status >= 500:
                return "AllDebrid server error"
            case _:
                return f"HTTP {status}"

    def _maybe_backoff(self, response: SmartResponse) -> None:
        """Check if we should back off based on response status and rate limit headers."""
        _maybe_backoff(response)

    @staticmethod
    def _extract_retry_after(response: SmartResponse) -> float | None:
        """Extract retry-after seconds from response headers."""
        return _extract_retry_after(response)

    def _error_from_detail(
        self,
        error: AllDebridErrorDetail,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> Exception:
        """Map provider error detail codes to typed availability and normalized ProviderError subclasses."""
        return _error_from_detail(
            error, status_code=status_code, retry_after=retry_after
        )

    def _error_from_response(self, response: SmartResponse) -> Exception:
        """Extract typed error from response body or classify by HTTP status."""
        try:
            raw_json = response.json()
            if isinstance(raw_json, dict):
                data = AllDebridResponse[None].model_validate({"data": raw_json}).data
                if isinstance(data, AllDebridErrorResponse):
                    return self._error_from_detail(data.error, response.status_code)
        except (ValueError, ValidationError):
            pass

        return normalize_provider_error(
            provider_name="AllDebrid",
            status_code=response.status_code,
            message=self._handle_error(response),
            retry_after=self._extract_retry_after(response),
        )

    def _availability_error(self, response: SmartResponse) -> Exception:
        """Classify a failed availability response before surfacing it upstream."""

        try:
            raw_json = response.json()
            if isinstance(raw_json, dict):
                data = AllDebridResponse[None].model_validate({"data": raw_json}).data
                if isinstance(data, AllDebridErrorResponse):
                    return self._error_from_detail(data.error, response.status_code)
        except (ValueError, ValidationError):
            pass

        return normalize_provider_error(
            provider_name="AllDebrid",
            status_code=response.status_code,
            message=self._handle_error(response),
            retry_after=self._extract_retry_after(response),
        )

    def get_instant_availability(
        self,
        infohash: str,
        item_type: ProcessedItemType,
    ) -> TorrentContainer | None:
        """
        Attempt a quick availability check by adding the magnet to AllDebrid
        and checking if it's instantly available (already cached).

        AllDebrid doesn't have a separate cache check endpoint,
        so we add the magnet and check its status.
        """

        torrent_id: int | None = None

        try:
            torrent_id = self.add_torrent(infohash)
            container, reason, info = self._process_torrent(
                torrent_id, infohash, item_type
            )

            if container is None and reason:
                logger.debug(f"Availability check failed [{infohash}]: {reason}")

                # Failed validation - delete the torrent
                if torrent_id:
                    try:
                        self.delete_torrent(torrent_id)
                    except Exception as e:
                        logger.debug(
                            f"Failed to delete failed torrent {torrent_id}: {e}"
                        )

                return None

            # Success - cache torrent_id AND info in container to avoid re-adding/re-fetching during download
            if container:
                container.torrent_id = torrent_id
                container.torrent_info = info

            return container

        except CircuitBreakerOpen:
            logger.debug(f"Circuit breaker OPEN for AllDebrid; skipping {infohash}")

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            raise
        except (DebridVpnBlockedError, ProviderAuthError):
            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            raise
        except (AllDebridError, ProviderError) as e:
            logger.warning(f"Availability check failed [{infohash}]: {e}")

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            return None
        except InvalidDebridFileException as e:
            logger.debug(
                f"Availability check failed [{infohash}]: Invalid debrid file(s) - {e}"
            )

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            return None
        except Exception as e:
            logger.debug(f"Availability check failed [{infohash}]: {e}")

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            return None

    def _process_torrent(
        self,
        torrent_id: int,
        infohash: str,
        item_type: ProcessedItemType,
    ) -> tuple[TorrentContainer | None, str | None, TorrentInfo | None]:
        """
        Process a single torrent and return (container, reason, info).

        Returns:
            (TorrentContainer or None, human-readable reason string if None, TorrentInfo or None)
        """

        info = self.get_torrent_info(torrent_id)

        if not info:
            return None, "no torrent info returned by AllDebrid", None

        # Check if torrent is ready (statusCode 4 = Ready)
        # Status codes: 0=In Queue, 1=Downloading, 2=Compressing, 3=Uploading, 4=Ready
        if info.status != "Ready":
            return None, f"Not instantly available (status={info.status})", None

        # Get files from the dedicated magnet/files endpoint
        files_data = self._get_magnet_files(torrent_id)

        if not files_data:
            return None, "no files present in the torrent", None

        files = list[DebridFile]()

        # Process files recursively from the nested structure
        self._extract_files_recursive(files_data, item_type, files, infohash)

        if not files:
            return None, "no valid files after validation", None

        # Return container WITH the TorrentInfo to avoid re-fetching in download phase
        return TorrentContainer(infohash=infohash, files=files), None, info

    def _flatten_magnet_files(
        self,
        files: list[AllDebridFile | AllDebridDirectory],
        result: list[AllDebridFile],
    ) -> None:
        """Flatten AllDebrid's file tree without losing leaf download links."""

        for file_obj in files:
            if isinstance(file_obj, AllDebridDirectory):
                self._flatten_magnet_files(file_obj.e, result)
            else:
                result.append(file_obj)

    def _extract_files_recursive(
        self,
        file_list: list[AllDebridFile | AllDebridDirectory],
        item_type: ProcessedItemType,
        files: list[DebridFile],
        infohash: str,
        path_prefix: str = "",
    ) -> None:
        """
        Recursively extract files from AllDebrid's nested file structure.

        AllDebrid returns files with:
        - 'n' (name): filename or folder name
        - 's' (size): file size in bytes (only for files, not folders)
        - 'l' (link): download link (only for files, not folders)
        - 'e' (entries): array of nested files/folders (only for folders)
        """

        for file_entry in file_list:
            if isinstance(file_entry, AllDebridDirectory):
                sub_prefix = (
                    f"{path_prefix}/{file_entry.n}" if path_prefix else file_entry.n
                )
                self._extract_files_recursive(
                    file_entry.e,
                    item_type,
                    files,
                    infohash,
                    path_prefix=sub_prefix,
                )
                continue

            name = file_entry.n.rsplit("/", 1)[-1]
            current_path = (
                f"{path_prefix}/{file_entry.n}"
                if path_prefix and not file_entry.n.startswith(path_prefix)
                else file_entry.n
            )

            link = file_entry.l
            size = file_entry.s

            if not link:
                continue

            try:
                df = DebridFile.create(
                    path=current_path,
                    filename=name,
                    filesize_bytes=size,
                    filetype=item_type,
                    file_id=None,
                )

                df.download_url = link
                files.append(df)
            except InvalidDebridFileException:
                pass

    def add_torrent(self, infohash: str) -> int:
        """
        Add a magnet by infohash.

        Returns:
            AllDebrid magnet id.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            DebridVpnBlockedError: If VPN/IP is blocked.
            ProviderAuthError: If authentication failed.
            AllDebridError: If the API returns a failing status.
        """

        api = self.api
        if api is None:
            raise AllDebridError("AllDebrid API client has not been initialized")

        magnet_url = f"magnet:?xt=urn:btih:{infohash}"

        response = api.session.post(
            "v4/magnet/upload",
            data={
                "magnets[]": magnet_url,
            },
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise self._availability_error(response)

        try:
            data = (
                AllDebridResponse[AllDebridMagnet]
                .model_validate({"data": response.json()})
                .data
            )
        except ValidationError as e:
            raise AllDebridError(f"Invalid response format from AllDebrid: {e}")

        if isinstance(data, AllDebridErrorResponse):
            raise self._error_from_detail(data.error, response.status_code)

        magnets = data.data.magnets

        if not magnets:
            raise AllDebridError("No magnet ID returned by AllDebrid")

        [magnet_info] = magnets

        magnet_id = magnet_info.id

        if not magnet_id:
            raise AllDebridError("No magnet ID in response")

        return int(magnet_id)

    def select_files(self, torrent_id: int | str, file_ids: list[int]) -> None:
        """
        Select which files to download from the magnet.

        Note: AllDebrid doesn't require explicit file selection.
        Files are automatically available once the magnet is ready.
        """

    def _get_magnet_files(
        self,
        magnet_id: int,
    ) -> list[AllDebridFile | AllDebridDirectory] | None:
        """Get file entries and download links for a magnet from dedicated magnet/files endpoint."""

        try:
            api = self.api
            if api is None:
                raise AllDebridError("AllDebrid API client has not been initialized")

            response = api.session.post(
                "v4/magnet/files",
                data={"id": [magnet_id]},
            )
            self._maybe_backoff(response)

            if not response.ok:
                return None

            data = (
                AllDebridResponse[AllDebridMagnetFilesResponse]
                .model_validate({"data": response.json()})
                .data
            )
            if isinstance(data, AllDebridErrorResponse):
                return None

            for magnet in data.data.magnets:
                if isinstance(magnet, AllDebridMagnetFilesResponse.MagnetErrorInfo):
                    continue

                if magnet.files:
                    return magnet.files

            return None
        except Exception as e:
            logger.debug(f"Error getting magnet files: {e}")
            return None

    @staticmethod
    def _map_status_code(code: int) -> str:
        """Map AllDebrid numeric statusCode to human-readable status string."""
        mapping: dict[int, str] = {
            AllDebridMagnetStatusCode.IN_QUEUE.value: "In Queue",
            AllDebridMagnetStatusCode.DOWNLOADING.value: "Downloading",
            AllDebridMagnetStatusCode.COMPRESSING.value: "Compressing",
            AllDebridMagnetStatusCode.UPLOADING.value: "Uploading",
            AllDebridMagnetStatusCode.READY.value: "Ready",
            AllDebridMagnetStatusCode.UPLOAD_FAILED.value: "Upload Failed",
            AllDebridMagnetStatusCode.ERROR.value: "Error",
            AllDebridMagnetStatusCode.BAD_TORRENT.value: "Bad Torrent",
            AllDebridMagnetStatusCode.NOT_FOUND.value: "Not Found",
            AllDebridMagnetStatusCode.DELETED.value: "Deleted",
            AllDebridMagnetStatusCode.SERVER_MAINTENANCE.value: "Maintenance",
            AllDebridMagnetStatusCode.PAUSED.value: "Paused",
        }
        return mapping.get(code, f"Status_{code}")

    def get_torrent_info(self, torrent_id: int | str) -> TorrentInfo:
        """
        Get information about a specific magnet using its ID.

        Args:
            torrent_id: ID of the magnet to get info for.

        Returns:
            TorrentInfo: Current information about the magnet.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            AllDebridError: If the API returns a failing status.
        """

        api = self.api
        if api is None:
            raise AllDebridError("AllDebrid API client has not been initialized")

        response = api.session.post(
            "v4.1/magnet/status",
            data={
                "id": str(torrent_id),
            },
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise self._error_from_response(response)

        data = (
            AllDebridResponse[AllDebridMagnetStatusResponse]
            .model_validate({"data": response.json()})
            .data
        )

        if isinstance(data, AllDebridErrorResponse):
            raise self._error_from_detail(data.error, response.status_code)

        magnets = data.data.magnets

        if not magnets:
            raise AllDebridError(f"Magnet {torrent_id} not found")

        [magnet_data] = magnets

        if isinstance(magnet_data, AllDebridMagnetStatusResponse.MagnetErrorInfo):
            raise self._error_from_detail(magnet_data.error)

        # Map status string or fallback to numeric statusCode mapping
        status = magnet_data.status or self._map_status_code(magnet_data.status_code)

        # Progress calculation
        if magnet_data.status_code == AllDebridMagnetStatusCode.READY:
            progress = 100.0
        elif (
            magnet_data.status_code == AllDebridMagnetStatusCode.DOWNLOADING
            and magnet_data.size > 0
            and magnet_data.downloaded > 0
        ):
            progress = round((magnet_data.downloaded / magnet_data.size) * 100.0, 2)
        else:
            progress = 0.0

        # Parse timestamps
        upload_date = magnet_data.upload_date
        completion_date = magnet_data.completion_date

        created_at = (
            datetime.fromtimestamp(upload_date, tz=timezone.utc)
            if upload_date
            else None
        )
        completed_at = (
            datetime.fromtimestamp(completion_date, tz=timezone.utc)
            if completion_date
            else None
        )

        return TorrentInfo(
            id=torrent_id,
            name=magnet_data.filename,
            status=status,
            infohash=magnet_data.hash,
            bytes=magnet_data.size,
            created_at=created_at,
            completed_at=completed_at,
            progress=progress,
            files={},  # Files are retrieved separately via magnet/files
            links=[],
        )

    def delete_torrent(self, torrent_id: str | int) -> None:
        """
        Delete a magnet on AllDebrid.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            AllDebridError: If the API returns a failing status.
        """

        api = self.api
        if api is None:
            raise AllDebridError("AllDebrid API client has not been initialized")

        response = api.session.post(
            url="v4/magnet/delete",
            data={
                "id": str(torrent_id),
            },
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise self._error_from_response(response)

    def unrestrict_link(self, link: str) -> UnrestrictedLink | None:
        """
        Unrestrict a link using AllDebrid.

        Args:
            link: The link to unrestrict.

        Returns:
            UnrestrictedLink, or None on error.
        """

        try:
            api = self.api
            if api is None:
                raise AllDebridError("AllDebrid API client has not been initialized")

            response = api.session.get(
                "v4/link/unlock",
                params={
                    "link": link,
                },
            )

            self._maybe_backoff(response)

            if not response.ok:
                return None

            data = (
                AllDebridResponse[AllDebridLinkUnlockResponse]
                .model_validate({"data": response.json()})
                .data
            )

            if isinstance(data, AllDebridErrorResponse):
                return None

            link_data = data.data
            unrestricted_url = link_data.link

            if not unrestricted_url:
                return None

            return UnrestrictedLink(
                download=unrestricted_url,
                filename=link_data.filename,
                filesize=link_data.filesize,
            )

        except Exception:
            return None

    def get_user_info(self) -> UserInfo | None:
        """
        Get normalized user information from AllDebrid.

        Returns:
            UserInfo with normalized fields, or None on error.
        """

        try:
            api = self.api
            if api is None:
                raise AllDebridError("AllDebrid API client has not been initialized")

            response = api.session.get("v4/user")

            self._maybe_backoff(response)

            if not response.ok:
                logger.error(f"Failed to get user info: {self._handle_error(response)}")
                return None

            data = (
                AllDebridResponse[AllDebridUserResponse]
                .model_validate({"data": response.json()})
                .data
            )

            if isinstance(data, AllDebridErrorResponse):
                logger.error(f"Failed to get user info: {data.error.message}")
                return None

            user_data = data.data.user

            if not user_data:
                return None

            # Parse premium expiration
            premium_expires_at = None
            premium_days_left_val = None
            is_premium = user_data.is_premium

            if is_premium:
                premium_until = user_data.premium_until

                if premium_until > 0:
                    premium_expires_at = datetime.fromtimestamp(
                        premium_until, tz=timezone.utc
                    )
                    premium_days_left_val = max(
                        0, (premium_expires_at - datetime.now(tz=timezone.utc)).days
                    )

            return UserInfo(
                service="alldebrid",
                username=user_data.username,
                email=user_data.email,
                user_id=user_data.username,
                premium_status="premium" if is_premium else "free",
                premium_expires_at=premium_expires_at,
                premium_days_left=premium_days_left_val,
                points=user_data.fidelity_points,
            )

        except Exception as e:
            logger.error(f"Error getting AllDebrid user info: {e}")
            return None

    def get_pin(self) -> AllDebridPinGetResponse:
        """Fetch a new PIN code for OAuth device flow."""
        if not self.api:
            raise AllDebridError("AllDebrid API client has not been initialized")
        return self.api.get_pin()

    def check_pin(self, check: str, pin: str) -> AllDebridPinCheckResponse:
        """Check PIN authentication status."""
        if not self.api:
            raise AllDebridError("AllDebrid API client has not been initialized")
        return self.api.check_pin(check=check, pin=pin)
