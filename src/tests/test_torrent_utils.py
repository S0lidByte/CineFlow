"""Regression tests for strict untrusted-provider infohash canonicalization."""

import base64

import pytest

from program.utils.torrent import (
    canonical_infohash,
    extract_infohash,
    normalize_infohash,
)

HEX_HASH = "00112233445566778899aabbccddeeff00112233"
BASE32_HASH = base64.b32encode(bytes.fromhex(HEX_HASH)).decode("ascii")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (HEX_HASH.upper(), HEX_HASH),
        (BASE32_HASH.lower(), HEX_HASH),
    ],
)
def test_canonical_infohash_accepts_exact_btv1_encodings(value, expected):
    assert canonical_infohash(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " " + HEX_HASH,
        HEX_HASH + " ",
        "g" * 40,
        "a" * 39,
        "a" * 41,
        "0" * 32,
        "A" * 31,
        "A" * 33,
    ],
)
def test_canonical_infohash_rejects_malformed_or_whitespace_values(value):
    assert canonical_infohash(value) is None


def test_legacy_normalization_and_extraction_remain_permissive():
    assert normalize_infohash("invalid-base32-value-which-is-32xx") == (
        "invalid-base32-value-which-is-32xx"
    )
    assert extract_infohash(f"magnet:?xt=urn:btih:{HEX_HASH.upper()}") == HEX_HASH


@pytest.mark.parametrize(
    "value",
    [
        # Long-S (ſ) uppercases to S in Python Unicode, which is valid Base32.
        "ſ" * 32,
        # Kelvin sign (K) uppercases to K, valid Base32.
        "\u212A" * 32,
        # Mixed ASCII + non-ASCII that could fold into valid Base32.
        "A" * 31 + "ſ",
        # Non-ASCII hex-length string that could fold into valid hex.
        "\u0430" * 40,  # Cyrillic 'а' looks like Latin 'a'
    ],
)
def test_canonical_infohash_rejects_unicode_lookalikes(value):
    """Non-ASCII input must be rejected before any uppercasing or regex matching."""
    assert canonical_infohash(value) is None
