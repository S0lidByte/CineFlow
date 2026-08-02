"""Filesystem cache paths must not claim an entire shared-memory mount."""

from pathlib import Path

import pytest

from program.settings.models import FilesystemModel


@pytest.mark.parametrize("bare_root", ["/dev/shm", "/run/shm"])
def test_bare_tmpfs_cache_paths_use_dedicated_subdirectory(bare_root: str) -> None:
    settings = FilesystemModel(
        cache_dir=Path(bare_root),
        cache_hot_dir=Path(bare_root),
    )

    expected = Path(bare_root) / "riven-cache"
    assert settings.cache_dir == expected
    assert settings.cache_hot_dir == expected


def test_non_tmpfs_and_empty_hot_cache_paths_are_unchanged() -> None:
    settings = FilesystemModel(
        cache_dir=Path("/mnt/cache"),
        cache_hot_dir=None,
    )

    assert settings.cache_dir == Path("/mnt/cache")
    assert settings.cache_hot_dir is None
