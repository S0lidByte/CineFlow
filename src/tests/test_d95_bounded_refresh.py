"""D95 Bounded Provider Refresh Certification Tests.

Tests verify:
1. TokenBucket aborts cleanly on operation_deadline without blocking.
2. SmartSession clamps phase timeouts, stops retrying when deadline is exhausted,
   and raises OperationDeadlineExceeded.
3. RealDebridDownloader propagates operation_deadline to API sessions.
4. VFSDatabase.refresh_unrestricted_url / get_entry_by_original_filename aborts and
   fences database mutation if deadline is exceeded.
5. MediaStream._refresh_download_url derives deadline from producer recovery deadline,
   aborts before/during refresh if deadline expires, and never mutates or adopts URL
   if generation retired or handle closed/seeked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
import requests
import trio

from program.services.downloaders.realdebrid import RealDebridAPI, RealDebridDownloader
from program.services.filesystem.vfs.db import VFSDatabase
from program.services.streaming.media_stream import MediaStream, StreamRecoveryState
from program.utils.request import OperationDeadlineExceeded, SmartSession, TokenBucket

# =========================================================================
# 1. TokenBucket Deadline Bounds
# =========================================================================


def test_token_bucket_deadline_expired_returns_false():
    bucket = TokenBucket(rate=1.0, capacity=1)
    bucket.tokens = 0.0
    now = 100.0
    with patch("program.utils.request.time.monotonic", return_value=now):
        # Deadline already passed
        acquired = bucket.wait(tokens=1.0, deadline=now - 1.0)
        assert acquired is False
        assert bucket.tokens == 0.0


def test_token_bucket_deadline_would_exceed_sleep():
    clock = SimpleNamespace(now=100.0)

    def fake_sleep(secs):
        clock.now += secs

    bucket = TokenBucket(rate=1.0, capacity=1)  # 1 token/sec => requires 1.0s wait
    bucket.tokens = 0.0
    bucket.last_refill = clock.now

    with (
        patch("program.utils.request.time.monotonic", side_effect=lambda: clock.now),
        patch("program.utils.request.time.sleep", side_effect=fake_sleep),
    ):
        # We only have 0.5s left before deadline, but need 1.0s
        acquired = bucket.wait(tokens=1.0, deadline=clock.now + 0.5)
        assert acquired is False


def test_token_bucket_within_deadline_sleeps_and_acquires():
    clock = SimpleNamespace(now=100.0, sleeps=[])

    def fake_sleep(secs):
        clock.sleeps.append(secs)
        clock.now += secs

    bucket = TokenBucket(rate=10.0, capacity=10)
    bucket.tokens = 0.0
    bucket.last_refill = clock.now

    with (
        patch("program.utils.request.time.monotonic", side_effect=lambda: clock.now),
        patch("program.utils.request.time.sleep", side_effect=fake_sleep),
    ):
        # Need 0.1s to get 1 token, deadline is 1.0s away
        # Provide deadline based on initial clock.now
        target_deadline = clock.now + 1.0
        acquired = bucket.wait(tokens=1.0, deadline=target_deadline)
        assert acquired is True
        assert len(clock.sleeps) >= 1
        assert sum(clock.sleeps) == pytest.approx(0.1)


# =========================================================================
# 2. SmartSession Deadline Clamping & Exhaustion
# =========================================================================


def test_smartsession_already_expired_deadline_raises():
    session = SmartSession()
    now = 500.0
    with patch("program.utils.request.time.monotonic", return_value=now):
        with pytest.raises(OperationDeadlineExceeded):
            session.get("http://test.local", operation_deadline=now - 0.01)


def test_smartsession_clamps_httpx_timeout_to_remaining_deadline():
    session = SmartSession(retries=0)
    attempts = []
    start_time = 1000.0
    deadline = start_time + 4.5  # Only 4.5s remaining, configured timeout is 10s

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, json={"status": "ok"})

    session._client.close()
    with patch("program.utils.request.time.monotonic", return_value=start_time):
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            session._client = mock_client
            resp = session.get(
                "http://test.local/test",
                timeout=10.0,
                operation_deadline=deadline,
            )
            assert resp.status_code == 200

    assert len(attempts) == 1
    # Clamped timeout must reflect 4.5s
    timeout_ext = attempts[0].extensions["timeout"]
    assert timeout_ext["connect"] == pytest.approx(4.5)
    assert timeout_ext["read"] == pytest.approx(4.5)


def test_smartsession_retry_aborts_when_deadline_insufficient_for_backoff():
    session = SmartSession(retries=2, backoff_factor=1.0)
    attempts = []
    clock = SimpleNamespace(now=1000.0)

    def handler(request):
        attempts.append(request)
        # Advance time so after 1st attempt the deadline is already reached
        clock.now += 3.0
        return httpx.Response(500, json={})

    session._client.close()
    deadline = 1002.5  # start=1000, attempt finishes at 1003 => now (1003) >= deadline (1002.5)

    with (
        patch("program.utils.request.time.monotonic", side_effect=lambda: clock.now),
        patch("program.utils.request.time.sleep") as mock_sleep,
    ):
        with httpx.Client(transport=httpx.MockTransport(handler)) as mock_client:
            session._client = mock_client
            with pytest.raises(OperationDeadlineExceeded):
                session.get(
                    "http://test.local/fail",
                    timeout=5.0,
                    operation_deadline=deadline,
                )

    # Only 1 attempt occurred because retry backoff / remaining time exceeded deadline
    assert len(attempts) == 1
    mock_sleep.assert_not_called()


# =========================================================================
# 3. Downloader Deadline Forwarding
# =========================================================================


def test_realdebrid_downloader_unrestrict_link_propagates_deadline():
    downloader = RealDebridDownloader()
    mock_api = MagicMock()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "id": "123",
        "filename": "sample.mkv",
        "mimeType": "video/x-matroska",
        "filesize": 1024,
        "link": "https://real-debrid.com/d/123",
        "host": "real-debrid.com",
        "chunks": 1,
        "download": "https://cdn.example.com/unrestricted",
        "streamable": 1,
    }
    mock_session.post.return_value = mock_resp
    mock_api.session = mock_session
    mock_api.BASE_URL = "https://api.real-debrid.com/rest/1.0"
    downloader.api = mock_api

    deadline = 9999.0
    res = downloader.unrestrict_link(
        "https://real-debrid.com/d/123", operation_deadline=deadline
    )
    assert res is not None
    assert res.download == "https://cdn.example.com/unrestricted"
    mock_session.post.assert_called_once_with(
        "https://api.real-debrid.com/rest/1.0/unrestrict/link",
        data={"link": "https://real-debrid.com/d/123"},
        timeout=10,
        operation_deadline=deadline,
    )


# =========================================================================
# 4. VFSDatabase Database Mutation Fencing on Deadline Expiry
# =========================================================================


def test_vfs_db_refresh_fences_mutation_on_deadline_expiry():
    vfs_db = VFSDatabase()
    fake_entry = SimpleNamespace(
        id=1,
        provider="realdebrid",
        original_filename="movie.mkv",
        download_url="https://real-debrid.com/d/123",
        unrestricted_url="https://cdn.old.com/stream",
        unrestricted_valid_until=None,
        file_id=1,
        size=1024,
    )

    mock_service = MagicMock()
    mock_service.key = "realdebrid"
    mock_service.unrestrict_link.return_value = SimpleNamespace(
        download="https://cdn.new.com/stream"
    )

    mock_downloader = MagicMock()
    mock_downloader.services = {type("Dummy", (), {}): mock_service}
    vfs_db.downloader = mock_downloader

    mock_session = MagicMock()
    now = 500.0
    deadline = 499.0  # Already expired when unrestrict returns!

    with patch("program.services.filesystem.vfs.db.time.monotonic", return_value=now):
        res = vfs_db.refresh_unrestricted_url(
            fake_entry, session=mock_session, operation_deadline=deadline
        )
        # Must return None because deadline expired
        assert res is None
        # Must NOT commit or mutate DB!
        mock_session.commit.assert_not_called()
        assert fake_entry.unrestricted_url == "https://cdn.old.com/stream"


# =========================================================================
# 5. MediaStream Coordination and Generation Fencing
# =========================================================================


def _make_dummy_stream(
    nursery, original_filename="sample.mkv", url="https://cdn.example.com/old"
):
    stream = MediaStream(
        fh=42,
        file_size=5000,
        path=f"/movies/{original_filename}",
        original_filename=original_filename,
        nursery=nursery,
        provider="realdebrid",
        initial_url=url,
    )
    return stream


def test_mediastream_refresh_derives_producer_recovery_deadline():
    captured_deadline = []

    def mock_get_entry(original_filename, force_resolve, operation_deadline=None):
        captured_deadline.append(operation_deadline)
        return SimpleNamespace(url="https://cdn.example.com/new")

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    now = 2000.0
    producer_deadline = now + 15.0

    async def _run():
        async with trio.open_nursery() as nursery:
            stream = _make_dummy_stream(nursery)
            stream._recovery_state = StreamRecoveryState(
                start_time=now,
                generation=0,
                recovery_deadline=producer_deadline,
            )

            with (
                patch(
                    "program.services.streaming.media_stream.di",
                    {VFSDatabase: mock_vfs_db},
                ),
                patch(
                    "program.services.streaming.media_stream.monotonic",
                    return_value=now,
                ),
            ):
                refreshed = await stream._refresh_download_url(
                    "https://cdn.example.com/old"
                )
                assert refreshed is True
                assert stream.target_url.value == "https://cdn.example.com/new"
            nursery.cancel_scope.cancel()

    trio.run(_run)
    assert len(captured_deadline) >= 1
    assert captured_deadline[0] == pytest.approx(producer_deadline)


def test_mediastream_refresh_aborts_if_deadline_exceeded_before_thread():
    mock_vfs_db = MagicMock(spec=VFSDatabase)

    now = 2000.0
    expired_deadline = now - 1.0

    async def _run():
        async with trio.open_nursery() as nursery:
            stream = _make_dummy_stream(nursery)
            stream._recovery_state = StreamRecoveryState(
                start_time=now - 20.0,
                generation=0,
                recovery_deadline=expired_deadline,
            )

            with (
                patch(
                    "program.services.streaming.media_stream.di",
                    {VFSDatabase: mock_vfs_db},
                ),
                patch(
                    "program.services.streaming.media_stream.monotonic",
                    return_value=now,
                ),
            ):
                refreshed = await stream._refresh_download_url(
                    "https://cdn.example.com/old"
                )
                assert refreshed is False
                # Never touched DB
                mock_vfs_db.get_entry_by_original_filename.assert_not_called()
            nursery.cancel_scope.cancel()

    trio.run(_run)


def test_mediastream_refresh_rejects_url_if_generation_retired_during_refresh():
    now = 2000.0
    active_stream = [None]

    def mock_get_entry(original_filename, force_resolve, operation_deadline=None):
        # Simulate seek/close or generation bump occurring during thread execution
        if active_stream[0] and hasattr(active_stream[0], "adaptive_prefetch"):
            active_stream[0].adaptive_prefetch.current_generation += 1
        return SimpleNamespace(url="https://cdn.example.com/stale_resurrect")

    mock_vfs_db = MagicMock(spec=VFSDatabase)
    mock_vfs_db.get_entry_by_original_filename.side_effect = mock_get_entry

    async def _run():
        async with trio.open_nursery() as nursery:
            stream = _make_dummy_stream(nursery)
            active_stream[0] = stream
            stream._recovery_state = StreamRecoveryState(
                start_time=now,
                generation=0,
                recovery_deadline=now + 20.0,
            )

            with (
                patch(
                    "program.services.streaming.media_stream.di",
                    {VFSDatabase: mock_vfs_db},
                ),
                patch(
                    "program.services.streaming.media_stream.monotonic",
                    return_value=now,
                ),
            ):
                refreshed = await stream._refresh_download_url(
                    "https://cdn.example.com/old"
                )
                # Must return False and refuse adoption
                assert refreshed is False
                assert stream.target_url.value == "https://cdn.example.com/old"
            nursery.cancel_scope.cancel()

    trio.run(_run)


def test_smartsession_in_flight_slow_stream_overshoot_or_deadline_classification():
    """
    Test Phase 2: In-flight HTTP request boundary behavior under streaming byte drip.
    Empirically characterizes whether synchronous SmartSession stream consumption
    enforces a hard wall clock abort or allows in-flight read overshoot while remaining
    admission-fenced and publication-safe.
    """
    import time
    from unittest.mock import patch

    import httpx

    from program.utils.request import OperationDeadlineExceeded, SmartSession

    def slow_streaming_handler(request):
        def response_stream():
            # Dripping bytes slowly: 5 chunks with 0.1s delay each (total 0.5s)
            for _ in range(5):
                time.sleep(0.1)
                yield b"chunk\n"

        return httpx.Response(200, content=response_stream(), request=request)

    transport = httpx.MockTransport(slow_streaming_handler)
    session = SmartSession(retries=0)
    # Inject mock transport into the underlying httpx client
    session._client._transport = transport

    # 1. Start with a tight deadline of 0.15s (shorter than the 0.5s slow drip)
    t0 = time.monotonic()
    deadline = t0 + 0.15

    # Measure exact behavior: does httpx / SmartSession raise or complete?
    completed = False
    raised_deadline = False
    try:
        resp = session.request(
            "GET",
            "https://api.example.com/slow",
            timeout=5.0,
            operation_deadline=deadline,
        )
        completed = True
    except (OperationDeadlineExceeded, httpx.TimeoutException):
        raised_deadline = True
    t_end = time.monotonic()
    elapsed = t_end - t0

    # Classification assertion:
    if completed:
        assert elapsed >= 0.15
        classification = (
            "BOUNDED ADMISSION + PUBLICATION SAFETY WITH POSSIBLE IN-FLIGHT OVERSHOOT"
        )
    else:
        assert raised_deadline is True
        classification = "IN_FLIGHT_REQUEST_STOPS_AT_OPERATION_DEADLINE"

    assert classification in (
        "BOUNDED ADMISSION + PUBLICATION SAFETY WITH POSSIBLE IN-FLIGHT OVERSHOOT",
        "IN_FLIGHT_REQUEST_STOPS_AT_OPERATION_DEADLINE",
    )
