"""Mid-cycle fast-lane checkpoints (user, 2026-09-19: 「极限压低信息到来到bayes概率更新的延迟」).

Right before the decision loop the head passes run again for what landed
since, and only for it: P1 for the
stations with new reports and the scopes a new P0-M re-rooted; P0-M only once
the chain has settled the roots.  (A checkpoint right after the P0 replay
compile did the chain's re-root work twice: 340s of a 1250s cycle.)
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw

MARK = (10, 0, "2026-09-19")


def _worker(tmp_path, *, chain_ran=True):
    worker = object.__new__(pw.TargetProductionWorker)
    worker._stage_timings_ms = {}
    worker.taf_inbox_path = tmp_path / "taf_inbox.json"
    worker.taf_inbox_path.write_bytes(b"{}")
    worker.canonical_source_conn = object()
    worker._probability_chain_ran = chain_ran
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
    worker._p1_covered_watermark = MARK
    return worker


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(pw, "_live_observation_watermark", lambda conn: _env.mark)
    _env.mark = MARK
    monkeypatch.setattr(pw, "utc_now", lambda: "2026-09-19T03:00:00Z")
    monkeypatch.setattr(pw.TargetProductionWorker, "_mark",
                        lambda self, name: self._stage_timings_ms.__setitem__(name, 1.0), raising=False)


def test_nothing_new_costs_nothing(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("no P0-M"))
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: pytest.fail("no P1"))
    assert worker._fast_lane_checkpoint("before_decisions") is None
    assert worker._stage_timings_ms == {}


def test_new_reports_run_p1_only_for_their_stations(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    _env.mark = (12, 0, "2026-09-19")
    monkeypatch.setattr(pw, "_stations_with_reports_after", lambda conn, covered: {"ZUUU"})
    seen = {}
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("TAF did not move"))
    monkeypatch.setattr(worker, "_materialize_early_p1",
                        lambda **k: seen.update(k) or {"ran": True, "p1": {"state": "COMMITTED"}})
    worker._fast_lane_checkpoint("before_decisions")
    assert {s for s, _d in seen["only_scopes"]} == {"ZUUU"}
    assert ("ZUUU", "2026-09-19") in seen["only_scopes"]
    assert worker._p1_covered_watermark == (12, 0, "2026-09-19")
    assert "fast_lane_before_decisions" in worker._stage_timings_ms


def test_a_new_taf_after_the_chain_reroots_only_what_it_wrote(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._p0m_covered_taf_watermark = ("old",)
    monkeypatch.setattr(worker, "_materialize_early_p0m",
                        lambda **k: {"ran": True, "new_envelopes": True, "rowid_before": 500})
    monkeypatch.setattr(worker, "_scopes_rooted_after", lambda mark: [("LTAC", "2026-09-19")] if mark == 500 else [])
    seen = {}
    monkeypatch.setattr(worker, "_materialize_early_p1",
                        lambda **k: seen.update(k) or {"ran": True, "p1": {"state": "COMMITTED"}})
    worker._fast_lane_checkpoint("before_decisions")
    assert seen["only_scopes"] == {("LTAC", "2026-09-19")} and seen["force"] is True
    # No new report: the covered report mark stays where it was.
    assert worker._p1_covered_watermark == MARK


def test_before_the_chain_a_new_taf_is_left_to_the_chain(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path, chain_ran=False)
    worker._p0m_covered_taf_watermark = ("old",)
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("the chain re-roots"))
    assert worker._fast_lane_checkpoint("before_decisions") is None


def test_unknown_stations_fall_back_to_the_full_pass(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    _env.mark = (12, 0, "2026-09-19")
    monkeypatch.setattr(pw, "_stations_with_reports_after", lambda conn, covered: None)
    seen = {}
    monkeypatch.setattr(worker, "_materialize_early_p1",
                        lambda **k: seen.update(k) or {"ran": True, "p1": {"state": "COMMITTED"}})
    worker._fast_lane_checkpoint("before_decisions")
    assert seen["only_scopes"] is None


def test_a_blocked_p1_covers_nothing(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    _env.mark = (12, 0, "2026-09-19")
    monkeypatch.setattr(pw, "_stations_with_reports_after", lambda conn, covered: {"ZUUU"})
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: {"ran": True, "p1": {"state": "BLOCKED_FAIL_CLOSED"}})
    worker._fast_lane_checkpoint("before_decisions")
    assert worker._p1_covered_watermark == MARK


def test_the_stations_query_reads_both_row_families() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reported_trajectory_observations(observation_id TEXT, source_lane TEXT, station_id TEXT)")
    conn.executemany("INSERT INTO reported_trajectory_observations VALUES (?,?,?)", [
        ("a1", "awc", "KLAX"), ("a2", "awc", "ZUUU"), ("asos_p1_x", "asos", "KATL"), ("m1", "madis_hfmetar", "EGLC"),
    ])
    assert pw._stations_with_reports_after(conn, (1, 2, "d")) == {"ZUUU", "KATL"}
    assert pw._stations_with_reports_after(conn, None) is None
    assert pw._stations_with_reports_after(object(), (1, 2, "d")) is None


def test_the_checkpoints_are_off_on_the_switch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pw, "FAST_LANE_CHECKPOINTS_ENABLED", False)
    _env.mark = (99, 0, "2026-09-19")
    assert _worker(tmp_path)._fast_lane_checkpoint("before_decisions") is None


def test_a_covered_mark_never_moves_back() -> None:
    assert pw._later_report_watermark((12, 3, "d"), (10, 9, "d")) == (12, 3, "d")
    assert pw._later_report_watermark((10, 0, "d"), (12, 0, "d")) == (12, 0, "d")
    assert pw._later_report_watermark(None, (1, 0, "d")) == (1, 0, "d")


def test_the_checkpoints_sit_after_the_chain_with_a_tier() -> None:
    tree = ast.parse(Path(pw.__file__).read_text())
    labels = sorted(
        (node.lineno, node.args[0].value) for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_fast_lane_checkpoint" and node.args
        and isinstance(node.args[0], ast.Constant)
    )
    assert [label for _l, label in labels] == ["before_decisions"]
    chain_line = min(node.lineno for node in ast.walk(tree)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "_materialize_resident_probability_chain")
    assert all(line > chain_line for line, _ in labels)
    for _l, label in labels:
        assert pw.STAGE_TIER[f"fast_lane_{label}"] == pw.STAGE_TIER_R0_DECISION


def test_the_p1_scope_check_is_a_per_pair_indexed_read() -> None:
    """The full-table DISTINCT (4.2s on every P1 pass) is gone."""

    source = Path(pw.__file__).read_text()
    assert "SELECT DISTINCT station_id,event_date \"\n                \"FROM basket_probability_envelope" not in source
    assert "WHERE station_id=? AND event_date=? AND state='COMMITTED' " in source


# --- Charter 165: the checkpoint re-armed between the heavy stages ----------


def test_the_re_armed_checkpoint_runs_at_most_once_per_interval(monkeypatch, tmp_path) -> None:
    """Charter 165: a check every interval, not every stage."""

    worker = _worker(tmp_path)
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        worker, "_fast_lane_checkpoint",
        lambda label, mark=True: calls.append((label, mark)) or {"label": label},
    )
    clock = [1000.0]
    monkeypatch.setattr(pw.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pw, "FAST_LANE_CHECKPOINT_MIN_INTERVAL_SEC", 45.0)
    worker._last_fast_lane_checkpoint_mono = 1000.0
    assert worker._fast_lane_checkpoint_if_due("after_regime") is None
    clock[0] = 1044.0
    assert worker._fast_lane_checkpoint_if_due("after_regime") is None
    clock[0] = 1045.0
    assert worker._fast_lane_checkpoint_if_due("after_regime") == {"label": "after_regime"}
    assert calls == [("after_regime", True)]
    # No stamp yet (first check of a process): it runs.
    del worker._last_fast_lane_checkpoint_mono
    assert worker._fast_lane_checkpoint_if_due("in_decision_loop", mark=False) == {
        "label": "in_decision_loop"
    }
    assert calls[-1] == ("in_decision_loop", False)


def test_a_check_that_found_nothing_still_stamps_the_clock(monkeypatch, tmp_path) -> None:
    """The watermark read is the cost being rationed, so it counts."""

    worker = _worker(tmp_path)
    monkeypatch.setattr(pw.time, "monotonic", lambda: 777.0)
    monkeypatch.setattr(worker, "_materialize_early_p1", lambda **k: pytest.fail("no P1"))
    assert worker._fast_lane_checkpoint("after_regime") is None
    assert worker._last_fast_lane_checkpoint_mono == 777.0


def test_inside_the_decision_loop_the_pass_is_a_span_not_a_mark(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    _env.mark = (12, 0, "2026-09-19")
    monkeypatch.setattr(pw, "_stations_with_reports_after", lambda conn, covered: {"ZUUU"})
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: pytest.fail("TAF did not move"))
    monkeypatch.setattr(worker, "_materialize_early_p1",
                        lambda **k: {"ran": True, "p1": {"state": "COMMITTED"}})
    spans: list[tuple[str, float]] = []
    monkeypatch.setattr(worker, "_add_span", lambda name, ms, **k: spans.append((name, ms)), raising=False)
    receipt = worker._fast_lane_checkpoint("in_decision_loop", mark=False)
    assert receipt["label"] == "in_decision_loop"
    assert "fast_lane_in_decision_loop" not in worker._stage_timings_ms
    assert [name for name, _ms in spans] == ["fast_lane_in_decision_loop"]
    assert worker._p1_covered_watermark == (12, 0, "2026-09-19")


def test_the_re_armed_checkpoints_sit_at_the_heavy_stage_boundaries_with_a_tier() -> None:
    tree = ast.parse(Path(pw.__file__).read_text())
    labels = sorted(
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_fast_lane_checkpoint_if_due" and node.args
        and isinstance(node.args[0], ast.Constant)
    )
    assert labels == sorted([
        "after_native_p0_replay_compile", "after_native_ensemble_oos", "after_regime",
        "after_forward_p0_loop", "after_market_ingress", "in_decision_loop",
    ])
    for label in labels:
        assert pw.STAGE_TIER[f"fast_lane_{label}"] == pw.STAGE_TIER_R0_DECISION
    # The direct call before decisions is untouched (charter 154 fix 2).
    direct = [
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_fast_lane_checkpoint" and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert direct == ["before_decisions"]


def test_a_costly_pass_buys_its_own_quiet(monkeypatch, tmp_path) -> None:
    """Charter 165's duty cycle: never re-arm more often than the last pass
    took.  Six checkpoints cost 403s of a 1366s cycle on the first deploy."""

    worker = _worker(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(pw.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pw, "FAST_LANE_CHECKPOINT_MIN_INTERVAL_SEC", 45.0)
    calls: list[str] = []
    monkeypatch.setattr(
        worker, "_fast_lane_checkpoint",
        lambda label, mark=True: calls.append(label) or {"label": label},
    )
    # A pass that cost four minutes is not re-armed forty-five seconds later.
    worker._last_fast_lane_checkpoint_mono = 1000.0
    worker._last_fast_lane_checkpoint_cost_sec = 240.0
    clock[0] = 1100.0
    assert worker._fast_lane_checkpoint_if_due("after_regime") is None
    clock[0] = 1241.0
    assert worker._fast_lane_checkpoint_if_due("after_regime") is not None
    # A cheap pass is governed by the interval, as before.
    worker._last_fast_lane_checkpoint_mono = clock[0]
    worker._last_fast_lane_checkpoint_cost_sec = 2.0
    clock[0] += 46.0
    assert worker._fast_lane_checkpoint_if_due("after_regime") is not None
    assert calls == ["after_regime", "after_regime"]


def test_the_checkpoint_stamps_what_it_cost(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    _env.mark = (12, 0, "2026-09-19")
    monkeypatch.setattr(pw, "_stations_with_reports_after", lambda conn, covered: {"ZUUU"})
    monkeypatch.setattr(worker, "_materialize_early_p0m", lambda **k: None)
    monkeypatch.setattr(worker, "_materialize_early_p1",
                        lambda **k: {"ran": True, "p1": {"state": "COMMITTED"}})
    ticks = iter([100.0, 137.5, 137.5, 137.5, 137.5])
    monkeypatch.setattr(pw.time, "perf_counter", lambda: next(ticks))
    worker._fast_lane_checkpoint("after_regime")
    assert worker._last_fast_lane_checkpoint_cost_sec == pytest.approx(37.5)
