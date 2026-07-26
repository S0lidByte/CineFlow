"""Utilities for subtitle handling, including OpenSubtitles hash calculation"""

import re
import struct
from typing import BinaryIO

# Hash / tag matches are trusted; all weaker match types need a title check.
STRONG_SUBTITLE_MATCH_TYPES = frozenset({"moviehash", "tag"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "to",
        "in",
        "on",
        "at",
        "for",
        "vs",
        "versus",
    }
)
_SEASON_EPISODE_TOKEN = re.compile(
    r"^(?:s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3}|season|episode|s\d{1,2}|e\d{1,3})$"
)


def _significant_title_tokens(text: str) -> list[str]:
    """Tokenize a title/filename into significant alphanumeric words."""
    tokens = _TOKEN_RE.findall((text or "").lower())
    return [
        token
        for token in tokens
        if len(token) > 1
        and token not in _STOP_WORDS
        and not token.isdigit()
        and not _SEASON_EPISODE_TOKEN.match(token)
    ]


def subtitle_title_matches(expected_title: str, *candidates: str | None) -> bool:
    """
    Return True when candidate movie_name/filename text matches expected_title.

    Used to reject fulltext (and other weak) OpenSubtitles hits that share
    season/episode numbers but belong to a different show (e.g. Game of Thrones
    for Stargate Atlantis).
    """
    expected_tokens = _significant_title_tokens(expected_title)
    if not expected_tokens:
        return True

    blob = " ".join(candidate for candidate in candidates if candidate)
    if not blob.strip():
        # No title metadata to compare — do not accept weak matches blindly.
        return False

    found = set(_significant_title_tokens(blob))
    return all(token in found for token in expected_tokens)


def subtitle_result_title_ok(
    expected_title: str,
    *,
    matched_by: str | None,
    movie_name: str | None = None,
    filename: str | None = None,
) -> bool:
    """
    Accept strong hash/tag matches unconditionally; otherwise require title match.
    """
    if (matched_by or "").lower() in STRONG_SUBTITLE_MATCH_TYPES:
        return True
    return subtitle_title_matches(expected_title, movie_name, filename)


def calculate_opensubtitles_hash(file_handle: BinaryIO, file_size: int) -> str:
    """
    Calculate OpenSubtitles hash for a video file.

    The OpenSubtitles hash is calculated by:
    1. Taking the file size
    2. Adding the first 64KB of the file (as 64-bit integers)
    3. Adding the last 64KB of the file (as 64-bit integers)
    4. All additions are done modulo 2^64

    This algorithm is fast because it doesn't read the entire file, making it
    perfect for large video files.

    Parameters:
        file_handle (BinaryIO): File handle opened in binary read mode, must support seek.
        file_size (int): Size of the file in bytes.

    Returns:
        str: 16-character hexadecimal hash string.

    Raises:
        ValueError: If file is too small (< 128KB).
        IOError: If file cannot be read.

    References:
        https://trac.opensubtitles.org/opensubtitles/wiki/HashSourceCodes
    """
    CHUNK_SIZE = 65536  # 64KB in bytes

    if file_size < CHUNK_SIZE * 2:
        raise ValueError(
            f"File is too small ({file_size} bytes). Minimum size is {CHUNK_SIZE * 2} bytes (128KB)."
        )

    # Start with file size as the hash
    hash_value = file_size

    # Read first 64KB
    file_handle.seek(0)
    first_chunk = file_handle.read(CHUNK_SIZE)

    # Read last 64KB
    file_handle.seek(max(0, file_size - CHUNK_SIZE))
    last_chunk = file_handle.read(CHUNK_SIZE)

    # Process chunks as 64-bit little-endian unsigned integers
    # Each chunk is 64KB = 8192 * 8 bytes
    for chunk in (first_chunk, last_chunk):
        # Unpack chunk into 64-bit integers (8 bytes each)
        # '<Q' means little-endian unsigned long long (8 bytes)
        for i in range(0, len(chunk), 8):
            if i + 8 <= len(chunk):
                # Unpack 8 bytes as unsigned 64-bit integer
                value = struct.unpack("<Q", chunk[i : i + 8])[0]
                hash_value = (hash_value + value) & 0xFFFFFFFFFFFFFFFF  # Keep it 64-bit

    # Return as 16-character hexadecimal string (64 bits = 16 hex chars)
    return f"{hash_value:016x}"


def generate_subtitle_path(video_path: str, language: str) -> str:
    """
    Generate a VFS path for a subtitle file based on the video path and language.

    Parameters:
        video_path (str): Virtual VFS path of the video file (e.g., '/Movies/Movie.2024/Movie.2024.mkv').
        language (str): ISO 639-3 language code (e.g., 'eng').

    Returns:
        str: Virtual VFS path for the subtitle (e.g., '/Movies/Movie.2024/Movie.2024.eng.srt').

    Example:
        >>> generate_subtitle_path('/Movies/Movie.2024/Movie.2024.mkv', 'eng')
        '/Movies/Movie.2024/Movie.2024.eng.srt'
    """
    import os

    # Split the video path into directory, filename, and extension
    directory = os.path.dirname(video_path)
    filename = os.path.basename(video_path)
    name_without_ext = os.path.splitext(filename)[0]

    # Generate subtitle filename: <video_name>.<language>.srt
    subtitle_filename = f"{name_without_ext}.{language}.srt"

    # Combine directory and subtitle filename
    subtitle_path = os.path.join(directory, subtitle_filename)

    return subtitle_path
