import httpx
import asyncio

from program.utils import async_client


def test_sanitize_logged_url_redacts_sensitive_query_params():
    url = "https://example.com/search?apikey=shh&API_KEY=UPPER&token=tok&access_token=at&refresh_token=rt&client_secret=cs&password=pwd&safe=ok"

    sanitized = async_client._sanitize_logged_url(url)

    assert "apikey=shh" not in sanitized
    assert "API_KEY=UPPER" not in sanitized
    assert "token=tok" not in sanitized
    assert "access_token=at" not in sanitized
    assert "refresh_token=rt" not in sanitized
    assert "client_secret=cs" not in sanitized
    assert "password=pwd" not in sanitized
    assert "safe=ok" in sanitized
    assert "apikey=%5Bredacted%5D" in sanitized
    assert "API_KEY=%5Bredacted%5D" in sanitized
    assert "token=%5Bredacted%5D" in sanitized
    assert "access_token=%5Bredacted%5D" in sanitized
    assert "refresh_token=%5Bredacted%5D" in sanitized
    assert "client_secret=%5Bredacted%5D" in sanitized
    assert "password=%5Bredacted%5D" in sanitized
    assert sanitized.count("%5Bredacted%5D") == 7


def test_log_request_redacts_sensitive_query_params(monkeypatch):
    captured = []

    def fake_log(level, message):
        captured.append((level, message))

    monkeypatch.setattr(async_client.logger, "log", fake_log)

    client = async_client.AsyncClient()
    request = httpx.Request(
        "GET",
        "https://example.com/search?apikey=shh&token=tok&safe=ok",
    )

    asyncio.run(client.log_request(request))

    assert len(captured) == 1
    level, message = captured[0]
    assert level == "NETWORK"
    assert "apikey=%5Bredacted%5D" in message
    assert "token=%5Bredacted%5D" in message
    assert "safe=ok" in message
    assert "shh" not in message


def test_log_response_redacts_sensitive_query_params(monkeypatch):
    captured = []

    def fake_log(level, message):
        captured.append((level, message))

    monkeypatch.setattr(async_client.logger, "log", fake_log)

    request = httpx.Request(
        "GET",
        "https://example.com/resolve?refresh_token=abc&safe=ok",
    )
    response = httpx.Response(200, request=request)

    client = async_client.AsyncClient()

    asyncio.run(client.log_response(response))

    assert len(captured) == 1
    level, message = captured[0]
    assert level == "NETWORK"
    assert "refresh_token=%5Bredacted%5D" in message
    assert "abc" not in message
    assert "safe=ok" in message


def test_async_client_connection_limits_are_bounded():
    client = async_client.AsyncClient()
    pool = client._transport._pool
    assert pool._max_connections == 200
    assert pool._max_keepalive_connections == 50
    asyncio.run(client.aclose())
