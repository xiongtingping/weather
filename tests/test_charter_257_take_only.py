"""Charter 257 补记 (user ruling 2026-10-09 「3+5」): the copy-follow lane's
margin goes 5 -> 3 cents, its slippage 6 -> 15 cents, and every follow is a
take or nothing -- a FOK at or under the cap inside the episode's window,
never a resting rung, never a chase of an episode whose take did not happen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_charter_256_copy_follow_lane import ED, ST, TZ, _book, _intents, _seed  # noqa: E402
from tests.test_charter_257_copy_follow_v2 import (  # noqa: E402,F401 -- fixtures
    Cluster, _hermetic, _run, _served, _taker,
)
from tests.test_tiebreak_lane import _insert, store  # noqa: E402,F401 -- ``store`` is a fixture

TAKE = {"take_only": True, "take_window_seconds": 1800.0, "max_slippage": 0.15, "model_margin": 0.03}
#: our p(28°C) 0.45 against the source's 0.30: cap = min(0.30 + 0.15, 0.45 - 0.03) = 0.42.
PROBS = {"27°C": 0.2, "28°C": 0.45, "29°C": 0.35}


def test_the_lane_file_carries_three_cents_fifteen_cents_and_take_only() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = strategy_lanes.lane_settings(strategy_lanes.LANE_COPY_FOLLOW,
                                       root / "config" / "trading" / "strategy_lanes_v1.json")
    assert cfg["model_margin"] == 0.03 and cfg["max_slippage"] == 0.15
    assert cfg["take_only"] is True and cfg["take_window_seconds"] == 1800.0
    # The code's own default stays the resting family; only the file turns take-only on.
    assert strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["take_only"] is False


def test_take_only_never_rests() -> None:
    quote = lane.side_quote(_book({"28°C": (0.26, 0.40)})[0], "YES")
    assert lane.plan_buy(quote, price_cap=0.36, gap=15.0, min_shares=5.0, take_only=True) == {
        "refusal": "ask_above_price_cap"}
    assert lane.plan_buy(quote, price_cap=0.36, gap=15.0, min_shares=5.0)["order_kind"] == "rest"
    thin = lane.side_quote(_book({"28°C": (0.26, 0.30)}, ask_shares=3.0)[0], "YES")
    assert lane.plan_buy(thin, price_cap=0.36, gap=15.0, min_shares=5.0, take_only=True) == {
        "refusal": "take_too_thin"}


def test_an_ask_the_old_six_cents_missed_is_taken_under_the_wider_cap(store) -> None:
    # The source paid 0.30; the book asks 0.40 -- over 0.36 (six cents), under 0.42.
    _seed(store, book=_book({"27°C": (0.18, 0.20), "28°C": (0.38, 0.40), "29°C": (0.18, 0.20)}))
    _served(store, PROBS)
    sent: list = []
    _run(store, Cluster([_taker(price=0.30)]), sent, **TAKE)
    (_, payload), = sent
    assert payload["order_type"] == "FOK" and payload["fok_price_cap"] == 0.42


def test_a_take_the_cap_does_not_reach_posts_nothing(store) -> None:
    _seed(store, book=_book({"27°C": (0.18, 0.20), "28°C": (0.43, 0.45), "29°C": (0.18, 0.20)}))
    _served(store, PROBS)
    sent: list = []
    receipt = _run(store, Cluster([_taker(price=0.30)]), sent, **TAKE)
    assert sent == [] and _intents(store) == [] and receipt["refusals"] == {"ask_above_price_cap": 1}


def test_three_cents_admits_what_five_refused(store) -> None:
    _seed(store, book=_book({"27°C": (0.18, 0.20), "28°C": (0.29, 0.31), "29°C": (0.18, 0.20)}))
    _served(store, {"27°C": 0.25, "28°C": 0.34, "29°C": 0.41})   # p(28°C) 0.34 against 0.30
    sent: list = []
    _run(store, Cluster([_taker(price=0.30)]), sent, **{**TAKE, "model_margin": 0.05})
    assert sent == []
    _run(store, Cluster([_taker(price=0.30, tx="0xb", at="2026-09-18T12:01:00Z")]), sent, **TAKE)
    (_, payload), = sent
    assert payload["order_type"] == "FOK" and payload["fok_price_cap"] == pytest.approx(0.31)


def test_the_take_window_closes_and_an_abandoned_episode_is_not_chased(store) -> None:
    _seed(store, book=_book({"27°C": (0.18, 0.20), "28°C": (0.43, 0.45), "29°C": (0.18, 0.20)}))
    _served(store, PROBS)
    sent: list = []
    source = Cluster([_taker(price=0.30, at="2026-09-18T12:00:00Z")])
    _run(store, source, sent, **TAKE)                                 # 0.45 ask: no take
    late = _run(store, source, sent, now="2026-09-18T12:31:00.000Z", **TAKE)
    assert sent == [] and late["refusals"] == {"take_window_passed": 1}
    # A second episode two hours on: one stake, not two.
    _insert(store, "basket_official_ws_snapshot", {
        "snapshot_id": "snap2", "market_id": "m", "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "sequence": 2, "observed_at": "2026-09-18T14:00:30.000Z", "quote_version": "v",
        "quote_observed_at": "2026-09-18T14:00:30.000Z", "quote_expires_at": "2026-09-18T15:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0, "state": "COMMITTED",
        "buckets_json": json.dumps(_book({"27°C": (0.18, 0.20), "28°C": (0.30, 0.32), "29°C": (0.18, 0.20)})),
        "created_at": "2026-09-18T14:00:30.000Z",
    })
    source.trades.append(_taker(price=0.30, at="2026-09-18T14:00:00Z", tx="0xc"))
    _run(store, source, sent, now="2026-09-18T14:01:00.000Z", **TAKE)
    (_, payload), = sent
    assert payload["order_type"] == "FOK"
    assert payload["size"] * payload["price"] <= 5.0 + 1e-6


def test_the_charter_carries_the_ruling() -> None:
    root = Path(__file__).resolve().parents[1]
    clause = next(line for line in (root / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(
        encoding="utf-8").splitlines() if line.startswith("257. "))
    for phrase in ("「3+5」", "take_only", "0.05→0.03", "0.06→0.15", "take_window_passed"):
        assert phrase in clause
    assert "只吃单不挂单" in (root / "AGENTS.md").read_text(encoding="utf-8")
