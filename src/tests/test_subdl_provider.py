"""SubDL provider unit tests (mocked HTTP + ZIP fixture)."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from program.services.post_processing.subtitles.providers.subdl import (
    SubDLProvider,
    alpha3_to_alpha2,
    extract_srt_from_zip,
)
from program.settings.models import AppModel, SubDLProviderConfig


def _zip_with_srt(content: str = "1\n00:00:01,000 --> 00:00:02,000\nHello\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("movie.en.srt", content.encode("utf-8"))
    return buf.getvalue()


def test_alpha3_to_alpha2_mapping():
    assert alpha3_to_alpha2("eng") == "en"
    assert alpha3_to_alpha2("en") == "en"
    assert alpha3_to_alpha2("spa") == "es"


def test_extract_srt_from_zip_reads_first_srt():
    data = _zip_with_srt("SRT BODY")
    assert extract_srt_from_zip(data) == "SRT BODY"


def test_extract_srt_from_zip_rejects_bad_zip():
    assert extract_srt_from_zip(b"not-a-zip") is None


def test_subdl_defaults_disabled_without_key():
    validated = AppModel.model_validate({})
    assert validated.post_processing.subtitle.providers.subdl.enabled is False
    assert validated.post_processing.subtitle.providers.subdl.api_key == ""


def test_subdl_enabled_requires_api_key():
    with pytest.raises(ValueError):
        SubDLProviderConfig(enabled=True, api_key="")


def test_subdl_search_and_download(monkeypatch):
    provider = SubDLProvider(api_key="test-key")

    search_payload = {
        "status": True,
        "subtitles": [
            {
                "release_name": "Inception.2010.1080p",
                "name": "inception.srt",
                "lang": "en",
                "url": "/subtitle/123.zip",
                "season": None,
                "episode": None,
            }
        ],
    }

    search_response = MagicMock()
    search_response.raise_for_status = MagicMock()
    search_response.json.return_value = search_payload

    download_response = MagicMock()
    download_response.raise_for_status = MagicMock()
    download_response.content = _zip_with_srt("DOWNLOADED")

    def fake_get(url, params=None, timeout=None):
        if "subtitles" in str(url) or (params and "api_key" in (params or {})):
            # Client uses relative "subtitles" path
            if params is not None:
                assert params["api_key"] == "test-key"
                assert params["tmdb_id"] == "27205"
                assert params["languages"] == "en"
                return search_response
        return download_response

    provider._client.get = fake_get  # type: ignore[method-assign]

    results = provider.search_subtitles(
        imdb_id="",
        language="eng",
        tmdb_id="27205",
    )
    assert len(results) == 1
    assert results[0].provider == "subdl"
    assert results[0].language == "eng"

    content = provider.download_subtitle(results[0])
    assert content == "DOWNLOADED"
    provider.close()
