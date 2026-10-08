"""Charter 257: the copy-follow lane's v2 rule -- the eight-wallet cluster's
first taker buy of a bucket episode, followed at a fixed stake where our own
served probability clears the source's price by a margin.

User ruling 2026-10-09 「开实盘，利用现有的跟单策略，但是我的资金量有限，如何跟单」.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_256_copy_follow_lane import (  # noqa: E402
    ED, LADDER, NOW, SLUG, ST, START, TZ, USER, _book, _intents, _seed, _transport, _ts,
)
from tests.test_tiebreak_lane import _insert, store  # noqa: E402,F401 -- ``store`` is a fixture

W2 = "0x6fdd2bda5c031d551f588f176535f061a29a0b07"
#: NOW is 2026-09-18T12:02Z; the event day is 2026-09-20 Asia/Shanghai, noon = 04:00Z -> ~40h lead.
SETTINGS = {"enabled": True, "live_ws_book": False, "follow_from_utc": START, "rule": lane.RULE_V2,
            "source_users": [USER, W2], "stake_usdc": 5.0, "model_margin": 0.05, "max_slippage": 0.06,
            "lead_hours": [12.0, 48.0], "station_units": ["C"], "model_stages": ["P0", "P0-M"],
            "episode_gap_seconds": 3600.0, "max_source_age_seconds": 0.0, "max_daily_cost_usdc": 40.0}


def _served(conn, probs, *, stage="P0", decided="2026-09-18T11:30:00Z", hid="hist1"):
    _insert(conn, "basket_probability_bridge_history", {
        "history_id": hid, "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "stage": stage,
        "checkpoint_id": f"cp_{hid}", "decision_at_utc": decided, "fresh_until_utc": "2026-09-19T00:00:00Z",
        "bucket_intervals_json": "{}", "envelope_json": "{}", "state": "COMMITTED", "created_at_utc": decided,
        "content_sha256": (hid + "c").ljust(64, "0"), "envelope_content_sha256": (hid + "e").ljust(64, "0"),
        "bridge_sha256": (hid + "b").ljust(64, "0"), "bucket_map_sha256": (hid + "m").ljust(64, "0"),
        "projection_sha256": (hid + "p").ljust(64, "0"),
    })
    for i, (bucket, p) in enumerate(probs.items()):
        _insert(conn, "basket_probability_bridge_bucket_probability", {
            "history_id": hid, "bucket": bucket, "ordinal": i, "lower_c": 27.0 + i - 0.5, "upper_c": 27.0 + i + 0.5,
            "probability": p, "content_sha256": (hid + bucket).encode().hex().ljust(64, "0")[:64],
        })


def _taker(bucket="28°C", *, user=USER, side="BUY", size=40.0, price=0.30, at="2026-09-18T12:00:00Z",
           token=None, tx="0xa"):
    return {"timestamp": _ts(at), "asset": token or f"yes_{bucket}", "side": side, "size": size, "price": price,
            "transactionHash": tx, "eventSlug": SLUG, "conditionId": "0xc", "title": f"Shanghai {bucket}",
            "proxyWallet": user}


class Cluster:
    """The data API as v2 reads it: each wallet's taker fills, newest first."""

    def __init__(self, trades=()):
        self.trades = list(trades)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        assert path == "/trades" and params["takerOnly"] == "true"
        rows = sorted((t for t in self.trades if t["proxyWallet"] == params["user"]),
                      key=lambda t: -t["timestamp"])
        return rows[int(params["offset"]): int(params["offset"]) + int(params["limit"])]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr(lane, "_TOKEN_CACHE", {})
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})
    monkeypatch.setattr("bayes_basket_target.exit_orders.egress_send_gate", lambda: {"hold": False})
    monkeypatch.setattr("bayes_basket_target.exit_orders.live_intent_armed", lambda: True)


def _run(conn, source, sent, *, now=NOW, fill=True, **overrides):
    return lane.execute_copy_follow(
        conn, transport=_transport(sent, fill=fill), now_utc=now, live_submit_authorized=True,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS, **overrides}, fetch=source,
        position_reader=lambda token: {"ok": True, "shares": 1e9}, ratio_cache={},
    )


def _sources(conn):
    return conn.execute(
        f"SELECT source_user, token_id, order_action, refusal, scaled_shares, our_p, price_cap, taker"
        f" FROM {lane.SOURCE_TABLE} ORDER BY source_ts, source_key"
    ).fetchall()


def test_the_defaults_name_the_cluster_and_the_stake_dial() -> None:
    cfg = strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS
    assert cfg["rule"] == lane.RULE_V1 and lane.RULE_V2 in lane.RULE_VERSIONS
    assert len(cfg["source_users"]) == 8 and USER in cfg["source_users"] and W2 in cfg["source_users"]
    assert cfg["stake_usdc"] == 5.0 and cfg["model_margin"] == 0.05 and cfg["station_units"] == ["C"]
    assert strategy_lanes.LANE_COPY_FOLLOW in strategy_lanes.STAKE_DIALS


def test_the_lanes_file_carries_v2() -> None:
    # The switch itself is the operator's (turned on 2026-10-08 16:20Z); only the rule and dials are pinned.
    root = Path(__file__).resolve().parents[1]
    cfg = strategy_lanes.lane_settings(strategy_lanes.LANE_COPY_FOLLOW, root / "config" / "trading" / "strategy_lanes_v1.json")
    assert cfg["rule"] == lane.RULE_V2
    assert cfg["mirror_sells"] is False and len(cfg["source_users"]) == 8


