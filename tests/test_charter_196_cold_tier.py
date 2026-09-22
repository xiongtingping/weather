"""Charter 196: the hot tier's evictions go to the cold tier on the external
volume, the live path never reads it, and an unusable volume degrades.

User ruling 2026-09-22 (「能否把模型和历史的原始数据移到外置硬盘，但是把校准、
融合、权重、oos、拟合等留在本机？如果可以就执行！」).  What these bind:

* an attached-but-unreadable volume (macOS TCC under launchd) is as loud as
  an unplugged one, because ``stat`` answers while listing does not;
* eviction is copy -> verify on the volume -> manifest -> unlink, and falls
  back to the pre-196 discard when the tier is not usable;
* a reference root the pass cannot read narrows the discard to re-obtainable
  stores -- the 2026-09-21/22 passes read ``data/research`` as empty;
* production's own readers treat an evicted body as absent (re-fetch) and
  never name the volume.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.official_nwp_raw_retention as retention  # noqa: E402
from bayes_basket_target.native_raw_object_store import (  # noqa: E402
    find_range_addressed_message,
    store_range_alias,
)
from bayes_basket_target.offloaded_paths import (  # noqa: E402
    OffloadedVolumeMissing,
    OffloadedVolumeUnreadable,
    assert_present,
    cold_tier_status,
    copy_to_cold_tier,
    evicted_copy,
    evicted_path,
    is_offloaded,
)
from scripts import cold_tier_offload  # noqa: E402

needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions",
)


@pytest.fixture(autouse=True)
def _no_host_capture_processes(monkeypatch) -> None:
    monkeypatch.setattr(retention, "_native_active_capture_processes", lambda: [])


@pytest.fixture()
def cold(tmp_path: Path) -> dict:
    root = tmp_path / "volume" / "weather_cold"
    root.mkdir(parents=True)
    status = cold_tier_status(root, local_reference=None, min_free_bytes=0)
    assert status["state"] == "available"
    return status


@pytest.fixture()
def unreadable_dir(tmp_path: Path):
    """What TCC does to a launchd job: stat works, listing is refused."""

    path = tmp_path / "tcc_denied"
    path.mkdir()
    (path / "doc.json").write_text("{}", encoding="utf-8")
    path.chmod(0)
    yield path
    path.chmod(0o755)


def _body(store: Path, payload: bytes, *, age_days: float = 30.0) -> Path:
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{hashlib.sha256(payload).hexdigest()}.grib2"
    path.write_bytes(payload)
    stamp = time.time() - age_days * 86400.0
    os.utime(path, (stamp, stamp))
    return path


# --- the guard -----------------------------------------------------------


@needs_permissions
def test_an_attached_but_unreadable_volume_is_as_loud_as_an_unplugged_one(
    unreadable_dir: Path,
) -> None:
    # The silent half, measured: stat says it is there, the walk says empty.
    assert unreadable_dir.is_dir() is True
    assert list(os.walk(unreadable_dir)) == []
    with pytest.raises(OffloadedVolumeUnreadable):
        assert_present(unreadable_dir, what="data/research")
    # Every caller that refuses the unplugged case refuses this one too.
    assert issubclass(OffloadedVolumeUnreadable, OffloadedVolumeMissing)


def test_a_path_under_an_unplugged_link_counts_as_offloaded(tmp_path: Path) -> None:
    link = tmp_path / "research"
    link.symlink_to(tmp_path / "no_such_volume" / "research")
    below = link / "bayes_basket_target"
    assert below.exists() is False  # the silent answer
    assert is_offloaded(below) is True
    with pytest.raises(OffloadedVolumeMissing):
        assert_present(below)


# --- the tier's own state ------------------------------------------------


@needs_permissions
def test_cold_tier_status_names_every_way_it_can_be_unusable(
    tmp_path: Path, unreadable_dir: Path
) -> None:
    assert cold_tier_status(None)["state"] == "disabled"
    assert cold_tier_status(tmp_path / "unplugged")["state"] == "missing"
    assert cold_tier_status(unreadable_dir, local_reference=None)["state"] == "unreadable"
    # A folder of the right name on the internal disk frees nothing.
    same = tmp_path / "same_disk"
    same.mkdir()
    assert cold_tier_status(same, local_reference=tmp_path)["state"] == "same_device"
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        assert (
            cold_tier_status(readonly, local_reference=None, min_free_bytes=0)["state"]
            == "unwritable"
        )
    finally:
        readonly.chmod(0o755)
    assert (
        cold_tier_status(same, local_reference=None, min_free_bytes=1 << 62)["state"]
        == "low_space"
    )


def test_a_copy_is_verified_on_the_volume_and_never_touches_the_source(
    tmp_path: Path, cold: dict
) -> None:
    source = tmp_path / "repo" / "data" / "raw" / "x.gz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"payload")
    destination = evicted_path(source, root=cold["root"], repo_root=tmp_path / "repo")
    assert destination == Path(cold["root"]) / "evicted" / "data" / "raw" / "x.gz"
    with pytest.raises(OSError, match="sha256_mismatch"):
        copy_to_cold_tier(source, destination, expected_sha256="0" * 64)
    assert not destination.exists() and not destination.with_name("x.gz.partial").exists()
    assert source.read_bytes() == b"payload"
    first = copy_to_cold_tier(source, destination)
    again = copy_to_cold_tier(source, destination)
    assert first["copied"] is True and again["copied"] is False
    assert destination.read_bytes() == b"payload" and source.exists()


def test_offline_readers_assert_before_reading_the_cold_copy(tmp_path: Path, cold: dict) -> None:
    repo = tmp_path / "repo"
    source = repo / "data" / "raw" / "x.gz"
    with pytest.raises(OffloadedVolumeMissing):
        evicted_copy(source, root=tmp_path / "unplugged" / "weather_cold", repo_root=repo)
    with pytest.raises(FileNotFoundError):
        evicted_copy(source, root=cold["root"], repo_root=repo)
    target = evicted_path(source, root=cold["root"], repo_root=repo)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"b")
    assert evicted_copy(source, root=cold["root"], repo_root=repo) == target


# --- the NWP retention pass ------------------------------------------------


def test_eviction_moves_to_the_cold_tier_and_production_then_refetches(
    tmp_path: Path, cold: dict
) -> None:
    repo = tmp_path / "repo"
    source_root = repo / "data" / "runtime" / "official_nwp_sources"
    store = source_root / "ecmwf_pf50" / "raw"
    body = _body(store, b"old unreferenced body")
    store_range_alias(
        object_key="20260801/00z/ifs/enfo.grib2",
        range_start=0,
        range_end=20,
        message_sha256=body.name.split(".", 1)[0],
        root=store,
    )
    audits = tmp_path / "audits"
    audits.mkdir()

    plan = retention.build_native_plan([source_root], reference_roots=[audits])
    assert [row["path"] for row in plan["unreferenced_files"]] == [str(body)]
    applied = retention.apply_native_unreferenced(
        plan, cold_tier=retention._ColdTier(cold, repo_root=repo)
    )

    assert not body.exists()
    cold_copy = Path(cold["root"]) / "evicted" / body.relative_to(repo)
    assert hashlib.sha256(cold_copy.read_bytes()).hexdigest() == body.name.split(".", 1)[0]
    manifest = json.loads((source_root / "raw_cold_archive_manifest.jsonl").read_text().strip())
    assert manifest["state"] == "body_moved_to_cold_tier"
    assert manifest["cold_path"] == str(cold_copy)
    assert applied["unreferenced_cold_tier"]["moved_to_cold_tier_count"] == 1
    # The live reader looks only in its own roots: the evicted body is
    # absent to it, so the capture downloads again -- it never reads the volume.
    assert (
        find_range_addressed_message(
            object_key="20260801/00z/ifs/enfo.grib2",
            range_start=0,
            range_end=20,
            roots=[store],
        )
        is None
    )


def test_an_unusable_cold_tier_degrades_to_the_pre_196_discard(tmp_path: Path) -> None:
    source_root = tmp_path / "official_nwp_sources"
    body = _body(source_root / "ecmwf_pf50" / "raw", b"body")
    audits = tmp_path / "audits"
    audits.mkdir()
    plan = retention.build_native_plan([source_root], reference_roots=[audits])
    retention.apply_native_unreferenced(
        plan, cold_tier=cold_tier_status(tmp_path / "unplugged")
    )
    assert not body.exists()
    manifest = json.loads((source_root / "raw_cold_archive_manifest.jsonl").read_text().strip())
    assert manifest["state"] == "body_discarded"
    assert manifest["cold_tier_state"] == "missing"


def test_a_volume_lost_mid_pass_discards_the_rest_instead_of_stalling(
    tmp_path: Path, cold: dict, monkeypatch
) -> None:
    source_root = tmp_path / "official_nwp_sources"
    bodies = [_body(source_root / "gefs" / "raw", f"b{index}".encode()) for index in range(3)]
    audits = tmp_path / "audits"
    audits.mkdir()
    calls = []

    def unplugged(*args, **kwargs):
        calls.append(args)
        raise OSError("device not configured")

    monkeypatch.setattr(retention, "copy_to_cold_tier", unplugged)
    plan = retention.build_native_plan([source_root], reference_roots=[audits])
    retention.apply_native_unreferenced(
        plan, cold_tier=retention._ColdTier(cold, repo_root=tmp_path)
    )
    assert len(calls) == 1  # the first failure turns the tier off
    assert not any(path.exists() for path in bodies)
    rows = [
        json.loads(line)
        for line in (source_root / "raw_cold_archive_manifest.jsonl").read_text().splitlines()
    ]
    assert {row["state"] for row in rows} == {"body_discarded"}
    assert all(row["cold_tier_state"].startswith("move_failed:") for row in rows)


def test_the_budget_lane_moves_what_it_selects(tmp_path: Path, cold: dict) -> None:
    repo = tmp_path / "repo"
    source_root = repo / "official_nwp_sources"
    store = source_root / "ecmwf_pf50" / "raw"
    old = _body(store, b"x" * 1000, age_days=10)
    new = _body(store, b"y" * 1000, age_days=5)
    audits = tmp_path / "audits"
    audits.mkdir()
    (audits / "packet.json").write_text(
        json.dumps([old.name.split(".")[0], new.name.split(".")[0]]), encoding="utf-8"
    )
    plan = retention.build_native_plan([source_root], reference_roots=[audits])
    selection = retention.plan_budget_cold_archive(
        plan, budget_root=source_root, budget_bytes=1500, store_keys=["ecmwf_pf50/raw"]
    )
    result = retention.apply_budget_cold_archive(
        plan, selection, cold_tier=retention._ColdTier(cold, repo_root=repo)
    )
    assert not old.exists() and new.is_file()
    assert (Path(cold["root"]) / "evicted" / old.relative_to(repo)).is_file()
    assert result["budget_cold_tier"]["moved_to_cold_tier_count"] == 1


def test_a_blind_reference_root_narrows_the_discard_to_reobtainable_stores(
    tmp_path: Path,
) -> None:
    """2026-09-21 and 09-22: ``data/research`` read as empty under launchd,
    and "unreferenced" was decided without it."""

    source_root = tmp_path / "official_nwp_sources"
    pf50 = _body(source_root / "ecmwf_pf50" / "raw", b"reobtainable")
    icon = _body(source_root / "icon" / "raw", b"gone for good if discarded")
    audits = tmp_path / "audits"
    audits.mkdir()
    research = tmp_path / "research"
    research.symlink_to(tmp_path / "unplugged_volume" / "research")

    plan = retention.build_native_plan([source_root], reference_roots=[audits, research])
    assert [row["state"] for row in plan["unavailable_reference_roots"]] == ["missing"]
    narrowed = retention.restrict_purge_when_blind(plan, ["ecmwf_pf50/raw", "gefs/raw"])
    assert [row["path"] for row in narrowed["unreferenced_files"]] == [str(pf50)]
    assert [row["path"] for row in narrowed["withheld_reference_root_unavailable_files"]] == [
        str(icon)
    ]
    retention.apply_native_unreferenced(narrowed)
    assert not pf50.exists() and icon.is_file()
    # With every root readable nothing is narrowed.
    seeing = retention.build_native_plan([source_root], reference_roots=[audits])
    assert retention.restrict_purge_when_blind(seeing, ["ecmwf_pf50/raw"]) == seeing


@needs_permissions
def test_a_tcc_denied_reference_root_is_recorded_not_read_as_empty(
    tmp_path: Path, unreadable_dir: Path
) -> None:
    source_root = tmp_path / "official_nwp_sources"
    _body(source_root / "icon" / "raw", b"body")
    plan = retention.build_native_plan([source_root], reference_roots=[unreadable_dir])
    assert [row["state"] for row in plan["unavailable_reference_roots"]] == ["unreadable"]
    narrowed = retention.restrict_purge_when_blind(plan, [])
    assert narrowed["unreferenced_count"] == 0


@needs_permissions
def test_an_unreadable_source_root_is_recorded_not_counted_as_zero_stores(
    tmp_path: Path, unreadable_dir: Path
) -> None:
    audits = tmp_path / "audits"
    audits.mkdir()
    plan = retention.build_native_plan([unreadable_dir], reference_roots=[audits])
    assert [row["state"] for row in plan["unavailable_source_roots"]] == ["unreadable"]
    assert plan["source_root_rows"] == []


# --- the file lanes ------------------------------------------------------


def _aged(path: Path, payload: bytes, *, age_hours: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_hours * 3600.0
    os.utime(path, (stamp, stamp))
    return path


def test_only_past_dated_capture_archives_are_cold(tmp_path: Path) -> None:
    root = tmp_path / "official_native_capture"
    today = datetime(2026, 9, 22, 8, tzinfo=timezone.utc)
    old = _aged(root / "ecmwf_2026-09-18_d1.json", b"a", age_hours=100)
    follow = _aged(root / "icon_2026-09-19_d0_06z.json", b"b", age_hours=80)
    edge = _aged(root / "gefs_2026-09-20_d0.json", b"c", age_hours=50)
    current = _aged(root / "run_2026-09-21_inbox.json", b"d", age_hours=30)
    undated = _aged(root / "g11d1.json", b"e", age_hours=1000)
    rewritten = _aged(root / "eccc_2026-09-10_d2.json", b"f", age_hours=1)
    selected = cold_tier_offload.select_capture_archives(
        root, today_utc=today, keep_days=3, now_epoch=time.time()
    )
    assert selected == sorted([old, follow])
    assert edge not in selected and current not in selected
    assert undated not in selected and rewritten not in selected


def test_the_file_lane_moves_verified_and_keeps_what_is_open(
    tmp_path: Path, cold: dict
) -> None:
    repo = tmp_path / "repo"
    madis = repo / "data" / "raw" / "madis_hfmetar"
    files = [
        _aged(madis / f"{hashlib.sha256(bytes([i])).hexdigest()}.gz", bytes([i]) * 64, age_hours=24 * 9)
        for i in range(3)
    ]
    young = _aged(madis / f"{'f' * 64}.gz", b"young", age_hours=2)
    selected = cold_tier_offload.select_madis_objects(madis, keep_days=7, now_epoch=time.time())
    assert selected == sorted(files) and young not in selected
    manifest = repo / "data" / "runtime" / "cold_tier_evictions.jsonl"
    result = cold_tier_offload.offload(
        "madis_hfmetar_raw",
        selected,
        cold_status=cold,
        manifest=manifest,
        apply=True,
        repo_root=repo,
        open_paths=lambda paths: {str(paths[0])},
    )
    assert result["state"] == "applied"
    assert result["moved_count"] == 2 and result["skipped_open_count"] == 1
    assert selected[0].is_file() and not selected[1].exists() and not selected[2].exists()
    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    for row in rows:
        landed = Path(row["cold_path"])
        assert hashlib.sha256(landed.read_bytes()).hexdigest() == row["sha256"]
        assert landed == Path(cold["root"]) / "evicted" / Path(row["path"]).relative_to(repo)


def test_the_file_lane_keeps_everything_when_the_tier_is_unusable(tmp_path: Path) -> None:
    path = _aged(tmp_path / "raw" / f"{'a' * 64}.gz", b"x", age_hours=24 * 30)
    result = cold_tier_offload.offload(
        "madis_hfmetar_raw",
        [path],
        cold_status=cold_tier_status(tmp_path / "unplugged"),
        manifest=tmp_path / "manifest.jsonl",
        apply=True,
        repo_root=tmp_path,
    )
    assert result["state"] == "skipped_cold_tier_missing"
    assert path.is_file() and not (tmp_path / "manifest.jsonl").exists()


# --- the live path never names the volume ---------------------------------

_COLD_NAMES = {
    "evicted_copy",
    "evicted_path",
    "cold_tier_root",
    "cold_tier_status",
    "copy_to_cold_tier",
    "DEFAULT_COLD_TIER_ROOT",
}
_LIVE_ENTRY_SCRIPTS = (
    "scripts/weather_basket_yes_daemon.py",
    "scripts/weather_maintenance_worker.py",
    "scripts/capture_official_native_ensemble_forward.py",
)


def test_production_code_never_reaches_for_the_cold_tier() -> None:
    """Asserted on names the code binds and calls, not on how it looks: an
    import of a cold-tier function, an attribute access to one, or a string
    naming ``/Volumes/`` anywhere in the resident's package or entry scripts."""

    files = [
        path
        for path in sorted((ROOT / "bayes_basket_target").glob("*.py"))
        if path.name != "offloaded_paths.py"
    ] + [ROOT / name for name in _LIVE_ENTRY_SCRIPTS]
    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("offloaded_paths"):
                offenders += [f"{path.name}:import:{a.name}" for a in node.names if a.name in _COLD_NAMES]
            elif isinstance(node, ast.Attribute) and node.attr in _COLD_NAMES:
                offenders.append(f"{path.name}:attr:{node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "/Volumes/" in node.value:
                offenders.append(f"{path.name}:literal")
    assert offenders == []
