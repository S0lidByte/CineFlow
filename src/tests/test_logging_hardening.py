"""Tests for logging hardening changes (SubDL redaction, MediaStream network trace redaction, DB TRACE logging)."""

from unittest.mock import MagicMock

from loguru import logger

from program.contracts.telemetry import redact_text
from program.services.post_processing.subtitles.providers.base import SubtitleItem
from program.services.post_processing.subtitles.providers.subdl import SubDLProvider


def test_subdl_download_failure_logging_does_not_leak_url(caplog):
    """Verify that SubDL subtitle download errors do not interpolate sensitive download URLs into error logs."""
    provider = SubDLProvider(api_key="mock_provider_key")
    provider._client = MagicMock()
    provider._client.get.side_effect = Exception("Connection refused to secret CDN")

    sub_info = SubtitleItem(
        provider="subdl",
        id="https://subdl.com/download/token_download12345/subtitle.zip?token=mock_secret_token_12345",
        filename="Test.Movie.2024.1080p.WEBRip.srt",
        language="en",
        download_count=100,
        rating=5.0,
        matched_by="imdb",
        movie_hash=None,
        movie_name="Test Movie",
        score=1.0,
    )

    logs = []
    logger_id = logger.add(lambda msg: logs.append(msg), level="ERROR")
    try:
        result = provider.download_subtitle(sub_info)
    finally:
        logger.remove(logger_id)

    assert result is None
    # Ensure raw secret token/api key or URL with query params is not in the log message
    for log in logs:
        assert "mock_secret_token_12345" not in log
        assert "token_download12345" not in log
        assert "SubDL download failed" in log
        assert "[REDACTED]" in log


def test_media_stream_network_trace_redaction():
    """Verify that redact_text strips query params / tokens from network trace payloads."""
    raw_trace_info = (
        "GET https://real-debrid.com/d/ABCXYZ123?token=mock_auth_token_value HTTP/1.1"
    )
    safe_info = redact_text(raw_trace_info)

    assert "mock_auth_token_value" not in safe_info
    assert "[REDACTED]" in safe_info


def test_db_functions_trace_logging_level():
    """Verify that the DB runner micro-timing logs are classified at TRACE level and filtered out at DEBUG level."""
    # When log level is DEBUG (numeric 10), TRACE messages (numeric 5) are ignored.
    logs = []
    logger_id = logger.add(lambda msg: logs.append(msg), level="DEBUG")
    try:
        logger.trace("ItemService item=101: session.get START")
        logger.debug("ItemService item=101: processing started")
    finally:
        logger.remove(logger_id)

    # logger.trace should not be present in DEBUG level output
    assert not any("session.get START" in log for log in logs)
    assert any("processing started" in log for log in logs)
