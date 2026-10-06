"""D80 characterization: isolated canonical consumption, not live-FUSE certification."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import kink
import pytest
import trio

from program.services.streaming.cache import Cache, CacheConfig
from program.services.streaming.cache_autotune import AutoTuneJobManager
from program.settings import SettingsManager
from program.settings import mutation as mutation_module
from program.settings.models import AppModel, FilesystemModel, Observable

MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SettingsManager, Cache]:
    """Real load/save, isolated file and DI; no production observers or settings."""
    monkeypatch.setattr(Observable, "_notify_observers", None)
    manager = SettingsManager.__new__(SettingsManager)
    manager.settings = AppModel.model_validate(
        {
            "filesystem": {
                "cache_dir": str(tmp_path / "warm"),
                "cache_hot_dir": str(tmp_path / "hot"),
            }
        }
    )
    manager.settings_file = tmp_path / "settings.json"
    manager.observers = []
    manager.last_changed_top_keys = None
    cache = Cache(
        CacheConfig(
            cache_dir=tmp_path / "warm",
            hot_dir=tmp_path / "hot",
            max_size_bytes=1000,
            hot_max_size_bytes=1000,
            metrics_enabled=False,
        )
    )
    container = kink.Container()
    container[Cache] = cache
    monkeypatch.setattr(kink, "di", container)
    monkeypatch.setattr(mutation_module, "settings_manager", manager)
    return manager, cache


def configured_payload() -> dict[str, Any]:
    return {
        "filesystem": {
            "hot_cache_watermark_high_pct": 88.0,
            "hot_cache_watermark_low_pct": 74.0,
            "hot_cache_reserve_pct": 12.0,
            "warm_cache_watermark_high_pct": 82.0,
            "warm_cache_watermark_low_pct": 63.0,
            "warm_cache_reserve_pct": 18.0,
            "warm_cache_min_free_mb": 37,
        }
    }


def test_canonical_nondefault_consumption_same_cache(
    isolated: tuple[SettingsManager, Cache],
) -> None:
    manager, cache = isolated
    result = mutation_module.apply_canonical_settings_mutation(configured_payload())
    assert kink.di[Cache] is cache
    assert isinstance(result.filesystem, FilesystemModel)
    status = cache.watermark_status()
    for key, value in {
        "hot_watermark_high_pct": 88.0,
        "hot_watermark_low_pct": 74.0,
        "warm_watermark_high_pct": 82.0,
        "warm_watermark_low_pct": 63.0,
        "warm_min_free_mb": 37,
    }.items():
        assert status[key] == value
    saved = json.loads(manager.settings_file.read_text(encoding="utf-8"))["filesystem"]
    assert all(
        saved[key] == value for key, value in configured_payload()["filesystem"].items()
    )
    # Reserve is a model geometry constraint, NOT a separate Cache runtime budget.
    assert "hot_reserve_pct" not in CacheConfig.__dataclass_fields__
    assert "warm_reserve_pct" not in CacheConfig.__dataclass_fields__
    assert status["hot_max_bytes"] == status["warm_max_bytes"] == 1000


def test_consumed_hot_and_warm_thresholds_drive_reclaim(
    isolated: tuple[SettingsManager, Cache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, cache = isolated
    mutation_module.apply_canonical_settings_mutation(configured_payload())
    monkeypatch.setattr(
        "program.services.streaming.cache.shutil.disk_usage",
        lambda _: SimpleNamespace(free=100 * MIB),
    )

    async def exercise() -> None:
        # 860/1000 is above default 85%, below configured 88%: no demotion.
        for number in range(4):
            await cache.put(f"hot-{number}", 0, b"h" * 215)
        assert cache.watermark_status()["hot_bytes"] == 860
        await cache._ensure_hot_capacity(30)
        assert cache.watermark_status()["hot_bytes"] <= 740
        for number in range(4):
            assert await cache.get(f"hot-{number}", 0, 214) == b"h" * 215

    trio.run(exercise)


def test_consumed_warm_thresholds_drive_reclaim(
    isolated: tuple[SettingsManager, Cache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, cache = isolated
    mutation_module.apply_canonical_settings_mutation(configured_payload())
    cache.cfg.hot_max_size_bytes = 1
    monkeypatch.setattr(
        "program.services.streaming.cache.shutil.disk_usage",
        lambda _: SimpleNamespace(free=100 * MIB),
    )

    async def exercise() -> None:
        for number in range(4):
            await cache.put(f"warm-{number}", 0, b"w" * 200)
        assert cache.watermark_status()["warm_bytes"] == 800
        # Projected 83% crosses configured 82%, but not default 85%.
        await cache._evict_lru(30)
        assert cache.watermark_status()["warm_bytes"] == 600
        assert not cache.has("warm-0", 0, 199)
        for number in range(1, 4):
            assert await cache.get(f"warm-{number}", 0, 199) == b"w" * 200

    trio.run(exercise)


def test_min_free_is_consumed_by_reclaim(
    isolated: tuple[SettingsManager, Cache],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, cache = isolated
    mutation_module.apply_canonical_settings_mutation(configured_payload())

    async def exercise() -> None:
        # Payload exceeds the hot budget, so admission uses warm storage.
        cache.cfg.hot_max_size_bytes = 1
        monkeypatch.setattr(
            "program.services.streaming.cache.shutil.disk_usage",
            lambda _: SimpleNamespace(free=100 * MIB),
        )
        await cache.put("warm", 0, b"w" * 100)
        assert cache.has("warm", 0, 99)
        monkeypatch.setattr(
            "program.services.streaming.cache.shutil.disk_usage",
            lambda _: SimpleNamespace(free=36 * MIB),
        )
        await cache.trim()
        assert not cache.has("warm", 0, 99)

    trio.run(exercise)


@pytest.mark.parametrize(
    "patch",
    [
        {"hot_cache_reserve_pct": 13},
        {"warm_cache_watermark_low_pct": 80},
        {"warm_cache_min_free_mb": -1},
    ],
)
def test_invalid_geometry_never_persists_or_updates(
    isolated: tuple[SettingsManager, Cache],
    patch: dict[str, int],
) -> None:
    manager, cache = isolated
    payload = configured_payload()
    payload["filesystem"].update(patch)
    before = cache.watermark_status()
    before.pop("free_disk_mb")  # External disk activity is not configuration mutation.
    with pytest.raises(ValueError):
        mutation_module.apply_canonical_settings_mutation(payload)
    assert not manager.settings_file.exists()
    after = cache.watermark_status()
    after.pop("free_disk_mb")
    assert after == before


def test_real_observer_branch_closes_filesystem_before_cache_update(
    isolated: tuple[SettingsManager, Cache],
) -> None:
    """Execute actual Program method AST with inert services; never instantiate Program."""
    manager, cache = isolated
    tree = ast.parse((ROOT / "program/program.py").read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Program"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "initialize_services"
    )
    stop = RuntimeError("test boundary: would construct replacement services")
    previous = MagicMock()
    instance = MagicMock()
    instance.services = previous
    instance._RUNTIME_ONLY_TOP_KEYS = frozenset({"stream", "logging", "log_level"})
    instance._VFS_REINIT_TOP_KEYS = frozenset({"filesystem", "downloaders"})
    namespace: dict[str, Any] = {
        "settings_manager": manager,
        "logger": MagicMock(),
        "Downloader": MagicMock(side_effect=stop),
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "program.py", "exec"),
        namespace,
    )
    manager.register_observer(lambda: namespace["initialize_services"](instance))
    with pytest.raises(ValueError, match="would construct replacement"):
        mutation_module.apply_canonical_settings_mutation(configured_payload())
    previous.filesystem.close.assert_called_once_with()
    assert kink.di[Cache] is cache
    assert cache.cfg.hot_watermark_high_pct is None  # live update never reached
    assert manager.settings_file.exists()  # persistence precedes observer failure
    assert manager.settings.filesystem.hot_cache_watermark_high_pct == 88


def test_implemented_autotune_is_recommendation_only(
    isolated: tuple[SettingsManager, Cache],
    tmp_path: Path,
) -> None:
    manager, cache = isolated
    before = manager.settings.model_dump()
    jobs = AutoTuneJobManager()
    job = jobs.start_job(profile="balanced", sandbox_base_dir=tmp_path)
    assert job.thread is not None
    job.thread.join(timeout=30)
    assert not job.thread.is_alive()
    assert job.status == "completed", job.error
    assert job.recommendation is not None
    snapshot = job.snapshot().model_dump(mode="json")
    rec = job.recommendation
    assert rec.reasons
    assert rec.hot_tier_throughput_mb_s > 0
    assert rec.warm_tier_throughput_mb_s > 0
    assert manager.settings.model_dump() == before
    assert not manager.settings_file.exists()
    assert kink.di[Cache] is cache
    assert cache.cfg.hot_watermark_high_pct is None
    assert not list(tmp_path.glob("cineflow_autotune_*"))
    assert "confidence" not in snapshot["recommendation"]
    evidence = {
        "scope": "isolated Windows temp sandbox; no live tier or FUSE certification",
        "quick": "NOT_IMPLEMENTED",
        "deep": "NOT_IMPLEMENTED",
        "confidence": None,
        "confidence_reason": "No confidence field or estimator implemented",
        "classification": "RECOMMENDATION_ONLY; LIVE_SAFE_APPLY_BLOCKED_BY_REMOUNT",
        "classification_source": "audit, not engine output",
        "benchmark_bytes_per_tier": 8 * MIB,
        "snapshot": snapshot,
        "canonical_configured": configured_payload()["filesystem"],
        "auto_applied": False,
    }
    evidence_path = os.environ.get("D80_CONFIGURATION_EVIDENCE")
    if evidence_path:
        Path(evidence_path).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
