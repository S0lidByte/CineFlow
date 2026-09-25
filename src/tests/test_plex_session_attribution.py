import json

import pytest
from starlette.requests import Request

from program.services.streaming.telemetry import playback_telemetry_collector
from routers.secure.webhooks import plex_webhook
from schemas.playback_telemetry import StreamSessionMetric


def test_stream_session_attribution_update_and_badge():
    tracker = playback_telemetry_collector.register_stream_start(
        stream_id="stream-attr-1",
        media_item_id=101,
        title="Oppenheimer.2023.2160p.UHD.Remux.mkv",
        provider="realdebrid",
        client_ip="192.168.1.50",
        client_user_agent="PlexMediaPlayer/1.0",
    )

    tracker.update_attribution(
        user_name="Alice",
        player_device="Living Room Shield",
        playback_state="playing",
        decision="directplay",
        video_decision="direct",
        audio_decision="direct",
        quality_profile="4K",
        media_resolution="4k",
        media_bitrate_kbps=42000,
    )

    metric = tracker.to_metric()
    assert metric.user_name == "Alice"
    assert metric.player_device == "Living Room Shield"
    assert metric.playback_state == "playing"
    assert metric.decision == "direct_play"
    assert metric.session_badge is not None
    assert "Alice" in metric.session_badge
    assert "Direct Play" in metric.session_badge
    assert "Living Room Shield" in metric.session_badge
    assert "4K" in metric.session_badge

    playback_telemetry_collector.register_stream_complete(
        "stream-attr-1", title="Oppenheimer.2023.2160p.UHD.Remux.mkv"
    )


def test_correlate_plex_session_matching_file_and_ip():
    # Register an active stream
    playback_telemetry_collector.register_stream_start(
        stream_id="stream-plex-match-1",
        media_item_id=202,
        title="Dune.Part.Two.2024.1080p.WEBDL.mkv",
        provider="realdebrid",
        client_ip="10.0.0.45",
        client_user_agent="Plex/1.0",
    )

    # Correlate via Plex play event with matching filename
    matched = playback_telemetry_collector.correlate_plex_session(
        event="media.play",
        user_name="Bob",
        player_device="Apple TV 4K",
        client_ip="10.0.0.45",
        file_path="/mnt/cineflow/movies/Dune.Part.Two.2024.1080p.WEBDL.mkv",
        title="Dune: Part Two",
        decision="directplay",
        media_resolution="1080",
    )

    assert matched >= 1
    snapshot = playback_telemetry_collector.get_snapshot()
    stream_m = next(
        (s for s in snapshot.active_streams if s.stream_id == "stream-plex-match-1"),
        None,
    )
    assert stream_m is not None
    assert stream_m.user_name == "Bob"
    assert stream_m.player_device == "Apple TV 4K"
    assert stream_m.playback_state == "playing"
    assert stream_m.session_badge is not None
    assert "Bob" in stream_m.session_badge
    assert "Apple TV 4K" in stream_m.session_badge

    # Now test pause event
    playback_telemetry_collector.correlate_plex_session(
        event="media.pause",
        user_name="Bob",
        player_device="Apple TV 4K",
        client_ip="10.0.0.45",
        file_path="Dune.Part.Two.2024.1080p.WEBDL.mkv",
    )
    snapshot = playback_telemetry_collector.get_snapshot()
    stream_m = next(
        (s for s in snapshot.active_streams if s.stream_id == "stream-plex-match-1"),
        None,
    )
    assert stream_m is not None
    assert stream_m.playback_state == "paused"

    playback_telemetry_collector.register_stream_complete(
        "stream-plex-match-1", title="Dune.Part.Two.2024.1080p.WEBDL.mkv"
    )


@pytest.mark.asyncio
async def test_plex_webhook_endpoint_session_attribution():
    # Start an active stream to be correlated
    playback_telemetry_collector.register_stream_start(
        stream_id="stream-endpoint-1",
        media_item_id=303,
        title="Severance.S01E01.1080p.mkv",
        provider="realdebrid",
        client_ip="192.168.1.120",
    )

    plex_payload = {
        "event": "media.play",
        "user": True,
        "owner": True,
        "Account": {"id": 1, "title": "Carol"},
        "Player": {
            "title": "Safari Web",
            "publicAddress": "192.168.1.120",
        },
        "Metadata": {
            "title": "Good News About Hell",
            "grandparentTitle": "Severance",
            "type": "episode",
            "Media": [
                {
                    "videoResolution": "1080",
                    "bitrate": 8000,
                    "Part": [
                        {
                            "file": "/riven/vfs/tv/Severance/Severance.S01E01.1080p.mkv",
                            "decision": "directplay",
                        }
                    ],
                }
            ],
        },
    }

    body = json.dumps(plex_payload).encode("utf-8")
    boundary = "----WebKitFormBoundaryTest123"
    form_body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="payload"\r\n\r\n'
        f"{json.dumps(plex_payload)}\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/webhooks/plex",
        "raw_path": b"/api/v1/webhooks/plex",
        "query_string": b"",
        "headers": [
            (
                b"content-type",
                f"multipart/form-data; boundary={boundary}".encode("latin-1"),
            ),
            (b"content-length", str(len(form_body)).encode("latin-1")),
        ],
        "client": ("127.0.0.1", 12345),
    }

    async def receive():
        return {"type": "http.request", "body": form_body, "more_body": False}

    req = Request(scope, receive)
    res = await plex_webhook(req)
    assert res.success is True
    assert res.event == "media.play"
    assert "session attributed" in res.message

    snapshot = playback_telemetry_collector.get_snapshot()
    stream_m = next(
        (s for s in snapshot.active_streams if s.stream_id == "stream-endpoint-1"), None
    )
    assert stream_m is not None
    assert stream_m.user_name == "Carol"
    assert stream_m.player_device == "Safari Web"
    assert stream_m.playback_state == "playing"

    playback_telemetry_collector.register_stream_complete(
        "stream-endpoint-1", title="Severance.S01E01.1080p.mkv"
    )


