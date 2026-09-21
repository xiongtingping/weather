"""Charter 189 (user, 2026-09-21: 「乙」): the OOS pass runs in its own process.

The worker claims the lane in a heartbeat; the cycle delegates only while
that claim is fresh, from a live pid, and not withdrawn -- otherwise it runs
the pass itself as before.  A station whose activation passes is handed to
the cycle, which replays its P0 (serving is written by production only).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "_maintenance_worker", ROOT / "scripts" / "weather_maintenance_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- is the worker there -------------------------------------------------------

def _beat(path: Path, **overrides) -> None:
    beat = {
        "pid": os.getpid(), "updated_epoch": time.time(),
        "lanes": ["native_ensemble_oos"], "last_state": "OOS_READY",
    }
    beat.update(overrides)
    path.write_text(json.dumps(beat))


def test_a_fresh_claim_from_a_live_pid_is_alive(tmp_path) -> None:
    path = tmp_path / "hb.json"
    _beat(path)
    assert pw.maintenance_worker_alive(path=path)["alive"] is True


@pytest.mark.parametrize("overrides,reason", [
    ({"updated_epoch": 0.0}, "heartbeat_stale"),
    ({"lanes": []}, "lane_not_served"),
    ({"pid": 999_999_9}, "worker_gone"),
])
def test_anything_else_hands_the_lane_back(tmp_path, overrides, reason) -> None:
    path = tmp_path / "hb.json"
    _beat(path, **overrides)
    verdict = pw.maintenance_worker_alive(path=path)
    assert verdict["alive"] is False and verdict["reason"] == reason


def test_no_heartbeat_is_not_alive(tmp_path) -> None:
    assert pw.maintenance_worker_alive(path=tmp_path / "none.json")["alive"] is False


# --- the handoff file ------------------------------------------------------------

def test_handoffs_are_sequenced_and_bounded(tmp_path, monkeypatch) -> None:
    path = tmp_path / "handoff.json"
    monkeypatch.setattr(pw, "MAINTENANCE_HANDOFF_KEEP", 3)
    seqs = [pw.append_oos_handoff({"station_id": f"S{i}"}, path=path)["seq"] for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert [e["seq"] for e in pw._read_oos_handoff(path)] == [3, 4, 5]


# --- the worker's step -------------------------------------------------------------

class _Report(dict):
    pass


def _report(station: str, passed: bool) -> dict:
    return {
        "schema": "oos_v1", "content_sha256": "c" * 64,
        "registered_system_count": 4, "formal_p0_eligible": passed,
        "cross_candidates": [{
            "season_bin": "SON", "lead_bucket": "h24_48", "run_cycle": "00Z",
            "state": "PRODUCTION_NO_LIVE_OOS_PASS" if passed else "ACCUMULATING",
        }],
    }


def _worker(monkeypatch, tmp_path, *, signatures, vintage, passed=True, fail=False):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.commit_conn = sqlite3.connect(":memory:")
    built: list[str] = []
    persisted: list[bool] = []
    monkeypatch.setattr(pw, "_candidate_ensemble_station_ids", lambda conn, limit: ["A", "B", "C"])
    monkeypatch.setattr(pw, "_native_ensemble_truth_signatures", lambda conn: dict(signatures))
    monkeypatch.setattr(pw, "_native_ensemble_artifact_vintage_by_station", lambda conn: dict(vintage))

    def build(conn, *, station_id, **kw):
        if fail:
            raise RuntimeError("boom")
        built.append(station_id)
        return _report(station_id, passed)

    def persist(conn, report, **kw):
        persisted.append(conn.in_transaction)
        return {"state": "persisted"}

    monkeypatch.setattr(pw, "build_native_ensemble_oos", build)
    monkeypatch.setattr(pw, "persist_native_ensemble_oos", persist)
    monkeypatch.setattr(pw, "MAINTENANCE_OOS_HANDOFF_PATH", tmp_path / "handoff.json")
    return worker, built, persisted


def test_a_station_whose_truth_moved_goes_first_stalest_artifact_first(monkeypatch, tmp_path) -> None:
    worker, built, persisted = _worker(
        monkeypatch, tmp_path,
        signatures={"A": "a1", "B": "b1", "C": "c1"},
        vintage={"A": "2026-09-20", "B": "2026-09-18", "C": "2026-09-19"},
    )
    receipt = worker.run_maintenance_oos_step(now_utc="2026-09-21T13:00:00.000Z")
    assert built == ["B"] and receipt["station_choice"] == "truth_moved_stalest_first"
    assert persisted == [True]  # the persist ran inside the write transaction
    assert receipt["handed_off"]["station_id"] == "B"
    assert pw._read_oos_handoff(tmp_path / "handoff.json")[0]["station_id"] == "B"
    # B is now current; the next stalest moved station is C
    worker.run_maintenance_oos_step(now_utc="2026-09-21T13:01:00.000Z")
    assert built == ["B", "C"]


def test_with_no_truth_moved_the_fleet_rotates(monkeypatch, tmp_path) -> None:
    worker, built, _ = _worker(monkeypatch, tmp_path, signatures={}, vintage={}, passed=False)
    for _ in range(4):
        receipt = worker.run_maintenance_oos_step(now_utc="2026-09-21T13:00:00.000Z")
    assert built == ["A", "B", "C", "A"]
    assert receipt["handed_off"] is None
    assert not (tmp_path / "handoff.json").exists()


def test_a_failed_step_is_a_receipt(monkeypatch, tmp_path) -> None:
    worker, _, _ = _worker(monkeypatch, tmp_path, signatures={}, vintage={}, fail=True)
    receipt = worker.run_maintenance_oos_step(now_utc="2026-09-21T13:00:00.000Z")
    assert receipt["state"] == "blocked_fail_closed" and "boom" in receipt["reason"]


# --- the cycle's half ----------------------------------------------------------------

def test_the_cycle_replays_each_handed_station_once(monkeypatch, tmp_path) -> None:
    path = tmp_path / "handoff.json"
    monkeypatch.setattr(pw, "MAINTENANCE_OOS_HANDOFF_PATH", path)
    now = "2026-09-21T13:00:00.000Z"
    fresh = pw.utc_now()
    pw.append_oos_handoff({"station_id": "OLD", "activated_at_utc": "2026-09-20T00:00:00.000Z"}, path=path)
    pw.append_oos_handoff({"station_id": "B", "activated_at_utc": fresh}, path=path)
    pw.append_oos_handoff({"station_id": "B", "activated_at_utc": fresh}, path=path)
    worker = object.__new__(pw.TargetProductionWorker)
    replayed: list[str] = []
    monkeypatch.setattr(
        worker, "_replay_oos_activation_p0",
        lambda *, oos_station, valid_native_packets, now, passed: replayed.append(oos_station) or {"state": "ok"},
    )
    first = worker._delegated_oos_handoff(valid_native_packets=[], now=now, delegate={"alive": True})
    assert replayed == ["B"]  # an entry older than the restart window is not replayed
    assert first["state"] == "delegated_to_maintenance_worker"
    assert worker._delegated_oos_handoff(valid_native_packets=[], now=now, delegate={})["handoff_count"] == 0
    pw.append_oos_handoff({"station_id": "C", "activated_at_utc": pw.utc_now()}, path=path)
    worker._delegated_oos_handoff(valid_native_packets=[], now=now, delegate={})
    assert replayed == ["B", "C"]


# --- the worker loop -----------------------------------------------------------------

class _StepWorker:
    def __init__(self, states):
        self.states = list(states)

    def run_maintenance_oos_step(self, *, now_utc):
        return {"state": self.states.pop(0), "station_id": "A"}


def test_failures_hand_the_lane_back_until_a_step_succeeds(tmp_path) -> None:
    script = _script()
    hb = tmp_path / "hb.json"
    loop = script.MaintenanceLoop(
        _StepWorker(["blocked_fail_closed"] * 3 + ["OOS_READY"]), heartbeat_path=hb,
    )
    for expected in (["native_ensemble_oos"], ["native_ensemble_oos"], [], ["native_ensemble_oos"]):
        loop.step()
        assert json.loads(hb.read_text())["lanes"] == expected


def test_the_loop_stops_with_the_lane_released_and_follows_its_parent(tmp_path) -> None:
    script = _script()
    hb = tmp_path / "hb.json"
    loop = script.MaintenanceLoop(
        _StepWorker(["OOS_READY"] * 5), heartbeat_path=hb, interval_sec=1.0,
        sleep=lambda _s: None,
    )
    loop.run(max_steps=2)
    beat = json.loads(hb.read_text())
    assert loop.steps == 2 and beat["state"] == "stopped" and beat["lanes"] == []
    orphan = script.MaintenanceLoop(_StepWorker(["OOS_READY"]), heartbeat_path=hb, parent_pid=-1)
    orphan.run()
    assert orphan.steps == 0


# --- the resident's supervisor ----------------------------------------------------------

class _Proc:
    def __init__(self, *args, **kwargs):
        self.pid = 4242
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


def test_the_supervisor_starts_restarts_after_backoff_and_stops(tmp_path, monkeypatch) -> None:
    import scripts.weather_basket_yes_daemon as daemon

    clock = [0.0]
    made: list[_Proc] = []

    def popen(*args, **kwargs):
        proc = _Proc()
        made.append(proc)
        return proc

    sup = daemon.MaintenanceWorkerSupervisor(
        archive_db=tmp_path / "archive.db", log_path=tmp_path / "mw.log",
        popen=popen, clock=lambda: clock[0], enabled=True,
    )
    assert sup.tick()["state"] == "started"
    assert sup.tick()["state"] == "running"
    made[0].code = 1
    assert sup.tick()["state"] == "backing_off"
    clock[0] += daemon.MAINTENANCE_WORKER_RESTART_BACKOFF_SEC + 1
    assert sup.tick()["state"] == "started" and len(made) == 2
    sup.stop()
    assert made[1].terminated
    assert daemon.MaintenanceWorkerSupervisor(
        archive_db=tmp_path / "a.db", enabled=False,
    ).tick()["state"] == "disabled"


def test_the_worker_rests_at_least_as_long_as_it_worked(tmp_path) -> None:
    script = _script()
    loop = script.MaintenanceLoop(_StepWorker([]), heartbeat_path=tmp_path / "hb.json", interval_sec=60.0)
    assert loop.rest_after(12.0) == 60.0
    assert loop.rest_after(67.0) == 67.0


# --- code skew: the worker runs the resident's code or claims nothing -----------------

def test_the_skew_check_names_the_heartbeat_the_cycle_reads() -> None:
    assert _script().HEARTBEAT_PATH == pw.MAINTENANCE_WORKER_HEARTBEAT_PATH


def test_code_changed_since_the_resident_started_is_named(tmp_path) -> None:
    script = _script()
    (tmp_path / "bayes_basket_target" / "sub").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    old = tmp_path / "bayes_basket_target" / "old.py"
    new = tmp_path / "bayes_basket_target" / "sub" / "new.py"
    worker = tmp_path / "scripts" / "weather_maintenance_worker.py"
    for path in (old, new, worker):
        path.write_text("x = 1\n")
    os.utime(old, (1000.0, 1000.0))
    os.utime(worker, (1000.0, 1000.0))
    os.utime(new, (3000.0, 3000.0))
    assert script.code_newer_than(2000.0, root=tmp_path) == ["bayes_basket_target/sub/new.py"]
    assert script.code_newer_than(3000.0, root=tmp_path) == ["bayes_basket_target/sub/new.py"]
    assert script.code_newer_than(3001.0, root=tmp_path) == []


def test_a_worker_newer_than_its_resident_beats_an_empty_claim_until_the_resident_goes(tmp_path) -> None:
    script = _script()
    hb = tmp_path / "hb.json"
    parents = iter([4242, 4242, 1])  # the resident goes after two beats
    beats: list[dict] = []
    newer = script.stand_down_on_code_skew(
        4242,
        heartbeat_path=hb,
        started_epoch=lambda pid: 1000.0,
        newer_than=lambda since: ["bayes_basket_target/native_ensemble_oos.py"],
        sleep=lambda _s: beats.append(json.loads(hb.read_text())),
        parent_now=lambda: next(parents),
    )
    assert newer == ["bayes_basket_target/native_ensemble_oos.py"]
    assert len(beats) == 2
    assert all(b["state"] == "code_skew" and b["lanes"] == [] for b in beats)
    # the cycle reads that claim as nobody serving the lane, and runs it itself
    verdict = pw.maintenance_worker_alive(path=hb)
    assert verdict["alive"] is False and verdict["reason"] in {"lane_not_served", "worker_gone"}


@pytest.mark.parametrize("parent,started,newer", [
    (None, 1000.0, ["x.py"]),   # run by hand: nothing to compare with
    (4242, None, ["x.py"]),     # the resident's start cannot be told
    (4242, 1000.0, []),         # same code
])
def test_otherwise_the_worker_runs(tmp_path, parent, started, newer) -> None:
    script = _script()
    assert script.stand_down_on_code_skew(
        parent,
        heartbeat_path=tmp_path / "hb.json",
        started_epoch=lambda pid: started,
        newer_than=lambda since: newer,
        sleep=lambda _s: pytest.fail("must not stand down"),
        parent_now=lambda: parent,
    ) == []
    assert not (tmp_path / "hb.json").exists()


def test_the_resident_start_is_read_from_the_process_table() -> None:
    started = _script().process_started_epoch(os.getpid())
    assert started is not None and started <= time.time()
