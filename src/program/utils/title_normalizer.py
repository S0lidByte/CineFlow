"""
Title normalization, alias generation, and search query sanitization utilities.

Faithfully reproduces upstream Riven-TS normalization (util-rank-torrent-name)
while providing CineFlow-specific alias synthesis and safe indexer query sanitization.
"""

from __future__ import annotations

import re
import unicodedata

# Upstream translation table from util-rank-torrent-name/lib/shared/normalise.ts
_TRANSLATION_TABLE: dict[str, str | None] = {
    "ā": "a",
    "ă": "a",
    "ą": "a",
    "ć": "c",
    "č": "c",
    "ç": "c",
    "ĉ": "c",
    "ċ": "c",
    "ď": "d",
    "đ": "d",
    "è": "e",
    "é": "e",
    "ê": "e",
    "ë": "e",
    "ē": "e",
    "ĕ": "e",
    "ę": "e",
    "ě": "e",
    "ĝ": "g",
    "ğ": "g",
    "ġ": "g",
    "ģ": "g",
    "ĥ": "h",
    "î": "i",
    "ï": "i",
    "ì": "i",
    "í": "i",
    "ī": "i",
    "ĩ": "i",
    "ĭ": "i",
    "ı": "i",
    "ĵ": "j",
    "ķ": "k",
    "ĺ": "l",
    "ļ": "l",
    "ł": "l",
    "ń": "n",
    "ň": "n",
    "ñ": "n",
    "ņ": "n",
    "ŉ": "n",
    "ó": "o",
    "ô": "o",
    "õ": "o",
    "ö": "o",
    "ø": "o",
    "ō": "o",
    "ő": "o",
    "œ": "oe",
    "ŕ": "r",
    "ř": "r",
    "ŗ": "r",
    "š": "s",
    "ş": "s",
    "ś": "s",
    "ș": "s",
    "ß": "ss",
    "ť": "t",
    "ţ": "t",
    "ū": "u",
    "ŭ": "u",
    "ũ": "u",
    "û": "u",
    "ü": "u",
    "ù": "u",
    "ú": "u",
    "ų": "u",
    "ű": "u",
    "ŵ": "w",
    "ý": "y",
    "ÿ": "y",
    "ŷ": "y",
    "ž": "z",
    "ż": "z",
    "ź": "z",
    "æ": "ae",
    "ǎ": "a",
    "ǧ": "g",
    "ə": "e",
    "ƒ": "f",
    "ǐ": "i",
    "ǒ": "o",
    "ǔ": "u",
    "ǚ": "u",
    "ǜ": "u",
    "ǹ": "n",
    "ǻ": "a",
    "ǽ": "ae",
    "ǿ": "o",
    "!": None,
    "?": None,
    ",": None,
    ".": " ",
    ":": None,
    ";": None,
    "'": None,
    "&": "and",
    "_": " ",
}

# Regex for illegal or syntax-breaking characters in tracker / indexer search queries
_QUERY_ILLEGAL_CHARS_PATTERN = re.compile(r'[\?!:\*/\\"~\^\[\]\{\}]')
_WHITESPACE_COLLAPSE_PATTERN = re.compile(r"\s+")


def normalize_title(raw_title: str, lower: bool = True) -> str:
    """
    Normalizes a title string according to upstream Riven-TS normalization rules.

    Applies NFKC normalization, Latin diacritic and ligature transliteration,
    separator substitution, punctuation removal, non-alphanumeric character stripping,
    and whitespace collapsing.
    """
    if not raw_title:
        return ""

    text = raw_title.lower() if lower else raw_title

    # Normalise unicode characters (NFKC)
    text = unicodedata.normalize("NFKC", text)

    # Apply specific translations from upstream translation table
    translated_chars: list[str] = []
    for ch in text:
        if ch in _TRANSLATION_TABLE:
            replacement = _TRANSLATION_TABLE[ch]
            if replacement is not None:
                translated_chars.append(replacement)
            # None indicates removal of the character
        else:
            translated_chars.append(ch)

    translated = "".join(translated_chars)

    # Keep only alphanumeric and whitespace characters (mirroring [^\\p{L}\\p{N}\\s])
    filtered_chars = [ch for ch in translated if ch.isalnum() or ch.isspace()]
    filtered = "".join(filtered_chars)

    # Collapse whitespace and strip
    return _WHITESPACE_COLLAPSE_PATTERN.sub(" ", filtered).strip()


