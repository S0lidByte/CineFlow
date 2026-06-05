from program.services.scrapers.base import ScraperService


def test_sanitize_logged_url_redacts_sensitive_query_params():
    url = (
        "http://prowlarr:9696/download?apikey=supersecret"
        "&token=abc123"
        "&query=The+Show+S01E01"
        "&api_key=secondary_secret"
    )
    sanitized = ScraperService._sanitize_logged_url(url)

    assert "supersecret" not in sanitized
    assert "abc123" not in sanitized
    assert "secondary_secret" not in sanitized
    assert "apikey=%5Bredacted%5D" in sanitized
    assert "token=%5Bredacted%5D" in sanitized
    assert "query=The+Show+S01E01" in sanitized
    assert "api_key=%5Bredacted%5D" in sanitized


def test_sanitize_logged_url_no_query():
    url = "http://example.com/indexer/42"
    assert ScraperService._sanitize_logged_url(url) == url
