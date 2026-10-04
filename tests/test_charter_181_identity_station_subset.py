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
import re
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
        # The pass runs its script through the group-bounded runner.
        monkeypatch.setattr(pw, "run_script_group_bounded", self)
        monkeypatch.setattr(pw, "load_bounded_json", self.receipt)
        # The projection and registry, as the export reads them; these
        # workers carry no target connection to read the registry from.
        self.inputs = "inputs-v1"
        monkeypatch.setattr(pw, "_identity_inputs_fingerprint", lambda _conn: self.inputs)

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


def test_a_station_outside_the_fleet_does_not_spoil_the_subset(monkeypatch) -> None:
    """2026-10-04 03:37Z: LTFM, UUWW and LLBG were collected into the archive
    and staged in the station registry for onboarding, but not in the fleet;
    the builder's full pass ignores them and, asked for one by name, refuses
    the whole subset -- so every pass after one of them reported fell back to
    the full pass (49 s).  They joined the fleet the same day; ZZZZ (ICAO's
    "no designator") stands in for the next station staged ahead of it."""

    fleet = set(pw.coordinate_registry_manifest()["station_ids"])
    assert "ZZZZ" not in fleet and "EGLC" in fleet
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    _report(archive, "awc_o2", "awc", "EGLC")
    _report(archive, "awc_o3", "awc", "ZZZZ")
    assert worker._identify_live_observations()["stations"] == ["EGLC"]
    # Only an outsider moved: nothing of the fleet's to subset, as when no
    # AWC station moved at all.
    _report(archive, "awc_o4", "awc", "ZZZZ")
    alone = worker._identify_live_observations()
    assert (alone["scope"], alone["full_pass_reason"]) == ("full", "no_awc_station_moved")
    assert [call["stations"] for call in runs.calls] == [None, "EGLC", None]


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


def test_a_projection_or_registry_change_is_a_full_pass_at_once(monkeypatch) -> None:
    """2026-10-04: the periodic net was ten minutes, 45-105 s a full pass on
    a busy morning.  What it was for is now asked directly -- the sweep script
    naming the projection, the projection's bytes, the station registry --
    and the period is an hour."""

    assert pw.LIVE_OBSERVATION_IDENTITY_FULL_PASS_SEC == 3600.0
    archive = _archive()
    runs = _Runs(monkeypatch)
    worker = _worker(archive)
    worker._identify_live_observations()
    _report(archive, "awc_o2", "awc", "EGLC")
    assert worker._identify_live_observations()["stations"] == ["EGLC"]
    runs.inputs = "inputs-v2"
    _report(archive, "awc_o3", "awc", "ZUUU")
    changed = worker._identify_live_observations()
    assert (changed["scope"], changed["full_pass_reason"]) == ("full", "identity_inputs_changed")
    # The full pass took the new inputs as its own: subsets resume.
    _report(archive, "awc_o4", "awc", "KORD")
    assert worker._identify_live_observations()["stations"] == ["KORD"]
    # Inputs that cannot be read decide nothing: the subset runs, and the
    # periodic net is what is left.
    runs.inputs = None
    _report(archive, "awc_o5", "awc", "KLAX")
    assert worker._identify_live_observations()["stations"] == ["KLAX"]


def test_the_inputs_fingerprint_reads_what_the_export_reads(tmp_path, monkeypatch) -> None:
    from bayes_basket_target.ssot import apply_production_ssot_schema

    target = sqlite3.connect(":memory:")
    apply_production_ssot_schema(target)
    first = pw._identity_inputs_fingerprint(target)
    assert first and first == pw._identity_inputs_fingerprint(target)
    # The resident's connections return sqlite3.Row (2026-10-04: a repr of
    # those carried an object address and changed on every call).
    target.row_factory = sqlite3.Row
    assert pw._identity_inputs_fingerprint(target) == first
    assert pw._identity_inputs_fingerprint(target) == first
    target.row_factory = None
    assert pw._identity_inputs_fingerprint(None) is None
    script = tmp_path / "sweep.sh"
    projection = tmp_path / "config" / "projection.json"
    projection.parent.mkdir()
    projection.write_text('{"v": 1}')
    script.write_text('PROJECTION="$ROOT/config/projection.json"\n')
    monkeypatch.setattr(pw, "_IDENTITY_SWEEP_SCRIPT", script)
    monkeypatch.setattr(pw, "ROOT", tmp_path)
    one = pw._identity_inputs_fingerprint(target)
    projection.write_text('{"v": 2}')
    two = pw._identity_inputs_fingerprint(target)
    columns = [r[1] for r in target.execute("PRAGMA table_info(calibration_station_registry)")]
    target.execute("PRAGMA foreign_keys=OFF")
    row = {c: f"x-{c}" for c in columns}
    row.update(station_id="ZZZZ", content_sha256="a" * 64)
    try:
        target.execute(
            f"INSERT INTO calibration_station_registry({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
            tuple(row[c] for c in columns),
        )
        three = pw._identity_inputs_fingerprint(target)
    except sqlite3.IntegrityError:
        three = None
    assert len({one, two}) == 2
    assert three is None or three != two
    projection.unlink()
    assert pw._identity_inputs_fingerprint(target) is None


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
    script = (ROOT / "scripts" / "pr12_awc_observation_sweep.sh").read_text()
    # The projection the sweep pins, whichever generation that is: the
    # script exits 78 without it.
    pinned = re.search(r'^PROJECTION="\$ROOT/(.+)"$', script, re.M).group(1)
    (root / pinned).write_text("{}")
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


