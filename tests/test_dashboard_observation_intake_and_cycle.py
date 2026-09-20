"""The board reads the observation door and the cycle journal (2026-09-20).

User: 「检查前端是否还有旧的内容，以及哪些新内容没有展示的」 -> 「做」.

Charter 162 gave the observation lane a second door (NOAA TGFTP) for when
every AWC channel fails at once, which is what happened when
aviationweather.gov's certificate expired at 2026-09-19T23:59:59Z.  Charters
153 and 154 put TAF and observations at the front of the production cycle to
cut the wait between a report landing and the probability moving.  None of
the three had anything on the page: the only sign of the outage was a
freshness count inside the station-day table, and the cycle's own journal --
per-stage milliseconds for every cycle -- had no reader at all.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _archive(tmp_path: Path, rows: list[tuple]) -> None:
    db = sqlite3.connect(tmp_path / "archive.db")
    db.execute(
        "CREATE TABLE reported_trajectory_observations (observation_id TEXT,"
        " source_lane TEXT, station_id TEXT, report_type TEXT, valid_at TEXT,"
        " canonical_committed_at TEXT)"
    )
    db.execute(
        "CREATE TABLE reported_trajectory_observation_origins (observation_id TEXT,"
        " source_lane TEXT, station_id TEXT, origin_source_id TEXT,"
        " origin_transport TEXT, fallback_used INTEGER, fallback_reason TEXT)"
    )
    for oid, station, valid, committed, origin in rows:
        db.execute(
            "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?,?,?)",
            (oid, "awc", station, "METAR", valid, committed),
        )
        db.execute(
            "INSERT INTO reported_trajectory_observation_origins VALUES (?,?,?,?,?,?,?)",
            (oid, "awc", station, origin, "t", 0, ""),
        )
    db.commit()
    db.close()


def _ssot(tmp_path: Path, stations: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(
        "CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT,"
        " stage TEXT, state TEXT, decision_at_utc TEXT, lineage_json TEXT)"
    )
    for station in stations:
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?)",
            (station, "2026-09-20", "P1", "COMMITTED", "2026-09-20T03:00:00Z", "{}"),
        )
    conn.commit()
    return conn


def test_the_lamp_reads_the_door_not_the_lane(tmp_path, monkeypatch) -> None:
    """Charter 162 keeps ``source_lane='awc'`` when TGFTP serves -- the lane
    is the station's roster binding, not the transport -- so a lamp that read
    the lane would show nothing at all during the outage."""

    monkeypatch.setattr(C, "ROOT", tmp_path)
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 3, 30, tzinfo=C.timezone.utc))
    (tmp_path / "data").mkdir()
    _archive(tmp_path / "data", [
        ("o1", "ZUUU", "2026-09-20T03:00:00Z", "2026-09-20T03:03:00Z", "global_noaa_awc_relay"),
        ("o2", "KLAX", "2026-09-20T03:00:00Z", "2026-09-20T03:02:00Z", "global_noaa_tgftp_relay"),
    ])
    out = C.collect_observation_intake(_ssot(tmp_path, ["ZUUU", "KLAX"]))
    assert out["available"] is True
    assert out["doors"] == {"AWC": 1, "NOAA TGFTP": 1}
    assert out["fallback"] == 1
    assert out["health"] == "amber", "a station on the second door is not a green fleet"
    assert out["oldest_minutes"] == pytest.approx(30.0)
    assert out["median_landing_minutes"] == pytest.approx(2.5)


def test_a_fleet_that_stopped_reporting_goes_red(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "ROOT", tmp_path)
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 8, 0, tzinfo=C.timezone.utc))
    (tmp_path / "data").mkdir()
    _archive(tmp_path / "data", [
        ("o1", "ZUUU", "2026-09-20T03:00:00Z", "2026-09-20T03:03:00Z", "global_noaa_awc_relay"),
        ("o2", "KLAX", "2026-09-20T03:00:00Z", "2026-09-20T03:02:00Z", "global_noaa_awc_relay"),
    ])
    out = C.collect_observation_intake(_ssot(tmp_path, ["ZUUU", "KLAX"]))
    assert out["stale"] == 2 and out["health"] == "red", out


def test_the_cycle_journal_is_read_and_the_worst_cycle_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 3, 30, tzinfo=C.timezone.utc))
    path = tmp_path / "production_cycle_history.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"completed_at_utc": "2026-09-20T03:00:00Z", "cycle_total_ms": 200_000,
         "state": "cycle_committed", "waited_sec": 0.0,
         "stages_ms": {"early_p0m": 10_000, "decision_loop": 40_000}, "top_spans_ms": {}},
        {"completed_at_utc": "2026-09-20T03:20:00Z", "cycle_total_ms": 1_600_000,
         "state": "cycle_committed_partial", "waited_sec": 5.0,
         "wait_blocked_by": ["sweep:settlement_truth"],
         "stages_ms": {"early_p0m": 700_000, "decision_loop": 50_000},
         "top_spans_ms": {"p0m_taf_fact_ingress": 690_000}},
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(C, "CYCLE_HISTORY_PATH", path)
    monkeypatch.setattr(C, "_report_to_probability_latency", lambda conn: {})
    out = C.collect_cycle_time(sqlite3.connect(":memory:"))
    assert out["available"] is True
    cycles = out["cycles"]
    assert cycles["n"] == 2 and cycles["max_sec"] == 1600.0 and cycles["last_sec"] == 1600.0
    assert cycles["partial"] == 1
    assert cycles["wait_blocked_by"] == {"sweep:settlement_truth": 1}
    # The stage that ate the cycle is the one the panel leads with.
    assert out["stages"][0]["stage"] == "early_p0m"
    assert out["stages"][0]["label"] == "早 P0-M（快车道）"
    assert out["worst"]["sec"] == 1600.0
    assert out["worst"]["spans"][0] == {"span": "p0m_taf_fact_ingress", "sec": 690.0}


def _latency_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(
        "CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT,"
        " stage TEXT, state TEXT, decision_at_utc TEXT, created_at TEXT,"
        " lineage_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_forward_taf_capture (station_id TEXT, event_date TEXT,"
        " received_at_utc TEXT, tx_available INTEGER)"
    )
    return conn


def test_a_taf_with_no_tx_is_not_a_latency_anybody_is_cutting(tmp_path, monkeypatch) -> None:
    """A capture with no TX leaves P0-M's likelihood identity, so nothing has
    to run promptly for it.  Pooling the two reported a delay that was mostly
    "nothing needed doing" (2026-09-20: 86 minutes against 19)."""

    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 3, 0, tzinfo=C.timezone.utc))
    conn = _latency_db(tmp_path)
    for tx, received in ((1, "2026-09-20T02:00:00Z"), (0, "2026-09-20T00:30:00Z")):
        conn.execute(
            "INSERT INTO calibration_forward_taf_capture VALUES ('ZUUU','2026-09-20',?,?)",
            (received, tx),
        )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES"
        " ('ZUUU','2026-09-20','P0-M','COMMITTED','2026-09-20T02:00:00Z',"
        "  '2026-09-20T02:10:00Z','{}')"
    )
    conn.commit()
    out = C._report_to_probability_latency(conn)
    assert out["taf_captures_without_tx"] == 1, "the no-TX ones are counted, not measured"
    assert out["taf_to_p0m"]["n"] == 1
    assert out["taf_to_p0m"]["median_min"] == pytest.approx(10.0), (
        "the 90-minute no-TX capture must not be in the median"
    )


def test_the_latency_reads_the_commit_clock_not_the_decision_clock(tmp_path, monkeypatch) -> None:
    """``decision_at_utc`` is not when the envelope was written.

    P0-M stamps the instant of the evidence it lays down -- for a TAF, the
    capture's own receipt -- so the P0-M that seated a TAF is never strictly
    after it, and a pairing on ``decision_at_utc > received_at`` skips it and
    lands on the next cycle's work.  On 2026-09-20 that reported 25.6
    minutes where the commit clock says 2.5, and sent me looking for a
    regression that was not there.  P1 has the same trap from the other
    side: its decision clock is the cycle's start, shared by every step of a
    replayed prefix.
    """

    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 9, 20, 3, 0, tzinfo=C.timezone.utc))
    conn = _latency_db(tmp_path)
    conn.execute(
        "INSERT INTO calibration_forward_taf_capture VALUES"
        " ('ZUUU','2026-09-20','2026-09-20T02:00:00Z',1)"
    )
    # The P0-M that seated it: decision clock == the capture's instant,
    # committed three minutes later.  Then the next cycle's, two hours on.
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES"
        " ('ZUUU','2026-09-20','P0-M','COMMITTED','2026-09-20T02:00:00Z',"
        "  '2026-09-20T02:03:00Z','{}')"
    )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES"
        " ('ZUUU','2026-09-20','P0-M','COMMITTED','2026-09-20T04:00:00Z',"
        "  '2026-09-20T04:02:00Z','{}')"
    )
    # One observation, read first by a cycle that started before it landed
    # (the replay prefix shares the cycle's clock) and committed after.
    lineage = json.dumps({
        "canonical_observation_committed_at": "2026-09-20T02:30:00Z",
        "canonical_observation_sha256": "a" * 64,
    })
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES"
        " ('ZUUU','2026-09-20','P1','COMMITTED','2026-09-20T02:29:00Z',"
        "  '2026-09-20T02:34:00Z',?)", (lineage,)
    )
    conn.commit()
    out = C._report_to_probability_latency(conn)
    assert out["taf_to_p0m"]["median_min"] == pytest.approx(3.0), (
        "the P0-M that seated the TAF is the answer, not the next cycle's"
    )
    assert out["metar_to_p1"]["median_min"] == pytest.approx(4.0), (
        "a cycle that started before the report landed still took four "
        "minutes to write the P1 -- a decision-clock reading would be "
        "negative and get dropped"
    )


def test_the_panel_and_the_lamp_are_wired() -> None:
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert '"observation_intake": observation_intake,' in source
    assert '"cycle_time": cycle_time,' in source
    assert "function cycleHealth(){" in source
    # The panel rides with the machine row, which is where "is the machine
    # turning" is asked.
    assert "O.banner + rendered[0] + cycle + sweeps" in source
    assert 'push("upstream", OB.health || "muted",' in source
