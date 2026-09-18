"""Tests for title normalization, alias variant generation, and search query sanitization."""

from program.utils.title_normalizer import (
    generate_title_alias_variants,
    normalize_title,
    sanitize_search_query_title,
)


def test_normalize_title_upstream_diacritics():
    """Verify faithful reproduction of upstream Riven-TS normalise.ts character translations."""
    # Accented characters from upstream test suite
    assert normalize_title("café") == "cafe"
    assert normalize_title("naïve") == "naive"
    assert normalize_title("über") == "uber"
    assert normalize_title("Pokémon") == "pokemon"
    assert normalize_title("Amélie") == "amelie"
    assert normalize_title("München") == "munchen"
    assert normalize_title("Łódź") == "lodz"
    assert normalize_title("Crème Brûlée") == "creme brulee"
    assert normalize_title("Kraków") == "krakow"

    # Ligatures and special letters
    assert normalize_title("Faerûn & Dæmon") == "faerun and daemon"
    assert normalize_title("Cœur") == "coeur"
    assert normalize_title("Straße") == "strasse"


def test_normalize_title_punctuation_and_separators():
    """Verify symbol conversions and punctuation removal matching upstream specification."""
    assert normalize_title("Law & Order") == "law and order"
    assert normalize_title("Rock & Roll") == "rock and roll"
    assert normalize_title("Hello_World") == "hello world"
    assert normalize_title("Mr. Robot") == "mr robot"
    assert normalize_title("What's Up?") == "whats up"
    assert normalize_title("Yes! No?") == "yes no"
    assert normalize_title("A: B; C") == "a b c"
    assert normalize_title("What If...?") == "what if"
    assert normalize_title("Grey's Anatomy") == "greys anatomy"
    assert normalize_title("Marvel's Agents of S.H.I.E.L.D.") == "marvels agents of s h i e l d"
    assert normalize_title("Spider-Man: Into the Spider-Verse") == "spiderman into the spiderverse"
    assert normalize_title("Ocean's Eleven!") == "oceans eleven"
    assert normalize_title("Fast_and_Furious.2001") == "fast and furious 2001"


def test_normalize_title_casing_and_whitespace():
    """Verify lower casing and whitespace collapsing."""
    assert normalize_title("  The   Matrix  \t \n ") == "the matrix"
    assert normalize_title("The Matrix", lower=False) == "The Matrix"
    assert normalize_title("") == ""
    assert normalize_title("   ") == ""


def test_generate_title_alias_variants_apostrophe():
    """Verify alias variant generation for titles with apostrophes."""
    variants = generate_title_alias_variants("Grey's Anatomy")
    assert "Greys Anatomy" in variants
    assert "Grey s Anatomy" in variants


def test_generate_title_alias_variants_acronym_and_periods():
    """Verify alias variant generation for acronyms and dotted titles."""
    variants = generate_title_alias_variants("Marvel's Agents of S.H.I.E.L.D.")
    # Should include both dot-spaced and dot-stripped versions
    assert any("SHIELD" in v for v in variants)
    assert any("S H I E L D" in v for v in variants)
    assert any("Marvels Agents of SHIELD" in v for v in variants)

    friends_variants = generate_title_alias_variants("F.R.I.E.N.D.S.")
    assert "FRIENDS" in friends_variants or "FRIENDS." in friends_variants or "F R I E N D S" in friends_variants


def test_generate_title_alias_variants_ampersand():
    """Verify alias variant generation for ampersands."""
    variants = generate_title_alias_variants("Law & Order")
    assert "Law and Order" in variants
    assert "Law Order" in variants


def test_generate_title_alias_variants_hyphen():
    """Verify alias variant generation for hyphens."""
    variants = generate_title_alias_variants("Spider-Man")
    assert "Spider Man" in variants
    assert "Spiderman" in variants


def test_generate_title_alias_variants_trailing_punct():
    """Verify alias variant generation for trailing punctuation."""
    variants = generate_title_alias_variants("What If...?")
    assert "What If" in variants


def test_generate_title_alias_variants_empty():
    """Verify empty or whitespace string handling."""
    assert generate_title_alias_variants("") == []


def test_sanitize_search_query_title():
    """Verify tracker/indexer query sanitization preserves alphanumeric and strips query-breaking chars."""
    assert sanitize_search_query_title("What If...?") == "What If..."
    assert sanitize_search_query_title("Law & Order: Special Victims Unit") == "Law & Order Special Victims Unit"
    assert sanitize_search_query_title("Marvel's Agents of S.H.I.E.L.D.") == "Marvel's Agents of S.H.I.E.L.D."
    assert sanitize_search_query_title("Movie [2024] {1080p} *Remastered*") == "Movie 2024 1080p Remastered"
    assert sanitize_search_query_title('Title/With\\Slashes and "Quotes"') == "Title With Slashes and Quotes"
    assert sanitize_search_query_title("") == ""
