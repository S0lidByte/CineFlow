"""D95 inspection of actual provider configuration; no live provider traffic.

These tests establish the baseline, NOT bounded-refresh certification.
Only the HTTP transport and clock are replaced for attempt observation.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import requests

import program.utils.request as request_module
from program.services.downloaders.realdebrid import RealDebridAPI


@pytest.fixture
def api():
    client = RealDebridAPI(api_key="inspection-only-not-a-credential")
    yield client
    client.session.close()


def test_actual_httpx_configuration_has_no_requests_adapter(api):
    session = api.session
    assert isinstance(session._client, httpx.Client)
    assert not isinstance(session, requests.Session)
    assert not hasattr(session, "get_adapter")
    assert session.retries == 2
    assert session.backoff_factor == 0.5
    # Inspect the real target transport before substituting MockTransport.
    transport = session._client._transport_for_url(httpx.URL(api.BASE_URL))
    assert isinstance(transport, httpx.HTTPTransport)
    assert transport._pool._retries == 0
    limiter = session.limiters["api.real-debrid.com"]
    assert limiter.rate == pytest.approx(250 / 60)
    assert limiter.capacity == 250


@pytest.mark.parametrize("failure", ["connect", "read", "status", "rate_limit"])
def test_post_has_three_outer_attempts_and_one_token_acquisition(
    api, monkeypatch, failure
):
    attempts = []
    sleeps = []
    limiter = api.session.limiters["api.real-debrid.com"]
    acquire = Mock(wraps=limiter.wait)
    monkeypatch.setattr(limiter, "wait", acquire)
    monkeypatch.setattr(request_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(request_module.random, "random", lambda: 0.0)

    def handler(request):
        attempts.append(request)
        if failure == "connect":
            raise httpx.ConnectTimeout("inspection", request=request)
        if failure == "read":
            raise httpx.ReadTimeout("inspection", request=request)
        return httpx.Response(429 if failure == "rate_limit" else 503, json={})

    # Retain production SmartSession retry and timeout mapping logic.
    api.session._client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport_client:
        monkeypatch.setattr(api.session, "_client", transport_client)
        if failure in ("connect", "read"):
            with pytest.raises(requests.exceptions.Timeout):
                api.session.post(
                    "/unrestrict/link", data={"link": "fixture"}, timeout=10
                )
        else:
            response = api.session.post(
                "/unrestrict/link", data={"link": "fixture"}, timeout=10
            )
            assert response.status_code == (429 if failure == "rate_limit" else 503)
    assert len(attempts) == 3  # initial attempt + two retries, including POST
    assert all(request.method == "POST" for request in attempts)
    assert all(request.url.path == "/rest/1.0/unrestrict/link" for request in attempts)
    assert all(
        request.extensions["timeout"]
        == {
            "connect": 10,
            "read": 10,
            "write": 10,
            "pool": 10,
        }
        for request in attempts
    )
    assert sleeps == [0.25, 0.5]
    acquire.assert_called_once_with()


def test_retry_after_can_exceed_existing_twenty_second_refresh_window(api, monkeypatch):
    sleeps = []
    monkeypatch.setattr(request_module.time, "sleep", sleeps.append)
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, json={})

    api.session._client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport_client:
        monkeypatch.setattr(api.session, "_client", transport_client)
        response = api.session.post("/unrestrict/link", timeout=10)
    assert response.status_code == 429
    assert len(attempts) == 3
    assert sleeps == [30.0, 30.0]
    assert sum(sleeps) > 20  # disproves the old bound without real sleeping


def test_actual_limiter_immediate_and_delayed_acquisition(api, monkeypatch):
    clock = SimpleNamespace(now=100.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        # Model scheduler progress even for sub-ULP refill deficits.
        clock.now += max(seconds, 1e-9)

    monkeypatch.setattr(
        request_module,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now,
            sleep=sleep,
        ),
    )
    limiter = api.session.limiters["api.real-debrid.com"]
    limiter.last_refill = clock.now
    limiter.tokens = 1
    limiter.wait()
    assert clock.sleeps == []
    limiter.wait()
    assert clock.sleeps[0] == pytest.approx(60 / 250)
    assert sum(clock.sleeps) == pytest.approx(60 / 250)
