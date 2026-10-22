"""A tiebreak sale belongs in its realised cash flow, even if the bucket loses."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as dashboard  # noqa: E402
import live_book  # noqa: E402
from bayes_basket_target.tiebreak_lane import RULE_V21, RULE_V23, rule_version_for  # noqa: E402


def _leg(station: str, day: str, intent: str, *, decision: str | None = None,
         bucket: str = "20°C", shares: float = 10.0, cost: float = 5.0) -> dict:
    return {
        "station_id": station, "event_date": day, "bucket": bucket,
        "order_intent_id": intent, "decision_id": decision or f"tiebreak_decision_{station}_{day}",
        "side": "YES", "stage": "P0-M", "filled_size": shares,
        "filled_notional_usdc": cost, "state": "filled", "limit_price": cost / shares,
    }


def _sell(station: str, day: str, *, bucket: str = "20°C",
          shares: float = 10.0, proceeds: float = 9.9, side: str = "YES") -> dict:
    return {
        "station_id": station, "event_date": day, "bucket": bucket,
        "sold_size": shares, "proceeds_usdc": proceeds, "side": side,
    }


def test_sells_are_allocated_to_intents_without_claiming_another_lane() -> None:
    legs = [
        _leg("KATL", "2026-09-01", "tie"),
        _leg("KATL", "2026-09-01", "basket", decision="basket_decision_a"),
    ]
    no_leg = _leg("KATL", "2026-09-01", "proper_no")
    no_leg["side"] = "NO"
    legs.append(no_leg)
    sells = [_sell("KATL", "2026-09-01")]
    assert live_book.allocate_sells_by_intent(legs, sells) == {
        "tie": pytest.approx((5.0, 4.95)),
        "basket": pytest.approx((5.0, 4.95)),
    }
    # External inventory and a NO sale cannot turn into a YES-lane receipt.
    sells = [_sell("KATL", "2026-09-01", shares=25.0, proceeds=24.75),
             _sell("KATL", "2026-09-01", shares=10.0, proceeds=9.9, side="NO")]
    assert live_book.allocate_sells_by_intent(legs, sells) == {
        "tie": pytest.approx((10.0, 9.9)),
        "basket": pytest.approx((10.0, 9.9)),
    }
