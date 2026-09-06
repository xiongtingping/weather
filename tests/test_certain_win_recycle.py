"""The 0.998 recycle lane: won positions sell, everything else is left alone.

Operator ruling 2026-08-31: 确定赢的仓位提前 0.2% 折价回笼资金再周转.  "Won"
is measured -- local settlement day closed, canonical observed maximum inside
a held bucket -- because a mid-day high can still leave the bucket and a
closed day cannot.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bayes_basket_target.certain_win_recycle import (
    RECYCLE_PRICE,
    execute_certain_win_recycles,
    plan_certain_win_recycles,
)
from weather_basket_yes_schema import apply_weather_basket_yes_schema

# A moment when Shanghai's 2026-08-29 has long closed and 2026-08-31 has not.
NOW = "2026-08-30T18:00:00Z"


def _target_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "target.db")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        " station_id TEXT, local_date TEXT, settlement_timezone TEXT,"
        " official_tmax_c REAL, source_lane TEXT, qc_status TEXT,"
        " authority_scope TEXT, revision_no INTEGER,"
        " observation_ids_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_confirmed_inventory_snapshot("
        " inventory_snapshot_id TEXT, station_id TEXT, event_date TEXT,"
        " inventory_version TEXT, observed_at TEXT, complete INTEGER,"
        " inventory_json TEXT, content_sha256 TEXT, state TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        " station_id TEXT, event_date TEXT, outcome TEXT, yes_token TEXT,"
        " source_eligible INTEGER, registry_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_target_order_intents("
        " order_intent_id TEXT PRIMARY KEY, decision_id TEXT, cycle_id TEXT,"
        " bucket TEXT, token_id TEXT, side TEXT, order_action TEXT,"
        " limit_price REAL, requested_size REAL, requested_notional_usdc REAL,"
        " state TEXT, live_submit_authorized INTEGER, idempotency_key TEXT,"
        " intent_json TEXT, intent_sha256 TEXT, created_at TEXT,"
        " target_execution_hard_deadline_at TEXT)"
    )
    return conn


def _canonical_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "canonical.db")
    conn.execute(
        "CREATE TABLE reported_trajectory_canonical_station_day_bounds("
        " station_id TEXT, event_date TEXT, settlement_timezone TEXT,"
        " observed_max_c REAL, max_observation_id TEXT)"
    )
    return conn


def _seed(
    target: sqlite3.Connection,
    canonical: sqlite3.Connection,
    *,
    station: str = "ZSPD",
    event_date: str = "2026-08-29",
    timezone_name: str = "Asia/Shanghai",
    held_bucket: str = "35°C",
    shares: float = 12.5,
    high_c: float = 35.2,
    lower: float = 35.0,
    upper: float = 35.9,
) -> None:
    target.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES"
        " ('inv1',?,?, 'v1', ?, 1, ?, 'c'*1, 'ok', ?)",
        (
            station, event_date, NOW,
            json.dumps({"filled_shares_by_bucket": {held_bucket: shares}}),
            NOW,
        ),
    )
    target.execute(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?,?,1,?)",
        (
            station, event_date, held_bucket, "777777",
            json.dumps({"lower": lower, "upper": upper}),
        ),
    )
    # Charter 100: the settled high is what the recycler reads; the trajectory
    # bound stays in the fixture because the timezone still falls back to it.
    target.execute(
        "INSERT INTO calibration_station_daily_high_truth"
        "(station_id,local_date,settlement_timezone,official_tmax_c,"
        " source_lane,qc_status,authority_scope,revision_no,"
        " observation_ids_json)"
        " VALUES(?,?,?,?,'AWC','ACCEPTED','FORMAL_NATIVE_ELIGIBLE',1,'[\"obs1\"]')",
        (station, event_date, timezone_name, high_c),
    )
    canonical.execute(
        "INSERT INTO reported_trajectory_canonical_station_day_bounds"
        " VALUES (?,?,?,?,'obs1')",
        (station, event_date, timezone_name, high_c),
    )


def test_a_closed_day_with_the_high_in_a_held_bucket_sells_at_998(tmp_path):
    target, canonical = _target_db(tmp_path), _canonical_db(tmp_path)
    _seed(target, canonical)

    plans = plan_certain_win_recycles(target, canonical, now_utc=NOW)

    assert len(plans) == 1
    plan = plans[0]
    assert plan["limit_price"] == RECYCLE_PRICE == 0.998
    assert plan["shares"] == 12.5
    assert plan["bucket"] == "35°C"
    assert plan["order_intent_id"].startswith("recycle998_")


def test_everything_short_of_certainty_is_left_alone(tmp_path):
    cases = {
        # The day is still open: an afternoon record can leave the bucket.
        "open_day": {"event_date": "2026-08-31"},
        # The high closed outside the held bucket: this position lost, and a
        # lost position at 0.998 would be a gift to whoever lifts it.
        "high_below": {"high_c": 33.0},
        "high_above": {"high_c": 36.5},
        # Nothing held.
        "no_shares": {"shares": 0.0},
    }
    for label, overrides in cases.items():
        (tmp_path / label).mkdir(exist_ok=True)
        target = _target_db(tmp_path / label)
        canonical = _canonical_db(tmp_path / label)
        _seed(target, canonical, **overrides)
        plans = plan_certain_win_recycles(target, canonical, now_utc=NOW)
        assert plans == [], label


def test_execution_mints_a_live_sell_intent_and_is_idempotent(tmp_path):
    target, canonical = _target_db(tmp_path), _canonical_db(tmp_path)
    _seed(target, canonical)
    sent: list[dict] = []

    def transport(path, payload):
        sent.append({"path": path, **dict(payload)})
        return {"ok": True, "accepted": True}

    receipt = execute_certain_win_recycles(
        target, canonical, transport=transport, now_utc=NOW,
        live_submit_authorized=True,
    )

    assert receipt["planned"] == 1 and receipt["submitted"] == 1
    assert sent[0]["path"] == "/place-order"
    assert sent[0]["side"] == "SELL"
    assert sent[0]["order_type"] == "GTC"
    assert sent[0]["post_only"] is True
    assert sent[0]["price"] == 0.998

    row = target.execute(
        "SELECT side, order_action, state, live_submit_authorized"
        " FROM basket_target_order_intents"
    ).fetchone()
    # side is the outcome side, not the direction -- the direction is
    # order_action, and the no-NO trigger on the real table only checks side.
    assert row == ("YES", "SELL", "submitted", 1)

    # Second cycle: the deterministic id makes replanning a no-op.
    again = execute_certain_win_recycles(
        target, canonical, transport=transport, now_utc=NOW,
        live_submit_authorized=True,
    )
    assert again["planned"] == 0
    assert len(sent) == 1


def test_without_authority_nothing_reaches_the_transport(tmp_path):
    target, canonical = _target_db(tmp_path), _canonical_db(tmp_path)
    _seed(target, canonical)
    receipt = execute_certain_win_recycles(
        target, canonical,
        transport=lambda *_: (_ for _ in ()).throw(AssertionError("must not send")),
        now_utc=NOW, live_submit_authorized=False,
    )
    assert receipt["planned"] == 1 and receipt["submitted"] == 0
    row = target.execute(
        "SELECT state, live_submit_authorized FROM basket_target_order_intents"
    ).fetchone()
    assert row == ("not_authorized", 0)