def test_taker_fills_are_read_back_to_the_cursor_oldest_first() -> None:
    src = Cluster([_taker(at="2026-09-18T11:00:00Z", tx="0x1"), _taker(at="2026-09-18T11:30:00Z", tx="0x2"),
                   _taker(at="2026-09-18T12:00:00Z", tx="0x3")])
    rows = lane.fetch_source_taker_trades(USER, start_ts=_ts("2026-09-18T11:15:00Z"), fetch=src)
    assert [r["transactionHash"] for r in rows] == ["0x2", "0x3"]
    assert all(p["takerOnly"] == "true" for _, p in src.calls)


def test_a_first_taker_buy_our_model_backs_is_admitted_at_the_fixed_stake(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})   # our p(28°C)=0.45 vs source price 0.30
    sent: list = []
    receipt = _run(store, Cluster([_taker(price=0.30)]), sent)
    rows = _sources(store)
    assert len(rows) == 1 and rows[0][3] is None and rows[0][7] == 1
    cap = min(0.30 + 0.06, 0.45 - 0.05)                      # 0.36
    assert rows[0][6] == pytest.approx(cap) and rows[0][4] == pytest.approx(round(5.0 / cap, 6))
    assert receipt["rule"] == "copy_follow_lane_v2" and receipt["ingest"]["admitted"] == 1
    # the book asks 0.32 for 28°C: at or under the cap -> taken FOK, capped at the cap
    intents = _intents(store)
    assert len(intents) == 1 and intents[0][3] == "BUY" and intents[0][0] == "28°C"
    assert json.loads(intents[0][6])["rule"] == "copy_follow_lane_v2"
    assert sent and sent[0][1]["order_type"] == "FOK"


def test_sells_maker_sources_f_stations_and_late_leads_are_refused_by_name(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})
    sent: list = []
    # a SELL is not followed; a lead past the window is refused
    _run(store, Cluster([_taker(side="SELL", tx="0xs")]), sent)
    assert _sources(store)[0][3] == "action_not_followed:SELL"
    late = "2026-09-20T02:00:00Z"                               # 2h before the event's local noon
    _run(store, Cluster([_taker(at=late, tx="0xl")]), sent, now="2026-09-20T02:01:00Z")
    assert any(str(r[3]).startswith("lead_outside_window") for r in _sources(store))
    assert not sent


def test_an_f_station_is_outside_the_units_followed(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})
    store.execute("UPDATE basket_target_market_registry SET settlement_unit='F'")
    sent: list = []
    _run(store, Cluster([_taker(bucket="29°C", tx="0xf")]), sent)
    assert [r[3] for r in _sources(store)] == ["station_unit_not_followed:F"] and not sent


def test_our_model_must_clear_the_source_price_by_the_margin(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.4, "28°C": 0.33, "29°C": 0.27})   # p(28°C)=0.33 < 0.30 + 0.05
    sent: list = []
    _run(store, Cluster([_taker(price=0.30)]), sent)
    rows = _sources(store)
    assert len(rows) == 1 and str(rows[0][3]).startswith("model_margin_short:0.330<0.350")
    assert not sent


def test_a_second_wallet_on_the_same_bucket_within_the_gap_does_not_add(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})
    sent: list = []
    _run(store, Cluster([_taker(price=0.30, tx="0x1"), _taker(user=W2, price=0.31, at="2026-09-18T12:01:00Z", tx="0x2")]), sent)
    rows = _sources(store)
    assert {r[0]: r[3] for r in rows} == {USER: None, W2: "episode_already_followed"}
    assert len(_intents(store)) == 1


def test_the_pass_ranks_candidates_by_our_margin(store) -> None:
    _seed(store)
    _served(store, {"27°C": 0.40, "28°C": 0.45, "29°C": 0.15})   # 27°C: p 0.40 vs price 0.19 -> margin 0.21; 28°C: 0.15
    sent: list = []
    receipt = _run(store, Cluster([_taker(bucket="28°C", price=0.30, tx="0x1"),
                                   _taker(bucket="27°C", price=0.19, tx="0x2", at="2026-09-18T12:00:30Z")]),
                   sent, max_sends_per_pass=1)
    assert receipt["planned"] == 1 and receipt["receipts"][0]["bucket"] == "27°C"


def test_the_charter_and_agents_carry_the_ruling() -> None:
    root = Path(__file__).resolve().parents[1]
    charter = (root / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    assert "257. **跟单线 v2" in charter and "cluster_taker_first_filtered_fixed_stake_v2" in charter
    assert "宪章 256/257" in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_a_take_is_sized_on_the_venues_amount_grid() -> None:
    # 2026-10-08 17:01Z: 15.62 shares x 0.32 = 4.9984 USDC was refused ("invalid amounts"); whole shares are legal.
    quote = {"bid": 0.30, "ask": 0.31, "asks": [(0.31, 500.0)]}
    plan = lane.plan_buy(quote, price_cap=0.32, gap=15.62, min_shares=5.0)
    assert plan["order_kind"] == "take" and plan["shares"] == 15.0
    assert abs(plan["shares"] * plan["limit_price"] * 100 - round(plan["shares"] * plan["limit_price"] * 100)) < 1e-6

