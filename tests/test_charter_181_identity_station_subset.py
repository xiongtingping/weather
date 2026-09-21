"""Charter 181: the observation-identity pass re-derives only the stations that moved.

The pass ran at every fast-lane checkpoint where a report had landed -- three
to four times a cycle -- and each time re-derived and re-staged the whole
three-day window, ~1,150 revision facts for the handful of new reports
(2026-09-21: 37s of sampled cycle time).  A station's facts depend on its
own archive rows only: the contract built for 3 and for 8 stations was, byte
for byte, the full contract's facts for those stations.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw

ROOT = Path(__file__).resolve().parents[1]


def _archive() -> sqlite3.Connection:
    archive = sqlite3.connect(":memory:")
    archive.execute(
        "CREATE TABLE reported_trajectory_observations("
        " observation_id TEXT PRIMARY KEY, source_lane TEXT, station_id TEXT,"
        " valid_at TEXT)"
    )
    archive.execute(
        "CREATE TABLE reported_trajectory_observation_origins("
        " observation_id TEXT, source_lane TEXT, station_id TEXT)"
    )
    archive.execute(
        "CREATE TABLE reported_trajectory_station_halts("
        " station_id TEXT, event_date TEXT, state TEXT)"
    )
    archive.execute(
        "CREATE TABLE reported_trajectory_station_halt_reviews("
        " station_id TEXT, event_date TEXT)"
    )
    _report(archive, "awc_o1", "awc", "KMIA")
    return archive


def _report(archive, observation_id, lane, station) -> None:
    archive.execute(
        "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?)",
        (observation_id, lane, station, "2026-09-21T06:53:00Z"),
    )
    archive.execute(
        "INSERT INTO reported_trajectory_observation_origins VALUES (?,?,?)",
        (observation_id, lane, station),
    )
    archive.commit()


class _Runs:
    def __init__(self, monkeypatch, *, codes=None, stage_written=True) -> None:
        self.calls: list[dict] = []
        self.codes = list(codes or [])
        self.stage_written = stage_written
        monkeypatch.setattr(pw.subprocess, "run", self)
        monkeypatch.setattr(pw, "load_bounded_json", self.receipt)

    def __call__(self, command, **kwargs):
        env = kwargs.get("env")
        self.calls.append({
            "stations": None if env is None else env.get("PR12_STATIONS"),
            "skip_substitute": None if env is None else env.get("PR12_SKIP_ASOS_SUBSTITUTE"),
        })
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(command, code, stdout="", stderr="")

    def receipt(self, *_a, **_kw):
        stamp = pw.utc_now() if self.stage_written else "2000-01-01T00:00:00.000Z"
        time.sleep(0.002)
        return {"ingested_at_utc": stamp, "revision_fact_count": 3}


def _worker(archive) -> pw.TargetProductionWorker:
    worker = object.__new__(pw.TargetProductionWorker)
    worker.canonical_source_conn = archive
    return worker


def test_the_first_pass_is_full_then_a_new_report_names_its_station(monkeypatch) -> None:
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    first = worker._identify_live_observations()
    assert (first["scope"], first["full_pass_reason"]) == ("full", "no_staged_snapshot")
    _report(archive, "awc_o2", "awc", "EGLC")
    _report(archive, "awc_o3", "awc", "ZUUU")
    second = worker._identify_live_observations()
    assert second["state"] == "STAGED"
    assert (second["scope"], second["stations"]) == ("stations", ["EGLC", "ZUUU"])
    assert runs.calls == [
        {"stations": None, "skip_substitute": None},
        {"stations": "EGLC,ZUUU", "skip_substitute": "1"},
    ]
    # only rows past the last staged pass count
    _report(archive, "awc_o4", "awc", "KORD")
    assert worker._identify_live_observations()["stations"] == ["KORD"]


def test_a_high_frequency_row_names_no_station(monkeypatch) -> None:
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    _report(archive, "awc_o2", "awc", "EGLC")
    _report(archive, "hf_1", "madis_hfmetar", "KLAX")
    assert worker._identify_live_observations()["stations"] == ["EGLC"]
    assert len(runs.calls) == 2


def test_a_halt_review_names_its_station_too(monkeypatch) -> None:
    archive = _archive()
    _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    archive.execute("INSERT INTO reported_trajectory_station_halt_reviews VALUES ('RKSI','2026-09-21')")
    _report(archive, "awc_o2", "awc", "EGLC")
    assert worker._identify_live_observations()["stations"] == ["EGLC", "RKSI"]


def test_a_substitute_fill_alone_is_a_full_pass(monkeypatch) -> None:
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    _report(archive, "asos_p1_KLAX_1", "madis_hfmetar", "KLAX")
    receipt = worker._identify_live_observations()
    assert (receipt["scope"], receipt["full_pass_reason"]) == ("full", "no_awc_station_moved")
    assert runs.calls[-1]["stations"] is None


def test_a_substitute_fill_with_a_report_does_not_skip_the_substitute_stage(monkeypatch) -> None:
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    _report(archive, "asos_p1_KLAX_1", "madis_hfmetar", "KLAX")
    _report(archive, "awc_o2", "awc", "EGLC")
    receipt = worker._identify_live_observations()
    assert receipt["stations"] == ["EGLC"]
    assert runs.calls[-1]["skip_substitute"] == "0"


def test_the_full_pass_comes_back_on_its_period_and_on_a_new_day(monkeypatch) -> None:
    archive = _archive()
    _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    worker._live_observation_identity_full_mono -= pw.LIVE_OBSERVATION_IDENTITY_FULL_PASS_SEC + 1
    _report(archive, "awc_o2", "awc", "EGLC")
    assert worker._identify_live_observations()["full_pass_reason"] == "periodic_full_pass"
    awc, substitute, _day = worker._live_observation_identity_watermark
    worker._live_observation_identity_watermark = (awc, substitute, "2026-09-20")
    _report(archive, "awc_o3", "awc", "ZUUU")
    assert worker._identify_live_observations()["full_pass_reason"] == "utc_day_changed"


def test_a_subset_the_pass_refuses_is_answered_by_the_full_pass(monkeypatch) -> None:
    archive = _archive()
    runs = _Runs(monkeypatch, codes=[0, 1, 0])
    worker = _worker(archive)
    worker._identify_live_observations()
    _report(archive, "awc_o2", "awc", "EGLC")
    receipt = worker._identify_live_observations()
    assert receipt["state"] == "STAGED"
    assert (receipt["scope"], receipt["full_pass_reason"]) == ("full", "subset_pass_failed")
    assert [call["stations"] for call in runs.calls] == [None, "EGLC", None]


def test_a_refused_start_keeps_the_stations_for_the_next_pass(monkeypatch) -> None:
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    runs.stage_written = False  # the single-instance guard turned it away
    _report(archive, "awc_o2", "awc", "EGLC")
    assert worker._identify_live_observations()["stations"] == ["EGLC"]
    runs.stage_written = True
    _report(archive, "awc_o3", "awc", "ZUUU")
    assert worker._identify_live_observations()["stations"] == ["EGLC", "ZUUU"]


def test_an_unreadable_archive_is_a_full_pass(monkeypatch) -> None:
    archive = _archive()
    _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    assert pw._identity_changed_stations(None, since={}, until={}) is None
    assert pw._identity_table_rowids(None) is None


# --- the builder --------------------------------------------------------------------

def test_the_builder_hands_its_stations_to_the_export(monkeypatch, tmp_path) -> None:
    spec = importlib.util.spec_from_file_location(
        "_pr12_builder", ROOT / "scripts" / "build_pr12_awc_observation_contract.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    seen: dict = {}

    class Stop(Exception):
        pass

    def export(*_a, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(builder, "_station_registry", lambda _t: {})
    monkeypatch.setattr(builder, "validate_pr12_authority_scope_projection", lambda p: {"content_sha256": "x"})
    monkeypatch.setattr(builder, "_materialized_day_scopes", lambda *_a: {})
    monkeypatch.setattr(builder, "export_awc_ingress_evidence_v4", export)
    projection = tmp_path / "projection.json"
    projection.write_text("{}")
    target = tmp_path / "target.db"
    target.write_bytes(b"")
    base = [
        "b", "--archive-database", str(tmp_path / "a.db"), "--target-database", str(target),
        "--as-of-utc", "2026-09-21T11:00:00.000Z", "--authority-projection", str(projection),
        "--evidence-output", str(tmp_path / "e"), "--contract-output", str(tmp_path / "c"),
        "--receipt-output", str(tmp_path / "r"),
    ]
    for extra, expected in ((["--stations", "zuuu, EGLC,,ZUUU"], ("EGLC", "ZUUU")), ([], ())):
        monkeypatch.setattr("sys.argv", base + extra)
        with pytest.raises(Stop):
            builder.main()
        assert seen["station_ids"] == expected


# --- the sweep script ------------------------------------------------------------------

def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "config" / "artifacts").mkdir(parents=True)
    (root / "config" / "artifacts" / "pr12_authority_scope_projection_current_20260826T120000Z.json").write_text("{}")
    script = (ROOT / "scripts" / "pr12_awc_observation_sweep.sh").read_text()
    (root / "scripts" / "pr12_awc_observation_sweep.sh").write_text(script)
    (root / "scripts" / "lib" / "single_instance.sh").write_text("single_instance() { :; }\n")
    python = root / ".venv" / "bin" / "python"
    python.write_text(
        "#!/bin/bash\n"
        "if [ \"$1\" = \"-c\" ]; then echo 2026-09-21T11:00:00.000Z; exit 0; fi\n"
        f"echo \"$@\" >> {root}/calls.log\n"
    )
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    return root


def _sweep(root: Path, **env) -> list[str]:
    subprocess.run(
        ["/bin/bash", str(root / "scripts" / "pr12_awc_observation_sweep.sh")],
        check=True, capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), **env},
    )
    return (root / "calls.log").read_text().splitlines()


def test_the_sweep_passes_the_stations_and_skips_the_substitute_stage(tmp_path) -> None:
    root = _fake_root(tmp_path)
    calls = _sweep(root, PR12_STATIONS="EGLC,ZUUU", PR12_SKIP_ASOS_SUBSTITUTE="1")
    assert len(calls) == 2
    assert "build_pr12_awc_observation_contract.py --stations EGLC,ZUUU" in calls[0]
    assert "stage_pr12_awc_observation_contract.py" in calls[1]


def test_the_sweep_unset_is_the_full_pass_it_always_was(tmp_path) -> None:
    root = _fake_root(tmp_path)
    calls = _sweep(root)
    assert len(calls) == 3
    assert "--stations" not in calls[0]
    assert "stage_asos_substitute_observation_facts.py" in calls[2]
