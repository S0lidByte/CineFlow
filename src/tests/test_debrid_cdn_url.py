from program.utils.debrid_cdn_url import DebridCDNUrl


def test_sanitize_logged_url_redacts_sensitive_query_params():
    url = (
        "https://example.com/stream?apikey=shh"
        "&token=tok"
        "&access_token=at"
        "&refresh_token=rt"
        "&client_secret=cs"
        "&password=pwd"
        "&safe=ok"
    )

    sanitized = DebridCDNUrl._sanitize_logged_url(url)

    assert "apikey=shh" not in sanitized
    assert "token=tok" not in sanitized
    assert "access_token=at" not in sanitized
    assert "refresh_token=rt" not in sanitized
    assert "client_secret=cs" not in sanitized
    assert "password=pwd" not in sanitized
    assert "apikey=%5Bredacted%5D" in sanitized
    assert "token=%5Bredacted%5D" in sanitized
    assert "access_token=%5Bredacted%5D" in sanitized
    assert "refresh_token=%5Bredacted%5D" in sanitized
    assert "client_secret=%5Bredacted%5D" in sanitized
    assert "password=%5Bredacted%5D" in sanitized
    assert "safe=ok" in sanitized


def test_sanitize_logged_url_no_query():
    url = "https://example.com/stream/file"
    assert DebridCDNUrl._sanitize_logged_url(url) == url