def test_multi_session_isolated_attribution_alice_and_bob():
    """Verify concurrent distinct sessions (Alice & Bob) remain completely isolated."""
    playback_telemetry_collector._sessions.clear()
    playback_telemetry_collector._events.clear()

    # 1. Start Alice & Bob streams
    tracker_alice = playback_telemetry_collector.register_stream_start(
        stream_id="stream-alice-session",
        media_item_id=101,
        title="Dune.Part.Two.2024.2160p.UHD.Remux.mkv",
        provider="realdebrid",
        client_ip="192.168.1.10",
        client_user_agent="Plex/Shield",
    )

    tracker_bob = playback_telemetry_collector.register_stream_start(
        stream_id="stream-bob-session",
        media_item_id=202,
        title="Shogun.S01E01.1080p.WEBDL.mkv",
        provider="alldebrid",
        client_ip="192.168.1.20",
        client_user_agent="Plex/AppleTV",
    )

    # 2. Correlate Alice with 4K Direct Play
    matched_alice = playback_telemetry_collector.correlate_plex_session(
        event="media.play",
        user_name="Alice",
        player_device="Living Room Shield",
        client_ip="192.168.1.10",
        file_path="/movies/Dune.Part.Two.2024.2160p.UHD.Remux.mkv",
        decision="directplay",
        media_resolution="4k",
        media_bitrate_kbps=45000,
    )
    assert matched_alice >= 1

    # 3. Correlate Bob with 1080p Transcode
    matched_bob = playback_telemetry_collector.correlate_plex_session(
        event="media.play",
        user_name="Bob",
        player_device="Apple TV 4K",
        client_ip="192.168.1.20",
        file_path="/tv/Shogun/Shogun.S01E01.1080p.WEBDL.mkv",
        decision="transcode",
        media_resolution="1080",
        media_bitrate_kbps=12000,
    )
    assert matched_bob >= 1

    # 4. Verify Snapshot has both isolated sessions
    snapshot = playback_telemetry_collector.get_snapshot()
    assert len(snapshot.active_streams) == 2

    alice_m = next((s for s in snapshot.active_streams if s.stream_id == "stream-alice-session"), None)
    bob_m = next((s for s in snapshot.active_streams if s.stream_id == "stream-bob-session"), None)

    assert alice_m is not None
    assert bob_m is not None

    assert alice_m.user_name == "Alice"
    assert alice_m.player_device == "Living Room Shield"
    assert alice_m.decision == "direct_play"
    assert alice_m.playback_state == "playing"
    assert alice_m.quality_profile == "4K"

    assert bob_m.user_name == "Bob"
    assert bob_m.player_device == "Apple TV 4K"
    assert bob_m.decision == "transcode"
    assert bob_m.playback_state == "playing"
    assert bob_m.quality_profile == "1080p"

    # 5. Mutate only Alice to PAUSED, verify Bob is untouched
    playback_telemetry_collector.correlate_plex_session(
        event="media.pause",
        user_name="Alice",
        player_device="Living Room Shield",
        client_ip="192.168.1.10",
        file_path="/movies/Dune.Part.Two.2024.2160p.UHD.Remux.mkv",
    )

    snapshot_after_pause = playback_telemetry_collector.get_snapshot()
    alice_paused = next((s for s in snapshot_after_pause.active_streams if s.stream_id == "stream-alice-session"), None)
    bob_unmutated = next((s for s in snapshot_after_pause.active_streams if s.stream_id == "stream-bob-session"), None)

    assert alice_paused is not None and alice_paused.playback_state == "paused"
    assert bob_unmutated is not None and bob_unmutated.playback_state == "playing"
    assert bob_unmutated.user_name == "Bob"
    assert bob_unmutated.decision == "transcode"

    # 6. Complete Alice, verify only Bob remains active
    playback_telemetry_collector.register_stream_complete(
        "stream-alice-session", title="Dune.Part.Two.2024.2160p.UHD.Remux.mkv"
    )
    snapshot_alice_ended = playback_telemetry_collector.get_snapshot()
    assert len(snapshot_alice_ended.active_streams) == 1
    assert snapshot_alice_ended.active_streams[0].stream_id == "stream-bob-session"

    # Cleanup Bob
    playback_telemetry_collector.register_stream_complete(
        "stream-bob-session", title="Shogun.S01E01.1080p.WEBDL.mkv"
    )

    playback_telemetry_collector.register_stream_complete(
        "stream-endpoint-1", title="Severance.S01E01.1080p.mkv"
    )
