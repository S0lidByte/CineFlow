"""Normalized provider error taxonomy and contract protocols for CineFlow."""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """Base exception for all external provider, scraper, and downloader interactions."""

    def __init__(
        self,
        message: str,
        *,
        provider_name: str | None = None,
        is_transient: bool = False,
        status_code: int | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider_name = provider_name
        self.is_transient = is_transient
        self.status_code = status_code
        self.raw_error = raw_error

    def __str__(self) -> str:
        provider_prefix = f"[{self.provider_name}] " if self.provider_name else ""
        return f"{provider_prefix}{self.message}"


class ProviderAuthError(ProviderError):
    """Raised when provider authentication fails (e.g. invalid API key, expired token, 401/403)."""

    def __init__(
        self,
        message: str = "Provider authentication failed",
        *,
        provider_name: str | None = None,
        status_code: int | None = 401,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider_name=provider_name,
            is_transient=False,
            status_code=status_code,
            raw_error=raw_error,
        )


class ProviderRateLimitError(ProviderError):
    """Raised when provider rate limits are exceeded (429 or provider throttle response)."""

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        *,
        provider_name: str | None = None,
        retry_after_seconds: float | None = None,
        status_code: int | None = 429,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider_name=provider_name,
            is_transient=True,
            status_code=status_code,
            raw_error=raw_error,
        )
        self.retry_after_seconds = retry_after_seconds


class ProviderNetworkError(ProviderError):
    """Raised when network connectivity, DNS, or socket transport timeouts occur."""

    def __init__(
        self,
        message: str = "Provider network connection failed",
        *,
        provider_name: str | None = None,
        status_code: int | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider_name=provider_name,
            is_transient=True,
            status_code=status_code,
            raw_error=raw_error,
        )


class ProviderUnavailableError(ProviderError):
    """Raised when the provider endpoint is temporarily down, maintenance (502, 503, 504)."""

    def __init__(
        self,
        message: str = "Provider is temporarily unavailable",
        *,
        provider_name: str | None = None,
        status_code: int | None = 503,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider_name=provider_name,
            is_transient=True,
            status_code=status_code,
            raw_error=raw_error,
        )


class ProviderQuotaExceededError(ProviderError):
    """Raised when provider account quota/limits are reached (e.g., Fair Usage limits, Debrid point depletion)."""

    def __init__(
        self,
        message: str = "Provider quota or account limits exceeded",
        *,
        provider_name: str | None = None,
        status_code: int | None = None,
        raw_error: Any = None,
    ) -> None:
        super().__init__(
            message,
            provider_name=provider_name,
            is_transient=False,
            status_code=status_code,
            raw_error=raw_error,
        )
