from program.services.scrapers.base import ScraperService
from program.utils.url_sanitizer import sanitize_url_for_logs


def test_sanitize_logged_url_redacts_sensitive_query_params():
    url = (
        "http://prowlarr:9696/download?apikey=supersecret"
        "&token=abc123"
        "&access_token=abc456"
        "&refresh_token=abc789"
        "&client_secret=abc000"
        "&password=pwd"
        "&query=The+Show+S01E01"
        "&api_key=secondary_secret"
    )
    sanitized = ScraperService._sanitize_logged_url(url)

    assert "supersecret" not in sanitized
    assert "abc123" not in sanitized
    assert "secondary_secret" not in sanitized
    assert "abc456" not in sanitized
    assert "abc789" not in sanitized
    assert "abc000" not in sanitized
    assert "pwd" not in sanitized
    assert "apikey=%5Bredacted%5D" in sanitized
    assert "token=%5Bredacted%5D" in sanitized
    assert "access_token=%5Bredacted%5D" in sanitized
    assert "refresh_token=%5Bredacted%5D" in sanitized
    assert "client_secret=%5Bredacted%5D" in sanitized
    assert "password=%5Bredacted%5D" in sanitized
    assert "query=The+Show+S01E01" in sanitized
    assert "api_key=%5Bredacted%5D" in sanitized


def test_sanitize_logged_url_no_query():
    url = "http://example.com/indexer/42"
    assert ScraperService._sanitize_logged_url(url) == url


def test_get_infohash_from_url_logs_redacted_url_and_error(monkeypatch):
    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("network unavailable")

    logged_messages = []

    def fake_debug(message, *_args, **_kwargs):
        logged_messages.append(message)

    monkeypatch.setattr("program.services.scrapers.base.logger.debug", fake_debug)

    assert (
        ScraperService.get_infohash_from_url(
            "https://example.com/download?token=abc123&safe=ok",
            session=FailingSession(),
        )
        is None
    )

    assert len(logged_messages) == 1
    message = logged_messages[0]
    assert sanitize_url_for_logs("https://example.com/download?token=abc123&safe=ok") in message
    assert "abc123" not in message
    assert "Failed to get infohash from URL" in message
