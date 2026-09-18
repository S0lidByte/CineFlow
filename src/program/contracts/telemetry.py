"""Correlation ID, context management, and secret redaction contracts for CineFlow."""

from __future__ import annotations

import contextvars
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Final, cast

# Header names used across FastAPI and HTTP proxies
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-ID"
REQUEST_ID_HEADER: Final[str] = "X-Request-ID"

# ContextVar storing active correlation ID for the current execution context (asyncio/threads)
_CORRELATION_ID_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

# Common sensitive parameter / key patterns across logs, settings and query payloads
SENSITIVE_KEY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)(api[_-]?key|apikey)"),
    re.compile(r"(?i)(token|auth[_-]?token|access[_-]?token|refresh[_-]?token)"),
    re.compile(r"(?i)(secret|auth[_-]?secret|client[_-]?secret|webhook[_-]?secret)"),
    re.compile(r"(?i)(password|passwd|pwd)"),
    re.compile(r"(?i)(session[_-]?token|cookie)"),
    re.compile(r"(?i)(private[_-]?key)"),
)

# Regex patterns for matching in-flight token strings in text / URLs
SENSITIVE_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Bearer tokens in headers/text
    re.compile(r"(?i)\b(bearer\s+)([a-zA-Z0-9_\-\.]{16,})\b"),
    # Standard query params or key=val text or key: val text
    re.compile(
        r"(?i)([?&]?\b(?:api[_-]?key|apikey|token|auth[_-]?token|secret|password)\s*[:= ]\s*)([a-zA-Z0-9_\-\.]{12,})"
    ),
    # Generic API key patterns in auth headers
    re.compile(r"(?i)(x-api-key\s*:\s*)([a-zA-Z0-9_\-\.]{12,})"),
    # Standard prefixed tokens (e.g. secret_*, sk_live_*, ghp_*, token_*)
    re.compile(
        r"(?i)\b((?:secret|sk_live|sk_test|ghp|gho|token)_[a-zA-Z0-9_\-]{8,})\b"
    ),
)

REDACTED_SUBSTITUTE: Final[str] = "[REDACTED]"


def generate_correlation_id() -> str:
    """Generate a clean UUIDv4 correlation ID."""
    return str(uuid.uuid4())


def get_correlation_id() -> str:
    """Retrieve the current correlation ID or initialize a new one if unset."""
    cid = _CORRELATION_ID_CTX.get()
    if not cid:
        cid = generate_correlation_id()
        _CORRELATION_ID_CTX.set(cid)
    return cid


def set_correlation_id(correlation_id: str | None) -> contextvars.Token[str | None]:
    """Set the correlation ID for the current async task context."""
    if not correlation_id:
        correlation_id = generate_correlation_id()
    return _CORRELATION_ID_CTX.set(correlation_id)


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    """Reset the correlation ID back to its previous state using the token."""
    _CORRELATION_ID_CTX.reset(token)


def is_sensitive_key(key: str) -> bool:
    """Check if a dictionary key or field name matches known sensitive patterns."""
    return any(pattern.search(key) for pattern in SENSITIVE_KEY_PATTERNS)


def redact_text(text: str) -> str:
    """Redact sensitive token and key patterns from arbitrary string text/URLs."""
    if not text:
        return text

    redacted = text
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if pattern.groups == 1:
            redacted = pattern.sub(REDACTED_SUBSTITUTE, redacted)
        elif pattern.groups == 2:
            redacted = pattern.sub(rf"\1{REDACTED_SUBSTITUTE}", redacted)
    return redacted


def redact_sensitive_data(data: Any, max_depth: int = 5) -> Any:
    """Recursively redact sensitive values without leaking at depth or cycle boundaries.

    Container traversal is bounded to prevent pathological payloads from exhausting
    resources. A container beyond the configured depth, or one that participates in
    an active reference cycle, is replaced wholesale rather than returned unredacted.
    """

    def _redact(value: Any, remaining_depth: int, active_containers: set[int]) -> Any:
        if isinstance(value, str):
            return redact_text(value)

        if not isinstance(value, (Mapping, list, tuple, set)):
            return value

        value_id = id(cast(object, value))
        if remaining_depth <= 0 or value_id in active_containers:
            return REDACTED_SUBSTITUTE

        next_active = active_containers | {value_id}
        if isinstance(value, Mapping):
            mapping_data = cast(Mapping[Any, Any], value)
            redacted_dict: dict[str, Any] = {}
            for raw_k, raw_v in mapping_data.items():
                str_key = str(raw_k)
                redacted_dict[str_key] = (
                    REDACTED_SUBSTITUTE
                    if is_sensitive_key(str_key)
                    else _redact(raw_v, remaining_depth - 1, next_active)
                )
            return redacted_dict

        seq_data = cast(Iterable[Any], value)
        redacted_items = [
            _redact(item, remaining_depth - 1, next_active) for item in seq_data
        ]
        if isinstance(value, tuple):
            return tuple(redacted_items)
        if isinstance(value, set):
            return set(redacted_items)
        return redacted_items

    return _redact(data, max_depth, set())
