"""The fast lane (user, 2026-09-19: 「把taf和metar的抓取入库和bayes更新优先级提高最高」).

A TAF the capture round landed became P0-M ~150s into the next cycle, behind
the intent reconcile (49s median) and the NWP stages; a report's P1 waited on
the reconcile too.  Now the head of the cycle runs P0-M for a moved TAF inbox
and then P1, before the reconcile; a P0-M that committed forces the head P1 so
P1 re-roots on it at once.
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import bayes_basket_target.production_worker as pw

CLOCK = "2026-09-19T02:00:00Z"
M1, M2 = (1, 10), (2, 11)


def _worker(tmp_path=None) -> pw.TargetProductionWorker:
    worker = object.__new__(pw.TargetProductionWorker)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope(station_id TEXT, event_date TEXT, stage TEXT, state TEXT)")
    conn.commit()
    worker.commit_conn = conn
    worker._stage_timings_ms = {}
    if tmp_path is not None:
        worker.taf_inbox_path = tmp_path / "taf_inbox.json"
    return worker


def test_the_head_p0m_stage_is_a_decision_tier_stage() -> None:
    assert pw.STAGE_TIER["early_p0m"] == pw.STAGE_TIER_R0_DECISION


def test_head_p0m_skips_when_no_taf_landed(monkeypatch) -> None:
    worker = _worker()
    worker._p0m_covered_taf_watermark = M1
    monkeypatch.setattr(worker, "_materialize_resident_p0m", lambda **k: pytest.fail("must not run"))
    out = worker._materialize_early_p0m(as_of_utc=CLOCK, taf_watermark=M1)
    assert out["state"] == "SKIPPED_NO_NEW_TAF" and out["ran"] is False


def test_head_p0m_is_off_on_the_switch(monkeypatch) -> None:
    monkeypatch.setattr(pw, "EARLY_P0M_ENABLED", False)
    out = _worker()._materialize_early_p0m(as_of_utc=CLOCK, taf_watermark=M2)
    assert out["state"] == "DISABLED"


def test_head_p0m_runs_on_a_moved_inbox_and_says_whether_it_wrote(monkeypatch) -> None:
    worker = _worker()
    worker._p0m_covered_taf_watermark = M1
    ceilings = iter([100, 103])
    monkeypatch.setattr(worker, "_envelope_rowid_ceiling", lambda: next(ceilings))
    seen: list = []
    monkeypatch.setattr(worker, "_materialize_resident_p0m", lambda *, as_of_utc: seen.append(as_of_utc) or {"state": "COMMITTED"})
    out = worker._materialize_early_p0m(as_of_utc=CLOCK, taf_watermark=M2)
    assert out["ran"] is True and out["new_envelopes"] is True and seen == [CLOCK]
    assert worker._p0m_covered_taf_watermark == M2


def test_a_blocked_or_failed_head_p0m_covers_nothing(monkeypatch) -> None:
    worker = _worker()
    worker._p0m_covered_taf_watermark = M1
    monkeypatch.setattr(worker, "_envelope_rowid_ceiling", lambda: 100)
    monkeypatch.setattr(worker, "_materialize_resident_p0m", lambda *, as_of_utc: {"state": "BLOCKED_FAIL_CLOSED"})
    out = worker._materialize_early_p0m(as_of_utc=CLOCK, taf_watermark=M2)
    assert out["new_envelopes"] is False and worker._p0m_covered_taf_watermark == M1

    def boom(*, as_of_utc):
        raise RuntimeError("inbox torn")
    monkeypatch.setattr(worker, "_materialize_resident_p0m", boom)
    out = worker._materialize_early_p0m(as_of_utc=CLOCK, taf_watermark=M2)
    assert out["state"] == "FAILED" and worker._p0m_covered_taf_watermark == M1


def test_a_committed_head_p0m_forces_the_head_p1(monkeypatch) -> None:
    worker = _worker()
    worker._p1_covered_watermark = (5, 0, "2026-09-19")
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: {"state": "SKIPPED_NO_NEW_REPORTS"})
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda *, as_of_utc: {"state": "COMMITTED", "p1_update_count": 3})
    skipped = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=(5, 0, "2026-09-19"))
    assert skipped["state"] == "SKIPPED_NO_UNCOVERED_REPORTS"
    forced = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=(5, 0, "2026-09-19"), force=True)
    assert forced["ran"] is True


def test_the_chains_p0m_records_the_inbox_it_covered(monkeypatch, tmp_path) -> None:
    worker = _worker(tmp_path)
    worker.taf_inbox_path.write_text("{}")
    worker._early_p1_this_cycle = {"ran": False}
    monkeypatch.setattr(worker, "materialize_resident_p0", lambda *, as_of_utc: {"state": "COMMITTED"})
    monkeypatch.setattr(worker, "_materialize_resident_p0m", lambda *, as_of_utc: {"state": "COMMITTED"})
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **k: {"state": "COMMITTED", "receipts": [], "blockers": []})
    worker._materialize_resident_probability_chain(as_of_utc=CLOCK)
    assert worker._p0m_covered_taf_watermark == pw._taf_inbox_watermark(worker.taf_inbox_path)


def test_the_covered_report_mark_is_the_head_p1s_own_read() -> None:
    early, cycle = (5, 0, "d"), (7, 0, "d")
    assert pw._covered_report_watermark(early_p1_ran=True, early_watermark=early, cycle_watermark=cycle) == early
    assert pw._covered_report_watermark(early_p1_ran=False, early_watermark=early, cycle_watermark=cycle) == cycle


def test_the_fast_lane_runs_before_the_intent_reconcile() -> None:
    """Order inside the cycle method: head P0-M, head P1, then the reconcile."""

    tree = ast.parse(Path(pw.__file__).read_text())
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        calls = {}
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.setdefault(node.func.attr, node.lineno)
        if "_reconcile_unresolved_intents" in calls and "_materialize_early_p1" in calls:
            assert calls["_materialize_early_p0m"] < calls["_materialize_early_p1"] < calls["_reconcile_unresolved_intents"]
            return
    pytest.fail("the cycle method was not found")