def generate_title_alias_variants(title: str) -> list[str]:
    """
    Generates alternative title alias variants for RTN ranking.

    Handles common title variations and combinations such as:
    - Acronym dots (e.g. 'S.H.I.E.L.D.' -> 'S H I E L D', 'SHIELD')
    - Apostrophes (e.g. "Grey's Anatomy" -> 'Greys Anatomy', 'Grey s Anatomy')
    - Ampersands (e.g. 'Law & Order' -> 'Law and Order', 'Law Order')
    - Hyphens (e.g. 'Spider-Man' -> 'Spider Man', 'Spiderman')
    - Colons & trailing punctuation (e.g. 'What If...?' -> 'What If')
    - Diacritic transliteration (e.g. 'Pokémon' -> 'Pokemon')

    Returns a deduplicated list of non-empty variant strings.
    """
    if not title:
        return []

    variants: list[str] = []

    def _add_variant(v: str) -> None:
        cleaned = _WHITESPACE_COLLAPSE_PATTERN.sub(" ", v).strip()
        if cleaned and cleaned not in variants and cleaned != title:
            variants.append(cleaned)

    # 1. Direct upstream normalized variant (if different)
    _add_variant(normalize_title(title, lower=False))

    # Base pool of candidates to expand combinatorially
    pool: list[str] = [title]

    # Expand apostrophes
    next_pool: list[str] = []
    for cand in pool:
        next_pool.append(cand)
        if "'" in cand or "’" in cand or "`" in cand:
            stripped = cand.replace("'", "").replace("’", "").replace("`", "")
            spaced = cand.replace("'", " ").replace("’", " ").replace("`", " ")
            if stripped not in next_pool:
                next_pool.append(stripped)
            if spaced not in next_pool:
                next_pool.append(spaced)
    pool = next_pool

    # Expand ampersands
    next_pool = []
    for cand in pool:
        next_pool.append(cand)
        if "&" in cand:
            and_v = cand.replace("&", "and")
            space_v = cand.replace("&", " ")
            if and_v not in next_pool:
                next_pool.append(and_v)
            if space_v not in next_pool:
                next_pool.append(space_v)
    pool = next_pool

    # Expand hyphens
    next_pool = []
    for cand in pool:
        next_pool.append(cand)
        if "-" in cand:
            space_v = cand.replace("-", " ")
            no_dash = cand.replace("-", "")
            if space_v not in next_pool:
                next_pool.append(space_v)
            if no_dash not in next_pool:
                next_pool.append(no_dash)
            if no_dash.capitalize() not in next_pool:
                next_pool.append(no_dash.capitalize())
    pool = next_pool

    # Expand periods / acronyms
    next_pool = []
    for cand in pool:
        next_pool.append(cand)
        if "." in cand:
            space_v = cand.replace(".", " ")
            no_dot = cand.replace(".", "")
            if space_v not in next_pool:
                next_pool.append(space_v)
            if no_dot not in next_pool:
                next_pool.append(no_dot)
    pool = next_pool

    # Expand colons and punctuation
    next_pool = []
    for cand in pool:
        next_pool.append(cand)
        if ":" in cand:
            space_v = cand.replace(":", " ")
            no_colon = cand.replace(":", "")
            if space_v not in next_pool:
                next_pool.append(space_v)
            if no_colon not in next_pool:
                next_pool.append(no_colon)
        stripped_punct = cand.rstrip("!?. ,;:-")
        if stripped_punct and stripped_punct not in next_pool:
            next_pool.append(stripped_punct)
    pool = next_pool

    # Add all generated variants and their normalized forms
    for cand in pool:
        _add_variant(cand)
        norm = normalize_title(cand, lower=False)
        if norm:
            _add_variant(norm)

    return variants


def sanitize_search_query_title(raw_title: str) -> str:
    """
    Sanitizes a title string for use in tracker / indexer search query parameters.

    Strips syntax-breaking characters (? ! : * / \\ " ~ ^ [ ] { }) without
    destroying the readability of the search query.
    """
    if not raw_title:
        return ""

    # Replace illegal query characters with space
    cleaned = _QUERY_ILLEGAL_CHARS_PATTERN.sub(" ", raw_title)

    # Collapse multiple whitespace characters and trim
    return _WHITESPACE_COLLAPSE_PATTERN.sub(" ", cleaned).strip()
