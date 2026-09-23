"""At a TAF station P1 roots on P0-M, in the fast lane too (charter 150 (三)).

Charter 195's checkpoint mints a P0 at its clock, re-seats P0-M on it -- a
millisecond later, since a stage may not share its root's instant
(``_p0m_replay_decision_at``) -- and then runs P1.  From 2026-09-22 07:55Z
(ea8efd96) that P1 read its root as of the clock alone, did not see the P0-M,
and rooted on the bare P0: 1,265 envelopes over 279 branches in 09-22..23,
each holding its station-day at the decision gate until the next pass
re-rooted it (median 112 s, p90 56 min).  FACT 2026-09-22 11:30:45Z: the TAF
said 32C, P0-M put 0.35 on 33C, the P0-rooted P1 0.05.  The chain always
passed the P0-M's clock; the fast lane now does as well.
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.production_worker as pw

CLOCK = "2026-09-22T11:30:45.000Z"
P0M_CLOCK = "2026-09-22T11:30:45.001Z"
MARK = (10, 0, "2026-09-22")


def _worker() -> pw.TargetProductionWorker:
    worker = object.__new__(pw.TargetProductionWorker)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.commit()
    worker.commit_conn = conn
    worker._stage_timings_ms = {}
    return worker


def test_the_head_p0m_reports_the_clock_it_stamped(monkeypatch) -> None:
    worker = _worker()
    worker._p0m_covered_taf_watermark = (1, 10)
    ceilings = iter([100, 103])
    monkeypatch.setattr(worker, "_envelope_rowid_ceiling", lambda: next(ceilings))
    monkeypatch.setattr(
        worker, "_materialize_resident_p0m",
        lambda *, as_of_utc: {"state": "COMMITTED", "stage_clock_utc": P0M_CLOCK},
    )
    out = worker._materialize_early_p0m(as_of_utc=CLOCK, taf_watermark=(2, 11), force=True)
    assert out["ran"] is True and out["stage_clock_utc"] == P0M_CLOCK


@pytest.mark.parametrize("stage_clock", [P0M_CLOCK, None])
def test_the_head_p1_reads_its_root_as_of_the_stage_clock(monkeypatch, stage_clock) -> None:
    worker = _worker()
    calls: list[dict] = []
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: {"state": "STAGED"})
    monkeypatch.setattr(
        worker, "_materialize_resident_p1",
        lambda **k: calls.append(k) or {"state": "COMMITTED", "receipts": [], "blockers": []},
    )
    worker._materialize_early_p1(
        as_of_utc=CLOCK, report_watermark=MARK, force=True,
        only_scopes={("FACT", "2026-09-22")}, stage_clock_utc=stage_clock,
    )
    expected = {"as_of_utc": CLOCK, "only_scopes": {("FACT", "2026-09-22")}}
    if stage_clock:
        expected["stage_clock_utc"] = stage_clock
    assert calls == [expected]


def test_the_checkpoint_hands_the_p0m_clock_to_p1(monkeypatch, tmp_path) -> None:
    worker = _worker()
    worker.taf_inbox_path = tmp_path / "taf_inbox.json"
    worker.taf_inbox_path.write_bytes(b"{}")
    worker.canonical_source_conn = object()
    worker._probability_chain_ran = True
    worker._p0m_covered_taf_watermark = pw._taf_inbox_watermark(worker.taf_inbox_path)
    worker._p1_covered_watermark = MARK
    worker.nwp_runtime_root = tmp_path
    inbox = tmp_path / "official_native_ensemble_forward_inbox.json"
    inbox.write_bytes(b'{"packets": []}')
    worker._p0_covered_official_watermark = pw._official_inbox_watermark(inbox)
    inbox.write_bytes(b'{"packets": ["new run"]}')
    monkeypatch.setattr(pw, "_live_observation_watermark", lambda conn: MARK)
    monkeypatch.setattr(pw, "utc_now", lambda: CLOCK)
    monkeypatch.setattr(
        pw.TargetProductionWorker, "_mark",
        lambda self, name: self._stage_timings_ms.__setitem__(name, 1.0), raising=False,
    )
    monkeypatch.setattr(
        worker, "_fast_lane_official_p0",
        lambda **k: {"state": "RAN", "new_envelopes": True, "rowid_before": 700},
    )
    monkeypatch.setattr(
        worker, "_materialize_early_p0m",
        lambda **k: {"ran": True, "new_envelopes": True, "rowid_before": 710,
                     "stage_clock_utc": P0M_CLOCK},
    )
    monkeypatch.setattr(worker, "_scopes_rooted_after", lambda mark: [("FACT", "2026-09-22")])
    seen: dict = {}
    monkeypatch.setattr(
        worker, "_materialize_early_p1",
        lambda **k: seen.update(k) or {"ran": True, "p1": {"state": "COMMITTED"}},
    )
    worker._fast_lane_checkpoint("before_decisions")
    assert seen["as_of_utc"] == CLOCK
    assert seen["stage_clock_utc"] == P0M_CLOCK


def test_every_early_p1_call_passes_the_stage_clock() -> None:
    source = Path(pw.__file__).read_text()
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_materialize_early_p1"
    ]
    assert len(calls) >= 2
    for call in calls:
        assert "stage_clock_utc" in {kw.arg for kw in call.keywords}, call.lineno
