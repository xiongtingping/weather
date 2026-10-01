"""Three board readings added 2026-09-18.

入围: the run each open day's P0 actually seats, beside the capture lamp.
挂单卖出: the sell lanes' orders on the station-day listing.
辐射降温项: the radiation shift each newest P0 served.
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

NOW = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)


def test_a_sell_lane_id_names_its_station_day() -> None:
    assert cd._intent_lane("recycle998_decision_ZUUU_2026-09-18") == "recycle998"
    assert cd._intent_lane("misbuyexit_decision_OEJN_2026-09-18") == "misbuyexit"
    assert cd._intent_lane("decision_0a1b2c") == "basket"
    # Underscored prefixes: read as basket until 2026-09-23.
    assert cd._intent_lane("p1_tail_decision_KDAL_2026-09-22") == "p1_tail"
    assert cd._intent_lane("p1_consensus_decision_LTAC_2026-09-23") == "p1_consensus"
    assert cd._lane_station_day(
        "p1_tail_decision_KDAL_2026-09-22", None, None
    ) == ("KDAL", "2026-09-22")
    assert cd._lane_station_day(
        "recycle998_decision_ZUUU_2026-09-18", None, None
    ) == ("ZUUU", "2026-09-18")
    # A decision row, where there is one, is the authority.
    assert cd._lane_station_day(
        "tiebreak_decision_WSSS_2026-09-20", "WSSS", "2026-09-20"
    ) == ("WSSS", "2026-09-20")


def _orders_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_order_intents("
        " order_intent_id TEXT PRIMARY KEY, decision_id TEXT, cycle_id TEXT,"
        " bucket TEXT, side TEXT, order_action TEXT, limit_price REAL,"
        " requested_size REAL, requested_notional_usdc REAL, state TEXT,"
        " live_submit_authorized INTEGER, created_at TEXT,"
        " target_execution_hard_deadline_at TEXT);"
        "CREATE TABLE auto_trade_decisions("
        " decision_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " details_json TEXT NOT NULL DEFAULT '{}');"
        "CREATE TABLE basket_target_order_reconciliations("
        " order_intent_id TEXT PRIMARY KEY, state TEXT, filled_size REAL,"
        " filled_notional_usdc REAL, blocking_reason TEXT);"
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES ('d1','ZUUU','2026-09-18','{}')"
    )
    conn.executemany(
        "INSERT INTO basket_target_order_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("buy1", "d1", "c", "24°C", "YES", "BUY", 0.07, 15, 1.05,
             "filled", 1, "2026-09-17T22:15:00Z", None),
            ("sell1", "recycle998_decision_ZUUU_2026-09-18", "c", "24°C",
             "YES", "SELL", 0.998, 15, 14.97, "filled", 1,
             "2026-09-18T09:00:00Z", None),
            ("sell2", "recycle998_decision_ZUUU_2026-09-18", "c", "25°C",
             "YES", "SELL", 0.99, 6, 5.94, "submitted", 1,
             "2026-09-18T10:00:00Z", None),
        ],
    )
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations VALUES"
        " ('sell1','filled',15,14.97,NULL)"
    )
    conn.commit()
    return conn


def test_a_sell_reaches_the_listing_under_its_station_day() -> None:
    rows = cd._collect_live_orders(_orders_conn())["rows"]
    sells = {r["order_intent_id"]: r for r in rows if r["order_action"] == "SELL"}
    assert set(sells) == {"sell1", "sell2"}
    for row in sells.values():
        assert (row["station_id"], row["event_date"]) == ("ZUUU", "2026-09-18")
        assert row["lane"] == "recycle998"
    assert sells["sell1"]["filled_size"] == 15
    buy = next(r for r in rows if r["order_intent_id"] == "buy1")
    assert buy["order_action"] == "BUY" and buy["lane"] == "basket"


def _p0_conn() -> sqlite3.Connection:
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

    def envelope(eid, station, day, icon_run, cooling):
        lineage = {
            "run_init_utc_by_system_json": json.dumps({
                "DWD_ICON_GLOBAL_EPS": icon_run,
                "ECMWF_IFS_ENS_OPEN_PF50": "2026-09-18T00:00:00Z",
            }),
            "system_weights_json": json.dumps({
                "DWD_ICON_GLOBAL_EPS": 0.3, "ECMWF_IFS_ENS_OPEN_PF50": 0.7,
            }),
            "synoptic_cooling_json": json.dumps(cooling),
            "model_center_c": 25.2,
            "training_lead_bucket": "h12_24",
            "center_width_json": json.dumps({"distribution_mean_c": 24.3}),
        }
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?)",
            (eid, station, day, "P0", "COMMITTED", "2026-09-18T10:30:00Z",
             json.dumps(lineage)),
        )

    cooling = {"state": "RESOLVED", "method_tag": "forecast_radiation_shift_v1",
               "shift_c": -0.87, "shift_sigma": -0.45, "standardised": -1.27,
               "shift_sigma_per_unit": 0.35, "radiation_sum": 10.18,
               "sample_count": 162}
    # 09-18 on ICON's 00Z; 09-19 still on the day before's 12Z (the case).
    envelope("a", "ZUUU", "2026-09-18", "2026-09-18T00:00:00Z", cooling)
    envelope("b", "ZUUU", "2026-09-19", "2026-09-17T12:00:00Z", cooling)
    for day, run in (("2026-09-18", "2026-09-18T00:00:00Z"),
                     ("2026-09-19", "2026-09-17T12:00:00Z")):
        conn.execute(
            "INSERT INTO calibration_native_ensemble_historical_member_run"
            " VALUES ('DWD_ICON_GLOBAL_EPS','DWD_OPENDATA_ICON_EPS','ACCEPTED',?,?,?)",
            (day, run, "2026-09-18T09:49:00Z"),
        )
    conn.commit()
    return conn


def test_admission_lights_a_day_served_an_older_run_than_a_nearer_day(monkeypatch) -> None:
    monkeypatch.setattr(cd, "_now", lambda: NOW)
    members = [{"system_id": "DWD_ICON_GLOBAL_EPS",
                "latest_run_utc": "2026-09-18T00:00:00Z"}]
    out = cd.collect_member_admission(_p0_conn(), members)
    icon = next(f for f in out["families"] if f["label"] == "ICON")
    by_day = {d["event_date"]: d for d in icon["days"]}
    assert by_day["2026-09-18"]["flag"] == "ok"
    assert by_day["2026-09-19"]["served_run_utc"] == "2026-09-17T12:00:00Z"
    assert by_day["2026-09-19"]["flag"] == "day_behind"
    assert icon["health"] == "amber"
    assert icon["focus_event_date"] == "2026-09-19"


def test_the_radiation_shift_is_read_off_each_newest_p0(monkeypatch) -> None:
    monkeypatch.setattr(cd, "_now", lambda: NOW)
    out = cd.collect_synoptic_cooling(_p0_conn())
    rows = {r["event_date"]: r for r in out["rows"]}
    assert rows["2026-09-19"]["shift_c"] == -0.87
    assert rows["2026-09-19"]["per_unit"] == 0.35
    assert rows["2026-09-19"]["served_mean_c"] == 24.3
    day = next(d for d in out["summary"] if d["event_date"] == "2026-09-19")
    assert day["coldest"] == ["ZUUU", -0.87] and day["big_cooling"] == 1


def test_the_previous_version_window_is_read_off_the_declarations(monkeypatch) -> None:
    """The comparison moves by itself on the next bump."""

    from bayes_basket_target import center_width_store, serving_arithmetic as sa

    monkeypatch.setattr(sa, "STAGE_VERSION_EFFECTIVE_FROM", {"P0": {
        "p0_v5": "2026-09-14T05:52:13Z", "p0_v6": "2026-09-18T09:18:16Z",
    }})
    monkeypatch.setattr(center_width_store, "served_arithmetic_anchor",
                        lambda conn, stage, known_floor=None: "2026-09-18T09:18:16Z")
    window = cd._previous_version_window(sqlite3.connect(":memory:"), "P0")
    assert window == {"version": "p0_v5", "since": "2026-09-14T05:52:13Z",
                      "until": "2026-09-18T09:18:16Z", "current_version": "p0_v6"}
    # A first version has nothing to compare with.
    monkeypatch.setattr(sa, "STAGE_VERSION_EFFECTIVE_FROM", {"P0": {"p0_v6": "2026-09-18T09:18:16Z"}})
    assert cd._previous_version_window(sqlite3.connect(":memory:"), "P0") is None


def test_the_publication_table_follows_the_upstream_lamps() -> None:
    """User, 2026-09-18: 四大模型、区域模型、TAF 的发布时间放在顶部看板."""

    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert "function publishTimes()" in page
    # 2026-10-01: the run-health matrix sits between the lamps and the table.
    assert "rendered[1] + runHealth + published" in page


def test_a_regional_lane_states_its_rhythm_and_publication_delay(monkeypatch) -> None:
    monkeypatch.setattr(cd, "_now", lambda: NOW)
    runs = [  # newest first: hourly issue, landing ~2h after
        {"run_utc": "2026-09-18T09:00:00Z", "capture_utc": "2026-09-18T11:00:00Z",
         "stations": 11, "run_age_hours": 2.0, "capture_age_hours": 0.0},
        {"run_utc": "2026-09-18T08:00:00Z", "capture_utc": "2026-09-18T10:10:00Z",
         "stations": 11, "run_age_hours": 3.0, "capture_age_hours": 0.8333},
        {"run_utc": "2026-09-18T07:00:00Z", "capture_utc": "2026-09-18T09:00:00Z",
         "stations": 10, "run_age_hours": 4.0, "capture_age_hours": 2.0},
    ]
    out = cd._run_rhythm(runs)
    assert out["cycle_hours"] == 1.0
    assert abs(out["publish_lag_hours"] - 2.0) < 1e-9
    assert out["latest_run_capture_utc"] == "2026-09-18T11:00:00Z"
    assert out["latest_run_stations"] == 11
    assert abs(out["due_in_hours"] - 1.0) < 1e-9     # 1h rhythm + 2h delay − 2h old
    assert cd._run_rhythm([])["cycle_hours"] is None


def test_station_day_money_is_the_collectors_not_the_browsers() -> None:
    def leg(intent, decision, bucket, action, state, filled, notional, size, ask,
            station="ZUUU", day="2026-09-18", truth="24°C", reason=""):
        return {"order_intent_id": intent, "decision_id": decision, "bucket": bucket,
                "order_action": action, "state": state, "filled_size": filled,
                "filled_notional_usdc": notional, "requested_size": size,
                "requested_notional_usdc": size * ask, "limit_price": ask,
                "station_id": station, "event_date": day, "reason": reason,
                "settled_bucket": truth, "settled_bucket_source": "paper_ledger",
                "lane": cd._intent_lane(decision)}
    rows = [
        leg("b1", "d1", "24°C", "BUY", "filled", 15, 1.05, 15, 0.07),
        leg("b2", "d1", "25°C", "BUY", "failed", 0, 0, 10, 0.2, reason="x"),
        leg("s1", "recycle998_decision_ZUUU_2026-09-18", "24°C", "SELL", "filled",
            5, 4.99, 5, 0.998),
        leg("s2", "recycle998_decision_ZUUU_2026-09-18", "25°C", "SELL",
            "submitted", 0, 0, 6, 0.99),
        # A basket the station bar refused whole never reached the exchange.
        leg("b3", "d2", "30°C", "BUY", "failed", 0, 0, 10, 0.3, station="ZBAA",
            truth="", reason="live_station_barred"),
    ]
    money = cd._live_station_day_money(rows)
    assert [r["station_id"] for r in money["rows"]] == ["ZUUU"]
    z = money["rows"][0]
    assert (z["buy_legs"], z["filled_legs"], z["cost_usdc"]) == (2, 1, 1.05)
    assert z["settlement_usdc"] == 10.0  # 15 held in 24°C, 5 of them sold
    assert (z["sell_proceeds_usdc"], z["payout_usdc"], z["pnl_usdc"]) == (4.99, 14.99, 13.94)
    assert (z["sold_legs"], z["resting_sells"], z["sell_legs"]) == (1, 1, 2)
    assert z["basket_covered"] and z["holding_hit"]
    day = money["days"][0]
    assert (day["stations"], day["baskets"], day["barred_baskets"]) == (1, 1, 1)
    assert day["day_settled"] and day["pnl_usdc"] == 13.94
    assert (day["settled_stations"], day["hit_stations"]) == (1, 1)
    live = cd._collect_live_orders(_orders_conn())["station_days"]
    assert {r["station_id"] for r in live["rows"]} == {"ZUUU"}
    assert live["rows"][0]["sold_legs"] == 1 and live["rows"][0]["resting_sells"] == 1
