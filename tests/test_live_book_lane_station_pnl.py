"""Each lane's settled cash P&L per station, and the lanes add up to the station.

User ruling 2026-10-05: 去掉下单与成交列、命中列、模型声称列，按照车道分别
统计盈亏.  A station's money was one sum over every lane that bought there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import live_book  # noqa: E402


def _leg(intent, decision, bucket, *, size, notional, side="YES", station="KLGA", day="2026-10-01"):
    return {
        "order_intent_id": intent, "decision_id": decision, "station_id": station,
        "event_date": day, "bucket": bucket, "filled_size": float(size),
        "filled_notional_usdc": float(notional), "side": side,
    }


def test_lanes_are_read_off_the_decision_id() -> None:
    assert live_book.intent_lane("decision_abc") == "basket"
    assert live_book.intent_lane("proper_bet_decision_ZSQD_2026-10-05") == "proper_bet"
    assert live_book.intent_lane("tail_short_decision_KLGA_2026-10-05") == "tail_short"
    assert live_book.intent_lane("p1_consensus_decision_KLGA_2026-10-05") == "p1_consensus"


def test_each_lane_is_scored_on_its_own_legs_and_they_sum_to_the_station() -> None:
    legs = [
        # Proper: YES on the truth bucket, 5 shares for 2.5.
        _leg("p1", "proper_bet_decision_KLGA_2026-10-01", "70-71", size=5, notional=2.5),
        # Tiebreak: YES on a miss, 4 for 1.6, two of them sold back for 0.5.
        _leg("t1", "tiebreak_decision_KLGA_2026-10-01", "72-73", size=4, notional=1.6),
        # Far tail: NO on a far bucket, 10 for 9.5, half sold at 0.99.
        _leg("n1", "tail_short_decision_KLGA_2026-10-01", "80+", size=10, notional=9.5, side="NO"),
        # Another day, not settled yet.
        _leg("p2", "proper_bet_decision_KLGA_2026-10-02", "70-71", size=5, notional=2.0, day="2026-10-02"),
        # Unfilled: not a leg of the money.
        _leg("b1", "decision_x", "70-71", size=0, notional=0),
    ]
    sells = [
        {"station_id": "KLGA", "event_date": "2026-10-01", "bucket": "72-73",
         "sold_size": 2.0, "proceeds_usdc": 0.5, "side": "YES"},
        {"station_id": "KLGA", "event_date": "2026-10-01", "bucket": "80+",
         "sold_size": 5.0, "proceeds_usdc": 4.95, "side": "NO"},
    ]
    book = {"legs": legs, "exit_sells": sells,
            "settled": {("KLGA", "2026-10-01"): ("70-71", "truth")}}
    out = live_book.lane_station_pnl(book)
    lanes = out["by_station"]["KLGA"]
    assert lanes["proper_bet"]["pnl_usdc"] == pytest.approx(5 - 2.5)
    assert lanes["proper_bet"]["settled_legs"] == 1 and lanes["proper_bet"]["pending_legs"] == 1
    assert lanes["tiebreak"]["pnl_usdc"] == pytest.approx(0.5 - 1.6)
    assert lanes["tiebreak"]["sell_proceeds_usdc"] == pytest.approx(0.5)
    # NO: 5 kept shares pay (truth elsewhere) + 4.95 from the sale.
    assert lanes["tail_short"]["pnl_usdc"] == pytest.approx(5 + 4.95 - 9.5)
    assert "basket" not in lanes
    total = sum(x["pnl_usdc"] for x in lanes.values())
    assert total == pytest.approx(2.5 - 1.1 + 0.45)
    assert out["by_lane"]["tail_short"]["roi"] == pytest.approx(0.45 / 9.5, rel=1e-4)


def test_a_no_leg_on_the_truth_bucket_loses_what_it_kept() -> None:
    legs = [_leg("n1", "tail_short_decision_KLGA_2026-10-01", "70-71", size=10, notional=9.0, side="NO")]
    book = {"legs": legs, "exit_sells": [],
            "settled": {("KLGA", "2026-10-01"): ("70-71", "truth")}}
    lane = live_book.lane_station_pnl(book)["by_station"]["KLGA"]["tail_short"]
    assert lane["pnl_usdc"] == pytest.approx(-9.0)
    assert lane["won_legs"] == 0
