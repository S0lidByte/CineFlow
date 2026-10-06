"""Failure-path checks for non-destructive cache demotion staging."""

import shutil
from pathlib import Path

import pytest

from program.services.streaming.cache import Cache, CacheConfig


def test_partial_staging_copy_is_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=4096,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=200,
            metrics_enabled=False,
        )
    )
    key = cache._key("movie.mkv", 0)
    source = cache._file_for(key, tier="hot")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"original playback bytes")

    def fail_copy(src: Path, dst: Path) -> None:
        dst.write_bytes(b"partial")
        raise OSError("injected copy failure")

    monkeypatch.setattr(shutil, "copyfile", fail_copy)
    with pytest.raises(OSError, match="injected copy failure"):
        cache._stage_demote_hot_to_warm_temp(key)

    assert source.read_bytes() == b"original playback bytes"
    assert not list((tmp_path / "warm").rglob("*.tmp"))
    assert not cache._file_for(key, tier="warm").exists()


def test_missing_source_aborts_staging(tmp_path: Path) -> None:
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            max_size_bytes=4096,
            hot_dir=tmp_path / "hot",
            hot_max_size_bytes=200,
            metrics_enabled=False,
        )
    )
    key = cache._key("missing.mkv", 0)
    with pytest.raises(FileNotFoundError):
        cache._stage_demote_hot_to_warm_temp(key)
    assert not list((tmp_path / "warm").rglob("*.tmp"))
