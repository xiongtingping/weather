"""Charter 268: the copy-follow lane's v4 rule -- the cluster's first taker
YES buys, every one at one flat stake, our probability recorded but never a
gate, no direction tier, no NO legs.

User ruling 2026-10-12 「改」 (on the diagnosis 「跟单线利润没有增长」).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_256_copy_follow_lane import _intents, _seed, _transport  # noqa: E402
from tests.test_charter_257_copy_follow_v2 import (  # noqa: E402
    SETTINGS, W2, Cluster, _hermetic, _served, _taker,
)
from tests.test_tiebreak_lane import store  # noqa: E402,F401 -- ``store`` is a fixture

assert _hermetic  # the v2 suite's autouse fixture, re-armed here

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-18T12:02:30.000Z"
V4 = {**SETTINGS, "rule": lane.RULE_V4, "stake_usdc": 2.0, "max_slippage": 0.15, "take_only": True,
      "max_daily_cost_usdc": None, "sides": ["YES", "NO"],
      # v3's tables stay in the file for a roll-back; v4 must not read them.
      "stake_tiers": {"momentum_agrees": 3.0, "model_agrees": 2.0, "probability": 1.0},
      "big_episode_usdc": 30.0, "big_episode_window_seconds": 60.0, "big_episode_multiplier": 2.0}


def _run(store, trades, *, p=None, now=NOW, sent=None, **overrides):
    _seed(store)
    _served(store, p or {"27°C": 0.40, "28°C": 0.45, "29°C": 0.15})
    sent = [] if sent is None else sent
    receipt = lane.execute_copy_follow(
        store, transport=_transport(sent), now_utc=now, live_submit_authorized=True,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **V4, **overrides}, fetch=Cluster(trades),
        position_reader=lambda token: {"ok": True, "shares": 1e9}, ratio_cache={},
    )
    return receipt, sent


def _sources(store):
    return store.execute(
        f"SELECT token_id, refusal, stake_tier, stake_usdc, scaled_shares, price_cap, our_p, direction_json"
        f" FROM {lane.SOURCE_TABLE} ORDER BY source_ts, source_key"
    ).fetchall()


def test_v4_is_a_cluster_rule_of_its_own() -> None:
    assert lane.RULE_V4 in lane.CLUSTER_RULES and lane.RULE_VERSIONS[lane.RULE_V4] == "copy_follow_lane_v4"
    assert lane.V4_SIDES == {"YES"}


def test_a_yes_leg_our_probability_refuses_is_followed_at_the_flat_stake(store) -> None:
    # Our p(29°C) = 0.15 against the source's 0.20: v2/v3's probability tier refuses it.
    receipt, sent = _run(store, [_taker("29°C", price=0.20, size=40.0)])
    token, refusal, tier, stake, shares, cap, our_p, direction = _sources(store)[0]
    assert refusal is None and tier == lane.TIER_FLAT and stake == pytest.approx(2.0)
    assert cap == pytest.approx(0.35) and shares == pytest.approx(5.714286)
    assert our_p == pytest.approx(0.15) and direction is None  # recorded, never a gate; no direction read
    assert receipt["rule"] == "copy_follow_lane_v4" and receipt["planned"] == 1
    plan = json.loads(_intents(store)[0][6])["plan"]
    assert plan["stake_tier"] == lane.TIER_FLAT and plan["size_multiplier"] == 1.0
    assert plan["our_p"] == pytest.approx(0.15) and plan["price_cap"] == pytest.approx(0.35)
    assert sent and sent[0][1]["order_type"] == "FOK"


def test_a_no_leg_is_refused_by_name_whatever_the_sides_list_says(store) -> None:
    receipt, sent = _run(store, [_taker("28°C", price=0.60, size=40.0, token="no_28°C")])
    assert _sources(store)[0][1] == "side_not_followed:NO"
    assert receipt["planned"] == 0 and not sent


def test_the_flat_stake_is_never_under_the_venue_minimum(store) -> None:
    # 2 U at a 0.45 cap is 4.4 shares: lifted to the five-share minimum.
    _receipt, sent = _run(store, [_taker("28°C", price=0.30, size=40.0)],
                          p={"27°C": 0.40, "28°C": 0.05, "29°C": 0.55})
    assert _sources(store)[0][4] == pytest.approx(5.0)
    assert sent and float(sent[0][1]["size"]) == pytest.approx(5.0)


def test_a_big_episode_is_not_doubled_under_v4(store) -> None:
    trades = [_taker("29°C", price=0.20, size=40.0, tx="0x1"),
              _taker("29°C", user=W2, price=0.21, size=150.0, at="2026-09-18T12:00:30Z", tx="0x2")]
    _receipt, _sent = _run(store, trades)
    plan = json.loads(_intents(store)[0][6])["plan"]
    assert plan["size_multiplier"] == 1.0 and plan["target_shares"] == pytest.approx(5.714286)


def test_episodes_are_taken_in_the_order_they_appeared(store) -> None:
    # One send a pass: the older episode goes first, whatever our probability says of either.
    trades = [_taker("29°C", price=0.20, size=40.0, at="2026-09-18T11:58:00Z", tx="0x1"),
              _taker("27°C", price=0.18, size=40.0, at="2026-09-18T12:00:00Z", tx="0x2")]
    _receipt, sent = _run(store, trades, max_sends_per_pass=1, p={"27°C": 0.60, "28°C": 0.30, "29°C": 0.10})
    assert [row[0] for row in _intents(store)] == ["29°C"]


def test_the_send_records_our_probability_but_never_waits_on_it(store) -> None:
    # Our p is far under the source's price at the send too: v4 takes it anyway.
    _receipt, sent = _run(store, [_taker("28°C", price=0.30, size=40.0)],
                          p={"27°C": 0.48, "28°C": 0.02, "29°C": 0.50})
    rows = store.execute(f"SELECT refusal, our_p, model_margin FROM {lane.EVALUATION_TABLE}").fetchall()
    assert sent and rows[0][0] is None and rows[0][1] == pytest.approx(0.02)
    assert rows[0][2] == pytest.approx(0.02 - 0.30)


def test_the_resident_feed_follows_v4(monkeypatch) -> None:
    import scripts.weather_basket_yes_daemon as daemon

    monkeypatch.setattr("bayes_basket_target.strategy_lanes.lane_settings",
                        lambda name, path=None: {"enabled": True, "rule": lane.RULE_V4,
                                                 "source_users": [SETTINGS["source_users"][0], W2]})
    assert daemon.copy_follow_feed_users()


def test_a_no_leg_admitted_under_an_earlier_rule_is_not_sent_under_v4(store) -> None:
    # Admitted under v2 (our NO p 0.80 clears 0.60 + 0.05), held back by a zero send limit ...
    _seed(store)
    _served(store, {"27°C": 0.20, "28°C": 0.20, "29°C": 0.60})
    trade = _taker("28°C", price=0.60, size=40.0, token="no_28°C")
    lane.execute_copy_follow(
        store, transport=_transport([]), now_utc=NOW, live_submit_authorized=True,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS, "take_only": True,
                  "max_sends_per_pass": 0}, fetch=Cluster([trade]),
        position_reader=lambda token: {"ok": True, "shares": 1e9}, ratio_cache={},
    )
    assert _sources(store)[0][1] is None
    # ... and the next pass, under v4 and inside the take window, refuses it by name.
    sent: list = []
    lane.execute_copy_follow(
        store, transport=_transport(sent), now_utc="2026-09-18T12:03:30.000Z", live_submit_authorized=True,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **V4}, fetch=Cluster([trade]),
        position_reader=lambda token: {"ok": True, "shares": 1e9}, ratio_cache={},
    )
    last = store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE} ORDER BY evaluated_at_utc DESC").fetchone()
    assert last[0] == "side_not_followed:NO" and not sent
