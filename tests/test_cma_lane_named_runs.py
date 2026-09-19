"""CMA's lane asks for cycles by name (2026-09-19, user: 「接 CMA 官方源」).

CMA's own WIS2 node lists CMA_GFS but publishes no file for it (only the
GRAPES ensemble, 5-6 hours after init and ~6 GB a run), so the aggregator
stays the transport.  What changed is how it is asked.  The lane used to read
the aggregator's current forecast and derive the cycle from the clock; checked
against the aggregator's single-run endpoint, every such row held the run six
hours older than its label, and 52 of 360 consecutive labels repeated the row
before.  Now each cycle is requested by name: the label is the run, an
unpublished cycle costs one request, and a named row supersedes a derived one
on its key (the old row is kept in an audit file, not lost).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from bayes_basket_target import cma_grapes_point_archive as archive  # noqa: E402
from bayes_basket_target import regional_point_lane as lane  # noqa: E402

SHANGHAI = ("ZSPD", 31.14, 121.81, "Asia/Shanghai")
NOW = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)


def test_cma_plans_every_published_cycle_of_the_last_day() -> None:
    runs = lane._candidate_runs("CMA", NOW, date(2026, 9, 19), "Asia/Shanghai")
    assert len(runs) > 1
    assert all(NOW - timedelta(hours=24) <= r <= NOW - timedelta(hours=lane.PUBLICATION_LAG_HOURS["CMA"]) for r in runs)
    assert all(r.hour in lane.RUN_CYCLE_HOURS["CMA"] for r in runs)


def test_the_run_addressable_products_still_enumerate_their_cadence() -> None:
    for product, station in (
        ("HRRR", ("KATL", 33.63, -84.44, "America/New_York")),
        ("MSM", SHANGHAI),
        ("ICON_EU", ("EDDM", 48.35, 11.79, "Europe/Berlin")),
    ):
        assert len(lane._candidate_runs(product, NOW, date(2026, 9, 19), station[3])) > 1


def test_a_cycle_outside_the_event_day_window_is_not_planned() -> None:
    assert lane._candidate_runs("CMA", NOW, date(2026, 9, 15), "Asia/Shanghai") == []


def _fake_fetch(fetched, published):
    def fetch(*, run_init, event_date, utc_offset_hours, latitude, longitude):
        fetched.append((run_init, event_date))
        if run_init not in published:
            raise archive.RunNotPublished("cma_grapes_run_not_published")
        base = 18.0 + run_init.hour / 6.0          # a curve that differs by run
        return {"hourly_c": {h: base + h / 10.0 for h in range(24)},
                "hours_wanted": list(range(24))}
    return fetch


def _pass(conn, monkeypatch, *, now, fetched, published, tmp_path):
    monkeypatch.setattr(archive, "fetch_day_maximum", _fake_fetch(fetched, published))
    monkeypatch.setattr(lane, "SUPERSEDED_AUDIT", tmp_path / "superseded.jsonl")
    return lane.capture_p1(conn, product="CMA", now=now, stations=[SHANGHAI], max_runs=2)


def _conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "ssot.db")
    lane.ensure_schema(conn)
    conn.commit()
    return conn


def test_rows_are_labelled_with_the_run_asked_for(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    fetched: list = []
    r12 = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    r06 = datetime(2026, 9, 18, 6, tzinfo=timezone.utc)
    receipt = _pass(conn, monkeypatch, now=NOW, fetched=fetched,
                    published={r12, r06}, tmp_path=tmp_path)
    rows = list(conn.execute(f"SELECT run_init_utc, extraction_version FROM {lane.TABLE}"))
    assert {r[0] for r in rows} <= {"2026-09-18T12:00:00Z", "2026-09-18T06:00:00Z"}
    assert {r[1] for r in rows} == {lane.EXTRACTION_VERSIONS["CMA"]}
    assert receipt["written"] == len(rows) > 0
    # 18Z is not out: one request said so for every station-day of it.
    r18 = datetime(2026, 9, 18, 18, tzinfo=timezone.utc)
    assert sum(1 for run, _ in fetched if run == r18) == 1
    assert "CMA 2026-09-18T18Z" in receipt["unpublished"]


def test_a_stored_named_run_costs_no_request_next_pass(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    fetched: list = []
    r12 = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    r06 = datetime(2026, 9, 18, 6, tzinfo=timezone.utc)
    _pass(conn, monkeypatch, now=NOW, fetched=fetched, published={r12, r06}, tmp_path=tmp_path)
    before = [f for f in fetched if f[0] in (r12, r06)]
    fetched.clear()
    _pass(conn, monkeypatch, now=NOW, fetched=fetched, published={r12, r06}, tmp_path=tmp_path)
    assert not [f for f in fetched if f[0] in (r12, r06)] and before


def test_a_named_run_supersedes_a_derived_label_and_keeps_it_on_file(tmp_path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    r12 = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    # A v2 row labelled 12Z holding another run's curve (the measured case).
    monkeypatch.setitem(lane.EXTRACTION_VERSIONS, "CMA", "cma_grapes_global_open_meteo_derived_cycle_v2")
    lane.write_run(conn, product="CMA", station_id="ZSPD", event_date=date(2026, 9, 19),
                   settlement_timezone="Asia/Shanghai", run_init=r12,
                   hourly_c={h: 30.0 for h in range(24)}, hours_wanted=24,
                   archive_query_at=datetime(2026, 9, 18, 17, 46, tzinfo=timezone.utc),
                   source_url="https://example.invalid/current")
    conn.commit()
    monkeypatch.undo()
    # The derived row does not count as holding 12Z.
    assert "2026-09-18T12:00:00Z" not in lane.stored_run_inits(
        conn, system_id="CMA_GRAPES_GLOBAL_POINT", station_id="ZSPD", event_date="2026-09-19")
    fetched: list = []
    _pass(conn, monkeypatch, now=NOW, fetched=fetched, published={r12}, tmp_path=tmp_path)
    rows = list(conn.execute(
        f"SELECT extraction_version, forecast_tmax_c FROM {lane.TABLE}"
        " WHERE station_id='ZSPD' AND target_local_date='2026-09-19' AND run_init_utc='2026-09-18T12:00:00Z'"))
    assert rows and rows[0][0] == lane.EXTRACTION_VERSIONS["CMA"] and rows[0][1] != 30.0
    audit = [json.loads(line) for line in (tmp_path / "superseded.jsonl").read_text().splitlines()]
    assert audit and audit[0]["row"]["extraction_version"].endswith("derived_cycle_v2")
    assert audit[0]["row"]["forecast_tmax_c"] == 30.0


def test_a_pass_with_no_new_cycle_takes_no_write_lock(monkeypatch) -> None:
    import capture_cma_grapes_forward as script  # noqa: PLC0415

    calls = {"prepare": 0}
    monkeypatch.setattr(lane, "plan_p1", lambda *a, **k: [
        {"product": "CMA", "run_init": datetime(2026, 9, 18, 18, tzinfo=timezone.utc), "targets": []}])
    monkeypatch.setattr(archive, "run_published", lambda run: False)
    monkeypatch.setattr(lane, "prepare", lambda conn: calls.__setitem__("prepare", calls["prepare"] + 1))
    monkeypatch.setattr(sys, "argv", ["capture_cma_grapes_forward.py", "--db", ":memory:"])
    assert script.main() == 0
    assert calls["prepare"] == 0


def test_the_probe_rings_often_and_writes_nothing(daemon_module) -> None:
    """The capture stays on the SSOT queue at half its cadence; the probe
    that makes it due at once asks often and holds no write domain."""

    intervals = {name: interval for name, _s, interval, _t in daemon_module.HOSTED_SWEEPS}
    spacing_sec = 24 * 3600 / len(lane.RUN_CYCLE_HOURS["CMA"])
    assert intervals["probe_cma_named_runs"] <= spacing_sec / 4
    assert daemon_module.hosted_sweep_write_domain("probe_cma_named_runs") == "none"
    assert intervals["capture_cma_grapes_forward"] >= spacing_sec / 2 - 1


def test_a_published_cycle_makes_the_capture_due_once(daemon_module, tmp_path) -> None:
    state = tmp_path / "probe.json"
    next_due = {"capture_cma_grapes_forward": 5000.0}
    honoured: dict = {}
    state.write_text(json.dumps({"newest_wanted_run_utc": "2026-09-18T18:00:00Z", "published": False}))
    assert daemon_module.honour_cma_probe(next_due, now_mono=100.0, honoured=honoured, path=state) is None
    assert next_due["capture_cma_grapes_forward"] == 5000.0
    state.write_text(json.dumps({"newest_wanted_run_utc": "2026-09-18T18:00:00Z", "published": True}))
    assert daemon_module.honour_cma_probe(next_due, now_mono=100.0, honoured=honoured, path=state) == "2026-09-18T18:00:00Z"
    assert next_due["capture_cma_grapes_forward"] == 100.0
    # The same cycle again: a refused or failed capture waits its interval.
    next_due["capture_cma_grapes_forward"] = 9000.0
    assert daemon_module.honour_cma_probe(next_due, now_mono=200.0, honoured=honoured, path=state) is None
    assert next_due["capture_cma_grapes_forward"] == 9000.0


@pytest.fixture(scope="module")
def daemon_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cma_interval_daemon_under_test", ROOT / "scripts" / "weather_basket_yes_daemon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
