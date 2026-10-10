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


def test_the_lanes_file_carries_a_cluster_rule() -> None:
    # The switch itself is the operator's (turned on 2026-10-08 16:20Z); only the rule and dials are pinned.
    # Charter 266 moved the file to v3 (v2's filter, three stake tiers); its own suite pins which.
    root = Path(__file__).resolve().parents[1]
    cfg = strategy_lanes.lane_settings(strategy_lanes.LANE_COPY_FOLLOW, root / "config" / "trading" / "strategy_lanes_v1.json")
    assert cfg["rule"] in lane.CLUSTER_RULES
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
    clause = charter[charter.index("\n257. ") + 1:charter.index("\n258. ")]
    assert "跟单线 v2（cluster_taker_first_filtered_fixed_stake_v2）" in clause
    # 2026-10-09 amendment: NO legs allowed, no daily cap.
    assert "可以买no" in clause and "不设置上限" in clause
    assert "宪章 256/257" in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_a_take_is_sized_on_the_venues_amount_grid() -> None:
    # 2026-10-08 17:01Z: 15.62 shares x 0.32 = 4.9984 USDC was refused ("invalid amounts"); whole shares are legal.
    quote = {"bid": 0.30, "ask": 0.31, "asks": [(0.31, 500.0)]}
    plan = lane.plan_buy(quote, price_cap=0.32, gap=15.62, min_shares=5.0)
    assert plan["order_kind"] == "take" and plan["shares"] == 15.0
    assert abs(plan["shares"] * plan["limit_price"] * 100 - round(plan["shares"] * plan["limit_price"] * 100)) < 1e-6



def test_a_second_wallets_fill_read_later_but_timed_earlier_does_not_add(store) -> None:
    """OEJN 2026-10-08 19:01Z: two cluster wallets bought the same NO bucket a
    second apart; the second wallet's fill (a second EARLIER) reached the tape
    five minutes later, found no admitted trade before it, was admitted too,
    and the leg carried two stakes (16.18 shares for a 5 U stake).  The
    episode window looks both ways, and the target is one stake an episode."""

    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})
    sent: list = []
    first = Cluster([_taker(price=0.30, tx="0x1", at="2026-09-18T12:00:01Z")])
    _run(store, first, sent)
    assert len(_intents(store)) == 1
    later = Cluster([_taker(price=0.30, tx="0x1", at="2026-09-18T12:00:01Z"),
                     _taker(user=W2, price=0.29, at="2026-09-18T12:00:00Z", tx="0x2")])
    _run(store, later, sent, now="2026-09-18T12:07:30.000Z")
    rows = _sources(store)
    assert {r[0]: r[3] for r in rows} == {USER: None, W2: "episode_already_followed"}
    targets = lane.cluster_targets(store, users=[USER, W2], since_event_date="2026-09-01", episode_gap_seconds=3600.0)
    assert len(targets) == 1 and next(iter(targets.values()))["target"] == pytest.approx(5.0 / 0.36, rel=1e-6)
    assert len(_intents(store)) == 1, "the second read adds no second order"


def test_two_admitted_rows_of_one_episode_count_one_stake(store) -> None:
    """Rows admitted before the fix can still carry both wallets; the target
    builder counts the episode's first and nothing within the gap after it,
    while a buy an episode later adds its own stake."""

    _seed(store)
    lane.ensure_lane_schema(store)
    base = dict(source_at_utc=NOW, transaction_hash="0x", token_id="yes_28°C", condition_id="0xc", order_action="BUY",
                outcome_side="YES", size=10.0, usdc_size=3.0, event_slug=SLUG, title="t", station_id=ST,
                event_date=ED, bucket="28°C", settlement_timezone=TZ, ratio=None, our_equity_usdc=None,
                source_value_usdc=None, refusal=None, ingested_at_utc=NOW, taker=1, model_stage="P0",
                our_p=0.45, model_margin=0.15)
    rows = [("k1", USER, 1_000, 0.30, 0.36, round(5 / 0.36, 6)), ("k2", W2, 999, 0.29, 0.35, round(5 / 0.35, 6)),
            ("k3", USER, 1_000 + 7200, 0.31, 0.37, round(5 / 0.37, 6))]
    for key, user, ts, price, cap, scaled in rows:
        body = {**base, "source_key": key, "source_user": user, "source_ts": ts, "price": price,
                "price_cap": cap, "scaled_shares": scaled}
        store.execute(f"INSERT INTO {lane.SOURCE_TABLE}({','.join(body)}) VALUES({','.join('?' for _ in body)})",
                      tuple(body.values()))
    target = lane.cluster_targets(store, users=[USER, W2], since_event_date="2026-09-01", episode_gap_seconds=3600.0)
    assert target["yes_28°C"]["target"] == pytest.approx(5 / 0.35 + 5 / 0.37, rel=1e-6)
    assert target["yes_28°C"]["buy_price"] == 0.31
    summed = lane.cluster_targets(store, users=[USER, W2], since_event_date="2026-09-01")
    assert summed["yes_28°C"]["target"] == pytest.approx(5 / 0.35 + 5 / 0.36 + 5 / 0.37, rel=1e-6), "no gap: legacy sum"


def test_a_cycle_this_lane_left_open_is_settled_by_the_shared_repair() -> None:
    """2026-10-09 00:00Z: a send that died before its intent row (the store
    at its ceiling) left ``copy_follow_cycle_*`` rows at ``submitting``, which
    the market ingress reads as a non-quiescent station-day."""

    from bayes_basket_target import tiebreak_lane

    assert "copy_follow_cycle_" in tiebreak_lane.LANE_CYCLE_PREFIXES


def test_a_null_daily_budget_is_no_cap_and_a_positive_one_still_holds(store) -> None:
    """User ruling 2026-10-09 「40 U 日预算改为不设置上限」: null (the code's
    default now) means no daily cap -- read as zero it would refuse every buy
    -- while a positive cap is still the UTC day's budget."""

    assert strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["max_daily_cost_usdc"] is None
    _seed(store)
    _served(store, {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35})
    sent: list = []
    receipt = _run(store, Cluster([_taker(price=0.30, tx="0x1")]), sent, max_daily_cost_usdc=None, stake_usdc=60.0)
    assert "daily_cost_limit" not in receipt["refusals"] and len(_intents(store)) == 1
    held = _run(store, Cluster([_taker(bucket="29°C", price=0.19, tx="0x2", token="yes_29°C")]), sent,
                max_daily_cost_usdc=1.0, stake_usdc=5.0)
    assert held["refusals"].get("daily_cost_limit") and len(_intents(store)) == 1
