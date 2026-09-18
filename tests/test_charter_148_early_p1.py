"""Charter 148 (2026-09-18): a report's P1 is written at the head of the cycle.

Before, P1 ran inside the chain, behind the inbox ingest, the P0 replay
compile and the OOS stages, so a report that landed during one cycle reached
P1 a median 2.1 min (p90 5.7) into the next.  The head pass runs identity and
P1 right after registry ingress when the archive holds a report no P1 has
covered; the chain then re-runs P1 only for scopes whose root it rebuilt.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import bayes_basket_target.production_worker as pw

W1 = (100, 0, "2026-09-18")
W2 = (101, 0, "2026-09-18")
CLOCK = "2026-09-18T06:00:00Z"


def _worker() -> pw.TargetProductionWorker:
    worker = object.__new__(pw.TargetProductionWorker)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.execute("INSERT INTO basket_probability_envelope VALUES ('ZUUU','2026-09-18','P1','COMMITTED')")
    conn.commit()
    worker.commit_conn = conn
    worker._stage_timings_ms = {}
    return worker


def _p1_receipt(scopes: list[tuple[str, str]], *, blockers: list | None = None) -> dict[str, Any]:
    receipts = [
        {"station_id": s, "event_date": d, "state": "COMMITTED", "p1_update_count": 1}
        for s, d in scopes
    ]
    return {
        "schema": "bayes_basket_target_resident_p1_ingress_v1",
        "state": "PARTIAL_FAIL_CLOSED" if blockers and receipts else ("BLOCKED_FAIL_CLOSED" if blockers else "COMMITTED"),
        "receipts": receipts,
        "blockers": list(blockers or []),
        "p1_update_count": len(receipts),
    }


# ---- the head pass --------------------------------------------------------

def test_the_head_stage_is_a_decision_tier_stage() -> None:
    assert pw.STAGE_TIER["early_p1"] == pw.STAGE_TIER_R0_DECISION


def test_head_pass_skips_when_no_report_is_uncovered(monkeypatch) -> None:
    worker = _worker()
    worker._p1_covered_watermark = W1
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: pytest.fail("identity ran"))
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **_k: pytest.fail("P1 ran"))
    out = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=W1)
    assert out["ran"] is False and out["state"] == "SKIPPED_NO_UNCOVERED_REPORTS"


def test_head_pass_skips_when_the_archive_cannot_be_asked(monkeypatch) -> None:
    worker = _worker()
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **_k: pytest.fail("P1 ran"))
    out = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=None)
    assert out["ran"] is False and out["state"] == "SKIPPED_WATERMARK_UNREADABLE"


def test_head_pass_is_off_on_the_switch(monkeypatch) -> None:
    worker = _worker()
    monkeypatch.setattr(pw, "EARLY_P1_ENABLED", False)
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **_k: pytest.fail("P1 ran"))
    assert worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=W2)["state"] == "DISABLED"


def test_head_pass_waits_for_the_identity_contract(monkeypatch) -> None:
    worker = _worker()
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: {"state": "DEADLINE_EXCEEDED"})
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **_k: pytest.fail("blind P1 written"))
    out = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=W2)
    assert out["ran"] is False and out["state"] == "SKIPPED_IDENTITY_NOT_STAGED"


@pytest.mark.parametrize("identity_state", ["STAGED", "SKIPPED_NO_NEW_REPORTS"])
def test_head_pass_runs_p1_on_the_cycle_clock(monkeypatch, identity_state: str) -> None:
    worker = _worker()
    worker._p1_covered_watermark = W1
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: {"state": identity_state})
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **k: calls.append(k) or _p1_receipt([("ZUUU", "2026-09-18")]))
    out = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=W2)
    assert out["ran"] is True and out["state"] == "RAN"
    # the head pass reads roots as of the cycle clock and restricts nothing
    assert calls == [{"as_of_utc": CLOCK}]


def test_head_pass_failure_falls_back_to_the_chain(monkeypatch) -> None:
    worker = _worker()
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: {"state": "STAGED"})

    def boom(**_k):
        raise RuntimeError("archive gone")

    monkeypatch.setattr(worker, "_materialize_resident_p1", boom)
    out = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=W2)
    assert out["ran"] is False and out["state"] == "FAILED"


def test_head_pass_that_could_not_attempt_p1_does_not_count(monkeypatch) -> None:
    worker = _worker()
    monkeypatch.setattr(worker, "_identify_live_observations", lambda: {"state": "STAGED"})
    monkeypatch.setattr(worker, "_materialize_resident_p1", lambda **_k: {"state": "not_configured"})
    out = worker._materialize_early_p1(as_of_utc=CLOCK, report_watermark=W2)
    assert out["ran"] is False and out["state"] == "SKIPPED_P1_NOT_ATTEMPTED"


# ---- the chain's second pass ---------------------------------------------

def _wire_chain(monkeypatch, worker, *, new_roots: list[tuple[str, str, str]]):
    calls: list[str] = []
    p1_calls: list[dict[str, Any]] = []

    def p0(*, as_of_utc):
        calls.append("P0")
        for station, day, stage in new_roots:
            worker.commit_conn.execute(
                "INSERT INTO basket_probability_envelope VALUES (?,?,?,'COMMITTED')",
                (station, day, stage),
            )
        return {"state": "COMMITTED"}

    monkeypatch.setattr(worker, "materialize_resident_p0", p0)
    monkeypatch.setattr(worker, "_materialize_resident_p0m", lambda *, as_of_utc: calls.append("P0-M") or {"state": "COMMITTED", "stage_clock_utc": "2026-09-18T06:00:00.001Z"})

    def p1(**kwargs):
        calls.append("P1")
        p1_calls.append(kwargs)
        return _p1_receipt(sorted(kwargs.get("only_scopes") or []))

    monkeypatch.setattr(worker, "_materialize_resident_p1", p1)
    return calls, p1_calls


def test_without_a_head_pass_the_chain_runs_p1_once_as_before(monkeypatch) -> None:
    worker = _worker()
    worker._early_p1_this_cycle = {"state": "SKIPPED_NO_UNCOVERED_REPORTS", "ran": False}
    calls, p1_calls = _wire_chain(monkeypatch, worker, new_roots=[])
    out = worker._materialize_resident_probability_chain(as_of_utc=CLOCK)
    assert calls == ["P0", "P0-M", "P1"]
    assert p1_calls == [{"as_of_utc": CLOCK, "stage_clock_utc": "2026-09-18T06:00:00.001Z"}]
    assert out["stage_order"] == ["P0", "P0-M", "P1"]


def test_after_a_head_pass_the_chain_skips_p1_when_no_root_was_rebuilt(monkeypatch) -> None:
    worker = _worker()
    head = _p1_receipt([("ZUUU", "2026-09-18"), ("ZUCK", "2026-09-18")])
    worker._early_p1_this_cycle = {"state": "RAN", "ran": True, "p1": head}
    calls, p1_calls = _wire_chain(monkeypatch, worker, new_roots=[])
    out = worker._materialize_resident_probability_chain(as_of_utc=CLOCK)
    assert calls == ["P0", "P0-M"]
    assert p1_calls == []
    assert out["p1"]["receipts"] == head["receipts"]
    assert out["p1"]["late_pass_scopes"] == []
    assert out["p1"]["state"] == "COMMITTED"


def test_after_a_head_pass_the_chain_reroots_only_what_it_rebuilt(monkeypatch) -> None:
    worker = _worker()
    head = _p1_receipt([("ZUUU", "2026-09-18"), ("ZUCK", "2026-09-18")])
    worker._early_p1_this_cycle = {"state": "RAN", "ran": True, "p1": head}
    calls, p1_calls = _wire_chain(
        monkeypatch, worker,
        new_roots=[("ZUUU", "2026-09-18", "P0"), ("ZUUU", "2026-09-18", "P0-M"), ("KATL", "2026-09-19", "P0-M"), ("RJTT", "2026-09-18", "P1")],
    )
    out = worker._materialize_resident_probability_chain(as_of_utc=CLOCK)
    assert calls == ["P0", "P0-M", "P1"]
    assert len(p1_calls) == 1
    assert p1_calls[0]["only_scopes"] == {("ZUUU", "2026-09-18"), ("KATL", "2026-09-19")}
    assert p1_calls[0]["stage_clock_utc"] == "2026-09-18T06:00:00.001Z"
    assert out["p1"]["late_pass_scopes"] == ["KATL|2026-09-19", "ZUUU|2026-09-18"]
    assert len(out["p1"]["receipts"]) == 4


def test_merged_receipt_keeps_every_blocker_and_its_state() -> None:
    head = _p1_receipt([("ZUUU", "2026-09-18")], blockers=[{"station_id": "RKPK", "event_date": "2026-09-18", "reason": "x"}])
    late = _p1_receipt([], blockers=[{"station_id": "KATL", "event_date": "2026-09-19", "reason": "y"}])
    merged = pw._merge_resident_p1_receipts(head, late, late_scopes=[("KATL", "2026-09-19")])
    assert merged["state"] == "PARTIAL_FAIL_CLOSED"
    assert [b["station_id"] for b in merged["blockers"]] == ["RKPK", "KATL"]
    only_blocked = pw._merge_resident_p1_receipts(
        _p1_receipt([], blockers=[{"station_id": "RKPK", "event_date": "2026-09-18", "reason": "x"}]), None, late_scopes=[]
    )
    assert only_blocked["state"] == "BLOCKED_FAIL_CLOSED"


def test_rooted_after_reads_only_committed_p0_and_p0m_past_the_mark() -> None:
    worker = _worker()
    mark = worker._envelope_rowid_ceiling()
    worker.commit_conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?)",
        [("zuuu", "2026-09-18", "P0-M", "COMMITTED"), ("KATL", "2026-09-19", "P0", "QUARANTINED"), ("RJTT", "2026-09-18", "P1", "COMMITTED")],
    )
    assert worker._scopes_rooted_after(mark) == [("ZUUU", "2026-09-18")]
    assert worker._scopes_rooted_after(None) == []


def test_the_real_p1_pass_restricts_to_the_scopes_it_is_handed(monkeypatch) -> None:
    """``only_scopes`` narrows the real materializer's loop, nothing else."""

    worker = object.__new__(pw.TargetProductionWorker)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT, decision_at_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?)",
        [("ZUUU", "2026-09-18", "P0-M", "COMMITTED", "2026-09-18T05:00:00Z"),
         ("ZUCK", "2026-09-18", "P0-M", "COMMITTED", "2026-09-18T05:00:00Z")],
    )
    worker.commit_conn = conn
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    worker._stage_spans = {}
    monkeypatch.setattr(worker, "_preclear_pending_corrections", lambda **_k: {})
    monkeypatch.setattr(worker, "_span", lambda *_a, **_k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(
        pw, "load_started_registry_rows",
        lambda *_a, **_k: [
            {"station_id": s, "event_date": "2026-09-18", "settlement_timezone": "Asia/Shanghai", "source_lane": "awc"}
            for s in ("ZUUU", "ZUCK")
        ],
    )
    read: list[str] = []

    def stop(_conn, *, station_id, event_date):
        read.append(station_id)
        raise RuntimeError("stop here")

    monkeypatch.setattr(pw, "target_registry_source_lane", stop)
    full = worker._materialize_resident_p1(as_of_utc=CLOCK)
    assert sorted(read) == ["ZUCK", "ZUUU"] and full["scope_count"] == 2
    read.clear()
    narrowed = worker._materialize_resident_p1(as_of_utc=CLOCK, only_scopes={("zuuu", "2026-09-18")})
    assert read == ["ZUUU"] and narrowed["scope_count"] == 1
    read.clear()
    none = worker._materialize_resident_p1(as_of_utc=CLOCK, only_scopes=set())
    assert read == [] and none["scope_count"] == 0