# --- charter 181(七): across a restart --------------------------------------------------

def _resident(archive, state_path) -> pw.TargetProductionWorker:
    worker = _worker(archive)
    worker.identity_pass_state_path = state_path
    return worker


def test_a_restart_takes_up_the_last_processs_bookkeeping(monkeypatch, tmp_path) -> None:
    """2026-10-04: five restarts by 06:30Z, each opening with a full pass of
    70-157 s on a host short of memory, re-deriving facts already staged."""

    state = tmp_path / "identity_pass_state.json"
    archive = _archive()
    runs = _Runs(monkeypatch)
    first = _resident(archive, state)
    assert first._identify_live_observations()["full_pass_reason"] == "no_staged_snapshot"
    _report(archive, "awc_o2", "awc", "EGLC")
    assert first._identify_live_observations()["stations"] == ["EGLC"]
    assert state.is_file()

    # The next process: nothing new is nothing to do.
    second = _resident(archive, state)
    skipped = second._identify_live_observations()
    assert skipped["state"] == "SKIPPED_NO_NEW_REPORTS" and skipped["adopted_staged_at_utc"]
    _report(archive, "awc_o3", "awc", "ZUUU")
    moved = second._identify_live_observations()
    assert (moved["scope"], moved["stations"]) == ("stations", ["ZUUU"])
    assert [call["stations"] for call in runs.calls] == [None, "EGLC", "ZUUU"]

    # And the one after that, if a report landed while nobody was running.
    _report(archive, "awc_o4", "awc", "KORD")
    third = _resident(archive, state)
    receipt = third._identify_live_observations()
    assert (receipt["scope"], receipt["stations"]) == ("stations", ["KORD"])
    assert receipt["adopted_staged_at_utc"]


def test_the_hourly_full_pass_is_counted_from_the_last_real_one(monkeypatch, tmp_path) -> None:
    state = tmp_path / "identity_pass_state.json"
    archive = _archive()
    runs = _Runs(monkeypatch)
    _resident(archive, state)._identify_live_observations()
    import json as _json
    kept = _json.loads(state.read_text())
    # A subset does not renew the period: the stamp is the full pass's.
    _report(archive, "awc_o2", "awc", "EGLC")
    second = _resident(archive, state)
    second._identify_live_observations()
    assert _json.loads(state.read_text())["full_pass_started_utc"] == kept["full_pass_started_utc"]
    # Past the period, the next process takes nothing up.
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(seconds=pw.LIVE_OBSERVATION_IDENTITY_FULL_PASS_SEC + 5)
    stale = {**_json.loads(state.read_text()), "full_pass_started_utc": old.isoformat().replace("+00:00", "Z")}
    state.write_text(_json.dumps(stale))
    _report(archive, "awc_o3", "awc", "ZUUU")
    late = _resident(archive, state)._identify_live_observations()
    assert (late["scope"], late["full_pass_reason"]) == ("full", "no_staged_snapshot")
    assert "adopted_staged_at_utc" not in late
    assert runs.calls[-1]["stations"] is None


def test_what_the_subset_rule_asks_it_still_asks_after_a_restart(monkeypatch, tmp_path) -> None:
    state = tmp_path / "identity_pass_state.json"
    archive = _archive()
    runs = _Runs(monkeypatch)
    _resident(archive, state)._identify_live_observations()
    # The inputs moved across the restart (a deploy, a new projection).
    runs.inputs = "inputs-v2"
    _report(archive, "awc_o2", "awc", "EGLC")
    changed = _resident(archive, state)._identify_live_observations()
    assert (changed["scope"], changed["full_pass_reason"]) == ("full", "identity_inputs_changed")
    # A new UTC day across the restart.
    import json as _json
    kept = _json.loads(state.read_text())
    kept["watermark"][2] = "2026-09-20"
    state.write_text(_json.dumps(kept))
    _report(archive, "awc_o3", "awc", "ZUUU")
    assert _resident(archive, state)._identify_live_observations()["full_pass_reason"] == "utc_day_changed"


