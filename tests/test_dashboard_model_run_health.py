"""模型 run 采集健康度 (user, 2026-10-01: 「前端突出模型run采集健康度的看不出来」).

* The recent runs are read every render, off the covering index, with the
  station count per target day and the arrival from the first row.
* They replace the paged cadence's newest runs, so the 发布时间 arrival is
  never the blank of a run the page did not have.
* 入围 lights a D+1 day where a family sits in fewer than nine in ten P0s,
  and a D+2 day where it sits in fewer than half -- amber there, never green.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as cd  # noqa: E402

NOW = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)
ICON = "DWD_ICON_GLOBAL_EPS"
ICON_T = "DWD_OPENDATA_ICON_EPS"


def _member_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_native_ensemble_historical_member_run("
        " ensemble_system_id TEXT, transport_provider_id TEXT,"
        " qc_status TEXT, target_local_date TEXT, station_id TEXT,"
        " native_run_init_utc TEXT, run_cycle TEXT, archive_lead_bucket TEXT,"
        " raw_tmax_c REAL, archive_query_at_utc TEXT);"
        "CREATE INDEX calibration_native_member_skill_idx ON"
        " calibration_native_ensemble_historical_member_run("
        " ensemble_system_id, transport_provider_id, qc_status,"
        " target_local_date, station_id, native_run_init_utc, run_cycle,"
        " archive_lead_bucket, raw_tmax_c);"
    )

    def rows(run, day, stations, at):
        for station in stations:
            conn.execute(
                "INSERT INTO calibration_native_ensemble_historical_member_run"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ICON, ICON_T, "ACCEPTED", day, station, run, run[11:13] + "Z",
                 "h24_48", 20.0, at),
            )

    # ICON 10-01 00Z read for 10-03 at 04:59Z for 4 stations, topped up at
    # 13:33Z for 27 more; read for 10-02 at 02:50Z for all.
    rows("2026-10-01T00:00:00Z", "2026-10-02", [f"S{i}" for i in range(45)],
         "2026-10-01T02:50:00Z")
    rows("2026-10-01T00:00:00Z", "2026-10-03", [f"S{i}" for i in range(4)],
         "2026-10-01T04:59:00Z")
    rows("2026-10-01T00:00:00Z", "2026-10-03", [f"S{i}" for i in range(4, 31)],
         "2026-10-01T13:33:00Z")
    # Older than the window: left to the paged cadence.
    rows("2026-09-29T12:00:00Z", "2026-10-01", ["S0"], "2026-09-29T15:00:00Z")
    conn.commit()
    return conn


def test_recent_runs_carry_per_day_stations_and_the_first_arrival(monkeypatch) -> None:
    monkeypatch.setattr(cd, "_now", lambda: NOW)
    plan = _member_conn().execute(
        "EXPLAIN QUERY PLAN " + cd.RECENT_RUN_STATIONS_SQL, (ICON, ICON_T, "x", "y", 0)
    ).fetchall()
    assert "COVERING INDEX calibration_native_member_skill_idx" in str(plan)
    out = cd.collect_recent_runs(_member_conn())
    assert [r["run_utc"] for r in out[ICON]] == ["2026-10-01T00:00:00Z"]
    run = out[ICON][0]
    assert run["days"] == {"2026-10-02": 45, "2026-10-03": 31}
    assert run["capture_utc"] == "2026-10-01T02:50:00Z"
    assert run["last_capture_utc"] == "2026-10-01T13:33:00Z"
    assert run["stations"] == 45


def test_the_fresh_runs_replace_the_pages_and_keep_its_older_ones() -> None:
    page = {ICON: [
        {"run_utc": "2026-09-30T18:00:00Z", "capture_utc": "old"},
        {"run_utc": "2026-09-30T12:00:00Z", "capture_utc": "older"},
    ]}
    fresh = {ICON: [
        {"run_utc": "2026-10-01T00:00:00Z", "capture_utc": "new"},
        {"run_utc": "2026-09-30T18:00:00Z", "capture_utc": "fresh"},
    ]}
    merged = cd._merge_recent_runs(page, fresh)
    assert [(r["run_utc"], r["capture_utc"]) for r in merged[ICON]] == [
        ("2026-10-01T00:00:00Z", "new"),
        ("2026-09-30T18:00:00Z", "fresh"),
        ("2026-09-30T12:00:00Z", "older"),
    ]


def _p0_conn(seated: dict[str, int], stations: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_probability_envelope("
        " envelope_id TEXT, station_id TEXT, event_date TEXT, stage TEXT,"
        " state TEXT, decision_at_utc TEXT, lineage_json TEXT);"
        "CREATE TABLE calibration_native_ensemble_historical_member_run("
        " ensemble_system_id TEXT, transport_provider_id TEXT,"
        " qc_status TEXT, target_local_date TEXT, native_run_init_utc TEXT,"
        " archive_query_at_utc TEXT);"
    )
    for day, n in seated.items():
        for i in range(stations):
            runs = {"ECMWF_IFS_ENS_OPEN_PF50": "2026-10-01T06:00:00Z"}
            weights = {"ECMWF_IFS_ENS_OPEN_PF50": 1.0}
            if i < n:
                runs[ICON] = "2026-10-01T00:00:00Z"
                weights[ICON] = 0.2
            conn.execute(
                "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?)",
                (f"{day}{i}", f"S{i}", day, "P0", "COMMITTED",
                 "2026-10-01T13:30:00Z", json.dumps({
                     "run_init_utc_by_system_json": json.dumps(runs),
                     "system_weights_json": json.dumps(weights),
                 })),
            )
    conn.commit()
    return conn


def test_a_short_near_day_and_an_empty_far_day_are_no_longer_green(monkeypatch) -> None:
    monkeypatch.setattr(cd, "_now", lambda: NOW)
    members = [{"system_id": ICON, "latest_run_utc": "2026-10-01T06:00:00Z"}]
    conn = _p0_conn({"2026-10-01": 44, "2026-10-02": 30, "2026-10-03": 0}, 45)
    out = cd.collect_member_admission(conn, members)
    icon = {d["event_date"]: d for d in next(
        f for f in out["families"] if f["system_id"] == ICON)["days"]}
    # a same-day run a station or two short is ordinary
    assert icon["2026-10-01"]["health"] == "green"
    # D+1 with a third of its P0s missing the family
    assert (icon["2026-10-02"]["flag"], icon["2026-10-02"]["health"]) == ("short", "amber")
    # D+2 with none: amber, the contract run lands in the evening
    assert (icon["2026-10-03"]["flag"], icon["2026-10-03"]["health"]) == ("absent", "amber")


def test_both_reads_are_every_run_and_the_matrix_is_on_the_head() -> None:
    assert {"member_admission", "recent_runs"} <= cd.RENDER_EVERY_RUN_SECTIONS
    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert "function modelRunHealth()" in page
    assert 'card(modelRunHealth, "模型 run 采集")' in page


def test_recent_runs_kept_between_paged_runs_read_only_new_rows(monkeypatch, tmp_path) -> None:
    # User 2026-11-03 「前端所有都是改为只读按日增量的部分」.
    monkeypatch.setattr(cd, "_now", lambda: NOW)
    monkeypatch.setattr(cd, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(cd._PAGED_RUN, "probe", True)
    conn = _member_conn()
    full = cd.collect_recent_runs(conn)
    reads: list[str] = []
    conn.set_trace_callback(reads.append)
    again = cd.collect_recent_runs(conn)
    conn.set_trace_callback(None)
    assert again == full
    assert not any("GROUP BY native_run_init_utc" in sql for sql in reads), reads
    monkeypatch.delitem(cd._PAGED_RUN, "probe")
