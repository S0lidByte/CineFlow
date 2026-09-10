"""Torrent utilities for infohash extraction and manipulation."""

import base64
import binascii
import re

from loguru import logger

# Pattern to match infohashes in magnet links (supports both 40-char hex and 32-char base32)
INFOHASH_PATTERN = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})", re.IGNORECASE)
_STRICT_HEX_INFOHASH = re.compile(r"[0-9a-fA-F]{40}\Z")
_STRICT_BASE32_INFOHASH = re.compile(r"[A-Z2-7]{32}\Z")


def canonical_infohash(value: str | None) -> str | None:
    """Return a strict lowercase BTv1 hexadecimal infohash, or ``None``.

    This is intentionally stricter than :func:`normalize_infohash` for untrusted
    provider input. It accepts exactly 40 ASCII hexadecimal characters or 32
    RFC 4648 Base32 characters representing exactly 20 bytes; it never trims.
    """
    if value is None:
        return None
    if not value.isascii():
        return None
    if _STRICT_HEX_INFOHASH.fullmatch(value):
        return value.lower()
    if not _STRICT_BASE32_INFOHASH.fullmatch(value.upper()):
        return None
    try:
        decoded = base64.b32decode(value.upper(), casefold=False)
    except (ValueError, binascii.Error):
        return None
    if len(decoded) != 20:
        return None
    return decoded.hex()


def normalize_infohash(infohash: str) -> str:
    """
    Normalize an infohash to 40-character hexadecimal format.

    Converts base32-encoded infohashes (32 chars) to base16 (40 chars).

    Args:
        infohash: The infohash to normalize (32 or 40 characters)

    Returns:
        str: The normalized 40-character hexadecimal infohash (lowercase)
    """

    if len(infohash) == 32:
        # Convert base32 to base16
        try:
            infohash = base64.b16encode(base64.b32decode(infohash.upper())).decode(
                "utf-8"
            )
        except Exception as e:
            logger.debug(f"Failed to convert base32 infohash to base16: {e}")
            return infohash.lower()

    return infohash.lower()


def extract_infohash(text: str) -> str | None:
    """
    Extracts infohash from btih: pattern in strings.
    Supports both 40-character hex and 32-character base32 formats.

    Args:
        text: Text that may contain an infohash

    Returns:
        str | None: The normalized 40-character infohash, or None if not found
    """

    if not text:
        return None

    match = INFOHASH_PATTERN.search(text)

    if match:
        infohash = match.group(1)

        return normalize_infohash(infohash)

    return None
