"""Settled days are read once (user 2026-10-20 「为什么要全量读历史」 / 「改」).

The centre landing, the model-skill table and the precipitation board re-read
their whole windows on every build although a closed day never changes.
Each now keeps its closed days and reads the rest; these pin that what it
reads back is what a full read gives, and that a day which changes is read
again.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import center_width_store  # noqa: E402

TODAY = date.today()


@pytest.fixture()
def paged(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    return tmp_path


# ---------------------------------------------------------------------------
# Centre landing: cells sampled one per three-hour block, as the store does.


def _bridge(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    conn.execute("CREATE TABLE basket_probability_bridge_history(stage TEXT, station_id TEXT,"
                 " event_date TEXT, decision_at_utc TEXT)")
    truth = {}
    for station in ("ZUUU", "KLGA"):
        for back in range(1, 14):
            day = TODAY - timedelta(days=back)
            truth[(station, day.isoformat())] = 20.0 + back
            for hours in range(-50, 20, 2):
                decided = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hours)
                conn.execute("INSERT INTO basket_probability_bridge_history VALUES('P0',?,?,?)",
                             (station, day.isoformat(), decided.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return truth


def _fake_cells(conn, truth, *, stage, min_decision_at_utc=None, pre_band=True, max_decision_at_utc=None):
    query = "SELECT station_id, event_date, decision_at_utc FROM basket_probability_bridge_history WHERE stage=?"
    params = [stage]
    if min_decision_at_utc:
        query += " AND decision_at_utc>=?"
        params.append(min_decision_at_utc)
    if max_decision_at_utc:
        query += " AND decision_at_utc<=?"
        params.append(max_decision_at_utc)
    blocks: dict[tuple[str, str, int], list[str]] = {}
    for station, day, decided in conn.execute(query, params):
        hour = int(decided[11:13])
        blocks.setdefault((station, day, decided[:10] + str(hour // 3)), []).append(decided)
    by_day: dict[str, list[dict]] = {}
    for (station, day, _block), stamps in blocks.items():
        stamps.sort()
        by_day.setdefault(day, []).append(
            {"station_id": station, "event_date": day, "decision_at_utc": stamps[len(stamps) // 2]})
    return by_day


def _entry(stage, cell, truth, seats):
    return {"station": cell["station_id"], "event_date": cell["event_date"],
            "error": float(cell["decision_at_utc"][11:13]), "sigma": 1.0}


def test_landing_cells_kept_or_read_are_the_full_reads(paged, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    truth = _bridge(conn)
    monkeypatch.setattr(center_width_store, "serving_cells_by_day", _fake_cells)
    monkeypatch.setattr(C, "_center_landing_entry", _entry)
    # A floor that cuts a day: its decisions span both sides.
    floor = f"{(TODAY - timedelta(days=9)).isoformat()}T05:00:00Z"

    def full(floor_):
        out = {}
        for day, cells in _fake_cells(conn, truth, stage="P0", min_decision_at_utc=floor_).items():
            out[day] = sorted((c["decision_at_utc"], c["station_id"]) for c in cells)
        return out

    def shaped(by_day):
        return {day: sorted((decided, e["station"]) for decided, e in items)
                for day, items in by_day.items() if items}

    first = C._landing_entries(conn, truth, {}, stage="P0", floor=floor)
    assert json.loads((paged / C.RENDER_CLOSED_LANDING_CELLS).read_text())["days"]
    second = C._landing_entries(conn, truth, {}, stage="P0", floor=floor)
    assert shaped(first) == full(floor)
    assert shaped(second) == full(floor)
    later = f"{(TODAY - timedelta(days=6)).isoformat()}T11:00:00Z"
    assert shaped(C._landing_entries(conn, truth, {}, stage="P0", floor=later)) == full(later)


def test_a_truth_revision_reads_the_day_again(paged, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    truth = _bridge(conn)
    monkeypatch.setattr(center_width_store, "serving_cells_by_day", _fake_cells)
    seen: list[str] = []

    def entry(stage, cell, truth_, seats):
        seen.append(cell["event_date"])
        return _entry(stage, cell, truth_, seats)

    monkeypatch.setattr(C, "_center_landing_entry", entry)
    C._landing_entries(conn, truth, {}, stage="P0", floor=None)
    revised = (TODAY - timedelta(days=10)).isoformat()
    truth[("ZUUU", revised)] += 1.0
    seen.clear()
    C._landing_entries(conn, truth, {}, stage="P0", floor=None)
    # Every settled day is held, recent ones too (user 2026-11-03
    # 「前端所有都是改为只读按日增量的部分」): only the revised day is read.
    assert set(seen) == {revised}, sorted(set(seen))


def test_a_seat_stored_after_the_first_read_reads_the_day_again(paged, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    truth = _bridge(conn)
    monkeypatch.setattr(center_width_store, "serving_cells_by_day", _fake_cells)
    seen: list[str] = []

    def entry(stage, cell, truth_, seats):
        seen.append(cell["event_date"])
        return _entry(stage, cell, truth_, seats)

    monkeypatch.setattr(C, "_center_landing_entry", entry)
    first = C._landing_entries(conn, truth, {}, stage="P0", floor=None)
    day = (TODAY - timedelta(days=1)).isoformat()
    decided = sorted(d for d, e in first[day] if e["station"] == "KLGA")[0]
    seen.clear()
    C._landing_entries(conn, truth, {}, stage="P0", floor=None)
    assert seen == []
    seats = {("KLGA", day, decided): {"seat_keys": ["21°C"], "state": "symmetric"}}
    C._landing_entries(conn, truth, seats, stage="P0", floor=None)
    assert set(seen) == {day}, sorted(set(seen))


# ---------------------------------------------------------------------------
# Precipitation: the AWC lane's summaries through a closed line.


def _precip_store(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_forecast_physics_version(station_id TEXT,"
                 " local_date TEXT, precipitation_sum REAL, capture_mode TEXT, captured_at_utc TEXT,"
                 " settlement_timezone TEXT, source_id TEXT)")
    (tmp_path / "data").mkdir(exist_ok=True)
    archive = sqlite3.connect(tmp_path / "data" / "archive.db")
    archive.execute("CREATE TABLE reported_trajectory_observations(station_id TEXT, report_type TEXT,"
                    " valid_at TEXT, raw_text TEXT, source_lane TEXT)")
    for back in range(1, 16):
        day = TODAY - timedelta(days=back)
        for station, zone in (("KLGA", "America/New_York"), ("ZUUU", "Asia/Shanghai")):
            captured = datetime.combine(day - timedelta(days=1), datetime.min.time(), timezone.utc)
            conn.execute("INSERT INTO calibration_station_forecast_physics_version VALUES(?,?,?,?,?,?,?)",
                         (station, day.isoformat(), float(back % 4), "forward",
                          captured.strftime("%Y-%m-%dT%H:%M:%SZ"), zone, "OPEN_METEO_MODEL_FORECAST"))
            for hour in range(0, 24):
                stamp = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour)
                wet = " RA " if (hour + back) % 7 == 0 else " "
                raw = f"METAR {station} {stamp:%d%H}00Z 00000KT 10SM{wet}FEW030 20/10 A3000 RMK AO2 P{(back * hour) % 9:04d}"
                archive.execute("INSERT INTO reported_trajectory_observations VALUES(?,?,?,?, 'awc')",
                                (station, "METAR", stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), raw))
    archive.commit()
    archive.close()
    return conn


def _without_clock(value):
    return {k: v for k, v in value.items() if k != "generated_at_utc"}


def test_precip_kept_days_read_back_as_the_full_read(paged, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(C, "ROOT", tmp_path)
    conn = _precip_store(tmp_path)
    monkeypatch.delitem(C._PAGED_RUN, "probe")
    full = C.collect_precip_classification(conn)
    assert full["available"]
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    first = C.collect_precip_classification(conn)
    memo = json.loads((paged / C.RENDER_CLOSED_PRECIP_DAYS).read_text())
    assert memo["days"] and memo["through"] == (TODAY - timedelta(days=C.PRECIP_CLOSED_DAYS)).isoformat()
    second = C.collect_precip_classification(conn)
    assert _without_clock(first) == _without_clock(full)
    assert _without_clock(second) == _without_clock(full)


# ---------------------------------------------------------------------------
# Model skill: closed days' run aggregates, invalidated by a later row.


def _skill_store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_station_market_timezone_binding(station_id TEXT, settlement_timezone TEXT);"
        "CREATE TABLE basket_target_market_registry(station_id TEXT, settlement_unit TEXT);"
        "CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT,"
        " official_tmax_c REAL, qc_status TEXT, revision_no INTEGER);"
        "CREATE TABLE calibration_native_ensemble_system_artifact(ensemble_system_id TEXT, station_id TEXT,"
        " lead_bucket TEXT, run_cycle TEXT, train_end_date TEXT, bias_parameters_json TEXT,"
        " spread_parameters_json TEXT);"
        "CREATE TABLE calibration_native_ensemble_historical_member_run(ensemble_system_id TEXT,"
        " transport_provider_id TEXT, qc_status TEXT, station_id TEXT, target_local_date TEXT,"
        " native_run_init_utc TEXT, run_cycle TEXT, archive_lead_bucket TEXT, raw_tmax_c REAL);"
    )
    system, _label, transport, _backfillable = C.MEMBER_FAMILIES[0]
    conn.execute("INSERT INTO calibration_station_market_timezone_binding VALUES('ZUUU','Asia/Shanghai')")
    conn.execute("INSERT INTO basket_target_market_registry VALUES('ZUUU','C')")
    for back in range(1, 20):
        day = TODAY - timedelta(days=back)
        conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES('ZUUU',?,?,'ACCEPTED',1)",
                     (day.isoformat(), 20.0 + back % 5))
        for lead_days in (1, 2):
            init = datetime.combine(day - timedelta(days=lead_days), datetime.min.time(), timezone.utc)
            for member in range(5):
                conn.execute(
                    "INSERT INTO calibration_native_ensemble_historical_member_run"
                    " VALUES(?,?,'ACCEPTED','ZUUU',?,?,'00Z','h24_48',?)",
                    (system, transport, day.isoformat(), init.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     19.0 + member * 0.4 + back % 3),
                )
    return conn


def test_skill_kept_days_read_back_and_a_backfilled_day_is_read_again(paged, monkeypatch) -> None:
    conn = _skill_store()
    monkeypatch.delitem(C._PAGED_RUN, "probe")
    full = C.collect_model_skill(conn)
    monkeypatch.setitem(C._PAGED_RUN, "probe", True)
    assert C.collect_model_skill(conn) == full
    assert json.loads((paged / C.RENDER_CLOSED_SKILL_DAYS).read_text())["days"]
    # Every day is held, open ones too (user 2026-11-03 「前端所有都是改为只读按日
    # 增量的部分」): a run reads only the member rows past the held rowid.
    reads: list[str] = []
    conn.set_trace_callback(reads.append)
    assert C.collect_model_skill(conn) == full
    conn.set_trace_callback(None)
    member_reads = [sql for sql in reads if "SUM(raw_tmax_c)" in sql]
    assert len(member_reads) == 1 and "rowid >" in member_reads[0], member_reads
    system, _label, transport, _backfillable = C.MEMBER_FAMILIES[0]
    day = (TODAY - timedelta(days=12)).isoformat()
    init = datetime.combine(TODAY - timedelta(days=13), datetime.min.time(), timezone.utc)
    conn.execute(
        "INSERT INTO calibration_native_ensemble_historical_member_run"
        " VALUES(?,?,'ACCEPTED','ZUUU',?,?,'00Z','h24_48',25.0)",
        (system, transport, day, init.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    kept_again = C.collect_model_skill(conn)
    monkeypatch.delitem(C._PAGED_RUN, "probe")
    assert kept_again == C.collect_model_skill(conn)
    assert kept_again != full, "the backfilled row is in"
