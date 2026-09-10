"""CineFlow contracts, protocols, and standard error taxonomy."""

from program.contracts.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNetworkError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    classify_http_status,
    normalize_provider_error,
)

__all__ = [
    "ProviderAuthError",
    "ProviderError",
    "ProviderNetworkError",
    "ProviderQuotaExceededError",
    "ProviderRateLimitError",
    "ProviderUnavailableError",
    "classify_http_status",
    "normalize_provider_error",
]
