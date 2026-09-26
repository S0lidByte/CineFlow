"""
Unit tests for MediaMetadata parsing and Stream Availability & Media Quality Matrix support (AVAIL-001).
"""

from RTN import parse

from program.media.models import MediaMetadata


def test_media_metadata_from_parsed_data_extracts_audio_channels_and_hdr():
    raw_title = "The.Matrix.1999.2160p.UHD.BluRay.TrueHD.Atmos.7.1.DV.HEVC-FLUX"
    parsed = parse(raw_title)

    meta = MediaMetadata.from_parsed_data(parsed, filename=raw_title)

    assert meta.video is not None
    assert meta.video.resolution_label == "4K"
    assert meta.video.resolution_width == 3840
    assert meta.video.resolution_height == 2160
    assert meta.video.hdr_type == "DV"
    assert meta.video.codec == "hevc"
    assert meta.quality_source == "BluRay"
    assert meta.is_remux is False

    # Check audio tracks have channels parsed (7.1 -> 8 channels)
    assert len(meta.audio_tracks) > 0
    assert any(
        track.codec == "TrueHD" and track.channels == 8 for track in meta.audio_tracks
    )
    assert any(
        track.codec == "Atmos" and track.channels == 8 for track in meta.audio_tracks
    )


def test_media_metadata_from_parsed_data_51_audio():
    raw_title = "Inception.2010.1080p.BluRay.DTS-HD.MA.5.1.x264-SPARKS"
    parsed = parse(raw_title)

    meta = MediaMetadata.from_parsed_data(parsed, filename=raw_title)

    assert meta.video is not None
    assert meta.video.resolution_label == "1080p"
    assert meta.video.resolution_width == 1920
    assert meta.video.resolution_height == 1080

    # 5.1 -> 6 channels
    assert any(track.channels == 6 for track in meta.audio_tracks)


def test_media_metadata_remux_flag():
    raw_title = "Interstellar.2014.2160p.UHD.BluRay.REMUX.HDR.HEVC.Atmos-FLUX"
    parsed = parse(raw_title)

    meta = MediaMetadata.from_parsed_data(parsed, filename=raw_title)

    assert meta.is_remux is True
    assert meta.video is not None
    assert meta.video.resolution_label == "4K"
    assert meta.video.hdr_type == "HDR"
