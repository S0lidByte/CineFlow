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


def classify_http_status(
    status_code: int,
    message: str = "",
    *,
    provider_name: str | None = None,
    retry_after: float | None = None,
    raw_payload: Any = None,
) -> ProviderError:
    """Classify an HTTP status code into the normalized ProviderError taxonomy."""
    msg = message or f"HTTP {status_code} error"
    if status_code in (401, 403):
        return ProviderAuthError(
            msg,
            provider_name=provider_name,
            status_code=status_code,
            raw_error=raw_payload,
        )
    if status_code == 429:
        return ProviderRateLimitError(
            msg,
            provider_name=provider_name,
            retry_after_seconds=retry_after,
            status_code=status_code,
            raw_error=raw_payload,
        )
    if status_code in (402,):
        return ProviderQuotaExceededError(
            msg,
            provider_name=provider_name,
            status_code=status_code,
            raw_error=raw_payload,
        )
    if 500 <= status_code < 600 or status_code in (520, 521, 522, 523, 524):
        return ProviderUnavailableError(
            msg,
            provider_name=provider_name,
            status_code=status_code,
            raw_error=raw_payload,
        )
    return ProviderError(
        msg,
        provider_name=provider_name,
        is_transient=False,
        status_code=status_code,
        raw_error=raw_payload,
    )


def normalize_provider_error(
    *,
    provider_name: str,
    status_code: int | None = None,
    message: str = "",
    raw_payload: Any = None,
    retry_after: float | None = None,
    exception: Exception | None = None,
) -> ProviderError:
    """Normalize any exception or HTTP status code into a standard ProviderError subclass."""
    if exception is not None:
        if isinstance(exception, ProviderError):
            if not exception.provider_name:
                exception.provider_name = provider_name
            return exception

        exc_str = str(exception) or type(exception).__name__
        msg = f"{message}: {exc_str}" if message else exc_str
        exc_type = type(exception).__name__.lower()

        # Extract retry_after attribute if available on exception (e.g. RateLimitError)
        effective_retry_after = retry_after
        if effective_retry_after is None:
            raw_retry: Any = getattr(exception, "retry_after", None)
            if raw_retry is not None:
                try:
                    effective_retry_after = float(raw_retry)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    pass

        # Check for HTTP status in exception attributes if present (e.g. httpx.HTTPStatusError)
        resp = getattr(exception, "response", None)
        if resp is not None and hasattr(resp, "status_code"):
            code = resp.status_code
            if effective_retry_after is None and hasattr(resp, "headers"):
                try:
                    retry_header = resp.headers.get("retry-after")
                    if retry_header:
                        effective_retry_after = float(retry_header)
                except (ValueError, TypeError, Exception):
                    pass

            payload = raw_payload
            if payload is None:
                try:
                    payload = resp.text if hasattr(resp, "text") else None
                except Exception:
                    payload = None

            return classify_http_status(
                code,
                msg,
                provider_name=provider_name,
                retry_after=effective_retry_after,
                raw_payload=payload,
            )

        # Rate limit exceptions by name
        if "ratelimit" in exc_type or "rate_limit" in exc_type:
            return ProviderRateLimitError(
                msg,
                provider_name=provider_name,
                retry_after_seconds=effective_retry_after,
                status_code=429,
                raw_error=exception,
            )

        # Timeout & Network connection exceptions
        if any(
            term in exc_type
            for term in ("timeout", "connect", "network", "socket", "dns", "protocol")
        ):
            return ProviderNetworkError(
                msg,
                provider_name=provider_name,
                raw_error=exception,
            )

        return ProviderError(
            msg,
            provider_name=provider_name,
            is_transient=False,
            raw_error=exception,
        )

    if status_code is not None:
        return classify_http_status(
            status_code,
            message,
            provider_name=provider_name,
            retry_after=retry_after,
            raw_payload=raw_payload,
        )

    return ProviderError(
        message or "Unknown provider error",
        provider_name=provider_name,
        is_transient=False,
        raw_error=raw_payload,
    )
