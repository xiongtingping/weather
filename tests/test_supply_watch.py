"""Charter 197: the supply watch lights what failed on 2026-09-21/22.

16:04Z the proxy tunnel stopped carrying traffic; every upstream failed with
transport errors; from 19:42Z no D+1 P0 was fresh anywhere, until 08:55Z the
next morning -- and every light on the board stayed green, because each
measured a lane moving rather than the thing it moves arriving.  These tests
hold the watch to the two readings that would have said so: the egress probe
and the served P0 freshness per listed future date.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from bayes_basket_target import supply_watch as sw
from bayes_basket_target.ssot import apply_production_ssot_schema

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
D1 = "2026-09-23"
D2 = "2026-09-24"
STATIONS = [f"S{i:03d}" for i in range(12)]


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(db)
    return db


def _bridge(conn: sqlite3.Connection, station: str, event_date: str, *,
            stage: str, fresh_until: datetime, decision_at: datetime | None = None) -> None:
    decision_at = decision_at or fresh_until - timedelta(hours=6)
    conn.execute(
        "INSERT OR REPLACE INTO basket_probability_bridge_latest("
        "station_id,event_date,settlement_timezone,history_id,stage,checkpoint_id,"
        "decision_at_utc,fresh_until_utc,envelope_content_sha256,projection_sha256,"
        "content_sha256,updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (station, event_date, "UTC", f"h-{station}-{event_date}", stage, "c",
         _iso(decision_at), _iso(fresh_until), "a" * 64, "b" * 64, "c" * 64,
         _iso(decision_at)),
    )
    conn.commit()


def _all_ok(url: str, proxy: str, timeout: float) -> dict:  # noqa: ARG001
    return {"outcome": "ok", "http_status": 200}


def _all_down(url: str, proxy: str, timeout: float) -> dict:  # noqa: ARG001
    return {"outcome": "transport_error", "error": "ConnectError"}


def _listed(*dates: str) -> dict[str, set[str]]:
    return {d: set(STATIONS) for d in dates}


# ---------------------------------------------------------------- P0 supply


def test_d1_with_every_p0_expired_is_red_and_first_in_the_recapture_order(conn) -> None:
    # The 2026-09-22 morning: the last 09-23 packet expired at 21:37Z.
    for station in STATIONS:
        _bridge(conn, station, D1, stage="P0",
                fresh_until=datetime(2026, 9, 21, 21, 37, tzinfo=timezone.utc))
        _bridge(conn, station, D2, stage="P0", fresh_until=NOW + timedelta(hours=3))

    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D1, D2),
                      egress=sw.probe_egress(proxy="", probe=_all_ok))

    assert out["overall"] == "red"
    [row] = [r for r in out["alerts"] if r["label"] == f"P0 供给 {D1}"]
    assert row["health"] == "red"
    assert "全队 P0 过期" in row["note"] and "2026-09-21T21:37:00Z" in row["note"]
    assert out["recapture_priority"][0]["event_date"] == D1
    assert out["notify"]["required"] is True


def test_a_fresh_p0m_is_supply_and_a_fully_fresh_date_is_green(conn) -> None:
    for station in STATIONS:
        _bridge(conn, station, D1, stage="P0-M", fresh_until=NOW + timedelta(hours=2))

    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D1),
                      egress=sw.probe_egress(proxy="", probe=_all_ok))

    assert out["overall"] == "green"
    assert out["p0_supply"][0]["fresh"] == len(STATIONS)


def test_a_p1_row_is_not_p0_supply(conn) -> None:
    for station in STATIONS:
        _bridge(conn, station, D1, stage="P1", fresh_until=NOW + timedelta(hours=1))

    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D1), egress=None)

    assert out["p0_supply"][0]["fresh"] == 0
    assert out["overall"] == "red"


def test_d1_under_half_fresh_is_amber(conn) -> None:
    for index, station in enumerate(STATIONS):
        fresh = index < 4
        _bridge(conn, station, D1, stage="P0",
                fresh_until=NOW + timedelta(hours=2 if fresh else -2))

    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D1), egress=None)

    assert out["overall"] == "amber"
    assert out["p0_supply"][0]["verdict"] == "thin"


def test_d2_waiting_for_its_first_run_is_not_an_alert_but_d2_gone_stale_is(conn) -> None:
    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D2), egress=None)
    assert out["overall"] == "green"

    for station in STATIONS:
        _bridge(conn, station, D2, stage="P0", fresh_until=NOW - timedelta(minutes=5))
    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D2), egress=None)
    assert out["overall"] == "amber"
    assert out["p0_supply"][0]["verdict"] == "fleet_expired"


def test_the_event_day_belongs_to_p1_and_is_not_judged(conn) -> None:
    today = NOW.date().isoformat()
    for station in STATIONS:
        _bridge(conn, station, today, stage="P0", fresh_until=NOW - timedelta(hours=5))

    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(today), egress=None)

    assert out["p0_supply"] == [] and out["overall"] == "green"


def test_a_small_early_listing_is_not_a_fleet(conn) -> None:
    listed = {D1: set(STATIONS[: sw.MIN_FLEET_STATIONS - 1])}

    out = sw.evaluate(now=NOW, conn=conn, listed=listed, egress=None)

    assert out["overall"] == "green"


def test_capture_evidence_names_why_the_families_held(tmp_path: Path, conn) -> None:
    status = tmp_path / "status.json"
    status.write_text(json.dumps({
        "official_native_capture_by_date": {D1: {"families_held_on_earlier_run": {
            "ECMWF_OPEN_DATA_S3": "ContractError:ecmwf_archive_transport_failed:ConnectError",
            "ECCC_DD_WEATHER": "ContractError:eccc_geps_archive_scope_invalid",
        }}},
        "production": {"native_ensemble_ingress": {"active_activation_p0_replay": {
            "failures": [{"reason": "ContractError:FAIL_CLOSED_NO_P0:native_ensemble_snapshot_stale"}] * 3,
        }}},
    }))
    evidence = sw.capture_evidence(status)
    assert evidence["families_held_by_date"][D1] == {
        "ECMWF_OPEN_DATA_S3": "网络", "ECCC_DD_WEATHER": "本轮不覆盖",
    }
    assert evidence["p0_compile_refusals"] == {"native_ensemble_snapshot_stale": 3}

    for station in STATIONS:
        _bridge(conn, station, D1, stage="P0", fresh_until=NOW - timedelta(hours=1))
    out = sw.evaluate(now=NOW, conn=conn, listed=_listed(D1), egress=None, evidence=evidence)
    note = out["alerts"][0]["note"]
    assert "ECMWF_OPEN_DATA_S3（网络）" in note and "native_ensemble_snapshot_stale×3" in note


# ---------------------------------------------------------------- egress


def test_every_upstream_failing_together_is_the_tunnel_amber_then_red(conn) -> None:
    first = sw.evaluate(now=NOW, conn=conn, listed={},
                        egress=sw.probe_egress(proxy="http://127.0.0.1:1082", probe=_all_down))
    assert first["egress"]["state"] == "down"
    assert first["overall"] == "amber"
    assert "不是某家限流" in first["alerts"][0]["note"]

    later = NOW + timedelta(seconds=sw.EGRESS_RED_AFTER_SEC + 60)
    second = sw.evaluate(now=later, conn=conn, listed={},
                         egress=sw.probe_egress(proxy="http://127.0.0.1:1082", probe=_all_down),
                         previous=first)
    assert second["overall"] == "red"
    assert second["egress"]["since_utc"] == first["egress"]["since_utc"]
    assert second["notify"]["required"] is True

    third = sw.evaluate(now=later + timedelta(minutes=5), conn=conn, listed={},
                        egress=sw.probe_egress(proxy="", probe=_all_down), previous=second)
    assert third["notify"]["key"] == second["notify"]["key"]
    assert third["notify"]["required"] is False

    cleared = sw.evaluate(now=later + timedelta(minutes=10), conn=conn, listed={},
                          egress=sw.probe_egress(proxy="", probe=_all_ok), previous=third)
    assert cleared["overall"] == "green"
    assert cleared["notify"]["required"] is True
    assert cleared["notify"]["text"].startswith("已恢复")
    assert cleared["egress"]["outage_since_utc"] == first["egress"]["since_utc"]


def test_a_throttle_is_not_an_outage() -> None:
    def s3_slow_down(url: str, proxy: str, timeout: float) -> dict:  # noqa: ARG001
        if "amazonaws" in url:
            return {"outcome": "throttled", "http_status": 503}
        return {"outcome": "ok", "http_status": 200}

    egress = sw.probe_egress(proxy="", probe=s3_slow_down)
    assert egress["state"] == "throttled"
    assert set(egress["throttled"]) == {"ecmwf_s3", "gefs_s3"}


def test_one_provider_down_is_partial() -> None:
    def gcs_blocked(url: str, proxy: str, timeout: float) -> dict:  # noqa: ARG001
        if "googleapis" in url:
            return {"outcome": "transport_error", "error": "ConnectError"}
        return {"outcome": "ok", "http_status": 200}

    assert sw.probe_egress(proxy="", probe=gcs_blocked)["state"] == "partial"


def test_a_refused_proxy_port_is_named_as_the_proxy_app_being_down() -> None:
    refused = httpx.ConnectError("[Errno 61] Connection refused")
    assert sw.classify_transport_error(refused) == "proxy_port_refused"
    assert sw.classify_transport_error(httpx.PoolTimeout("pool")) == "PoolTimeout"

    def port_closed(url: str, proxy: str, timeout: float) -> dict:  # noqa: ARG001
        return {"outcome": "transport_error", "error": "proxy_port_refused"}

    assert sw.probe_egress(proxy="http://127.0.0.1:1082", probe=port_closed)["state"] == "proxy_down"


def test_the_head_probe_reports_status_and_never_raises(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "slow" in str(request.url):
            return httpx.Response(503)
        if "dead" in str(request.url):
            raise httpx.ConnectError("EOF occurred in violation of protocol")
        return httpx.Response(403)

    real_client = httpx.Client

    def client(**kwargs):
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(sw.httpx, "Client", client)
    assert sw.http_head_probe("https://ok.example/", "", 1.0) == {"outcome": "ok", "http_status": 403}
    assert sw.http_head_probe("https://slow.example/", "", 1.0)["outcome"] == "throttled"
    assert sw.http_head_probe("https://dead.example/", "", 1.0) == {
        "outcome": "transport_error", "error": "ConnectError",
    }


# ---------------------------------------------------------------- reads


def test_the_bridge_read_is_a_primary_key_seek(conn) -> None:
    plan = " ".join(
        str(row[-1]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sw.bridge_rows_query(3), ["A", "B", "C", D1]
        )
    )
    assert "sqlite_autoindex_basket_probability_bridge_latest_1" in plan
    assert "SCAN" not in plan


def test_the_book_clock_is_a_covering_index_seek(conn) -> None:
    plan = " ".join(
        str(row[-1]) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(quote_observed_at) FROM basket_official_ws_snapshot"
        )
    )
    assert "COVERING INDEX" in plan and "SCAN" not in plan


def test_the_registry_lists_only_active_fleet_markets(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"rows": [
        {"station_id": "S000", "event_date": D1, "market_active": 1},
        {"station_id": "S001", "event_date": D1, "market_active": 0},
        {"station_id": "LLBG", "event_date": D1, "market_active": 1},
    ]}))
    assert sw.listed_scopes(registry, fleet=["S000", "S001"]) == {D1: {"S000"}}


def test_run_writes_the_file_and_carries_the_clock(tmp_path: Path, conn, monkeypatch) -> None:
    db = tmp_path / "ssot.db"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"rows": []}))
    monkeypatch.setattr(sw, "fleet_station_ids", lambda: STATIONS)
    out = tmp_path / "watch.json"
    first = sw.run(now=NOW, db_path=db, registry_path=registry,
                   status_path=tmp_path / "missing.json", taf_archive_dir=tmp_path,
                   output_path=out, probe=_all_down, proxy="")
    assert json.loads(out.read_text())["egress"]["since_utc"] == first["egress"]["since_utc"]
    second = sw.run(now=NOW + timedelta(minutes=5), db_path=db, registry_path=registry,
                    status_path=tmp_path / "missing.json", taf_archive_dir=tmp_path,
                    output_path=out, probe=_all_down, proxy="")
    assert second["overall"] == "red"
    assert second["writes_database"] is False and second["calls_order_gateway"] is False


# ---------------------------------------------------------------- wiring


def _daemon():
    spec = importlib.util.spec_from_file_location(
        "weather_basket_yes_daemon_supply_watch",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_watch_runs_on_the_resident_clock_without_a_write_domain() -> None:
    daemon = _daemon()
    entry = {name: (script, interval, timeout)
             for name, script, interval, timeout in daemon.HOSTED_SWEEPS}["supply_watch"]
    script, interval, timeout = entry
    assert (ROOT / script).is_file()
    # An outage must be red inside two passes plus the red threshold.
    assert 2 * interval + sw.EGRESS_RED_AFTER_SEC <= 900.0
    assert timeout >= sw.EGRESS_TIMEOUT_SEC * 2
    assert daemon.hosted_sweep_write_domain("supply_watch") == "none"
    assert daemon.writer_excluding_sweeps(["supply_watch"]) == []


def _dashboard():
    spec = importlib.util.spec_from_file_location(
        "calibration_dashboard_supply_watch", ROOT / "scripts" / "calibration_dashboard.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_board_banner_carries_the_watch_rows_and_runs_it_when_the_sweep_is_silent(
    tmp_path: Path, monkeypatch,
) -> None:
    dashboard = _dashboard()
    out = tmp_path / "watch.json"
    monkeypatch.setattr(sw, "DEFAULT_OUTPUT", out)
    red = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alerts": [{"health": "red", "label": "外网出口", "note": "全部上游同时不可达"}],
    }
    out.write_text(json.dumps(red))
    calls: list[int] = []
    monkeypatch.setattr(sw, "DEFAULT_DB", tmp_path / "ssot.db")
    assert dashboard._supply_watch_alerts(
        db=tmp_path / "fixture.db", run_inline=lambda: calls.append(1) or {},
    ) == []  # a render of any other database never probes or reads the file
    rows = dashboard._supply_watch_alerts(
        db=tmp_path / "ssot.db", run_inline=lambda: calls.append(1) or {},
    )
    assert rows == [{"health": "red", "label": "外网出口", "note": "全部上游同时不可达"}]
    assert calls == []  # a fresh file is read, not re-run

    stale = dict(red, generated_at_utc="2026-09-21T00:00:00Z")
    out.write_text(json.dumps(stale))
    rows = dashboard._supply_watch_alerts(
        db=tmp_path / "ssot.db", run_inline=lambda: {"alerts": []},
    )
    assert [r["label"] for r in rows] == ["守望失效"]
    assert "由看板代跑" in rows[0]["note"] and "不拦单" in rows[0]["note"]