def test_bookkeeping_that_does_not_fit_the_archive_is_not_taken_up(monkeypatch, tmp_path) -> None:
    import json as _json

    state = tmp_path / "identity_pass_state.json"
    archive = _archive()
    _Runs(monkeypatch)
    _resident(archive, state)._identify_live_observations()
    kept = _json.loads(state.read_text())
    # Counts past the archive (a rebuilt archive), a missing table, a
    # receipt that is not a stage, another schema, and garbage.
    for broken in (
        {**kept, "rowids": {**kept["rowids"], "reported_trajectory_observations": 10**9}},
        {**kept, "rowids": {k: v for k, v in kept["rowids"].items() if k != "reported_trajectory_station_halts"}},
        {**kept, "receipt": {**kept["receipt"], "state": "FAILED"}},
        {**kept, "schema": "other"},
    ):
        state.write_text(_json.dumps(broken))
        worker = _resident(archive, state)
        assert worker._adopt_identity_pass_state(pw._identity_table_rowids(archive)) is None
        assert getattr(worker, "_live_observation_identity_rowids", None) is None
    state.write_text("{not json")
    assert _resident(archive, state)._adopt_identity_pass_state(pw._identity_table_rowids(archive)) is None
    state.unlink()
    assert _resident(archive, state)._adopt_identity_pass_state(pw._identity_table_rowids(archive)) is None


def test_a_refused_start_writes_nothing_and_a_worker_without_the_path_keeps_it_in_memory(monkeypatch, tmp_path) -> None:
    state = tmp_path / "identity_pass_state.json"
    archive = _archive()
    runs = _Runs(monkeypatch)
    runs.stage_written = False
    _resident(archive, state)._identify_live_observations()
    assert not state.exists()
    runs.stage_written = True
    _worker(archive)._identify_live_observations()
    assert not state.exists() and list(tmp_path.iterdir()) == []
    # Only the resident's production worker is pointed at the file.
    assert pw.TargetProductionWorker.identity_pass_state_path is None
    daemon = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text()
    assert daemon.count(".identity_pass_state_path = IDENTITY_PASS_STATE_PATH") == 1
    assert 'production_worker.identity_pass_state_path = IDENTITY_PASS_STATE_PATH' in daemon


def test_the_fingerprint_follows_the_scripts_the_sweep_runs(tmp_path, monkeypatch) -> None:
    from bayes_basket_target.ssot import apply_production_ssot_schema

    target = sqlite3.connect(":memory:")
    apply_production_ssot_schema(target)
    (tmp_path / "config").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "config" / "projection.json").write_text('{"v": 1}')
    builder = tmp_path / "scripts" / "build.py"
    builder.write_text("print(1)\n")
    script = tmp_path / "sweep.sh"
    script.write_text('PROJECTION="$ROOT/config/projection.json"\n"$PY" "$ROOT/scripts/build.py" --x\n')
    monkeypatch.setattr(pw, "_IDENTITY_SWEEP_SCRIPT", script)
    monkeypatch.setattr(pw, "ROOT", tmp_path)
    one = pw._identity_inputs_fingerprint(target)
    builder.write_text("print(2)\n")
    two = pw._identity_inputs_fingerprint(target)
    assert one and two and one != two
    builder.unlink()
    assert pw._identity_inputs_fingerprint(target) is None
    # The real sweep names its three steps.
    named = re.findall(r'"\$ROOT/(scripts/[^"]+\.py)"', (ROOT / "scripts" / "pr12_awc_observation_sweep.sh").read_text())
    assert sorted(set(named)) == [
        "scripts/build_pr12_awc_observation_contract.py",
        "scripts/stage_asos_substitute_observation_facts.py",
        "scripts/stage_pr12_awc_observation_contract.py",
    ]


def test_the_charter_carries_the_restart_rule() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    body = text[text.index("\n181. "):text.index("\n182. ")]
    for phrase in ("（七）", "identity_pass_state_path", "identity_pass_state.json",
                   "adopted_staged_at_utc", "stage_asos_substitute_observation_facts.py"):
        assert phrase in body
