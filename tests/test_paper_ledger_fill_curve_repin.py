"""The maker fill curves re-pin weekly, and only on an out-of-sample pass.

User ruling 2026-09-25: 「每周自动（样本外核对过线才换）」.  A candidate is
fitted on the orders before the last week's event days and scored, with the
curves in service, on that week's orders -- censored-data Brier and log loss
at the rung-life horizons.  It replaces the curves only when it is no worse
on both; one decided attempt stands for a week.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_ledger_fill_curves as F  # noqa: E402

AS_OF = "2026-10-02T04:10:00Z"


def _orders(*, days, per_day, fill_rate, seed, start="2026-09-01"):
    """Orders whose first fill (if any) comes at 20 minutes; the rest are
    cancelled at 2 hours.  Every cell alike, so one rate describes them."""

    rng = random.Random(seed)
    rows = []
    first = date.fromisoformat(start)
    for d in range(days):
        day = (first + timedelta(days=d)).isoformat()
        for k in range(per_day):
            filled = rng.random() < fill_rate
            rows.append({
                "intent_id": f"{day}-{k}", "station_id": "ZUUU", "event_date": day,
                "placed_at_utc": f"{day}T01:00:00Z", "bucket": "25°C",
                "won": k % 2 == 0, "lead_band": ("in_day", "d1", "d2plus")[k % 3],
                "filled": filled, "seconds": 1200.0 if filled else 7200.0,
            })
    return rows


def _incumbent(fill_rate: float) -> dict:
    """Curves in service that say every cell fills at ``fill_rate`` by 20m."""

    curve = {"t_seconds": [1200.0], "F": [fill_rate], "orders": 100, "fills": 0,
             "rule": "cell", "earliest_event_date": "2026-09-01"}
    cells = {f"{band}|{kind}": dict(curve) for band, _u in F.LEAD_BANDS for kind in ("won", "lost", "any")}
    return F._sealed({"schema": F.SCHEMA, "curves": cells})


def test_the_scores_reward_the_curve_nearer_the_truth() -> None:
    rows = _orders(days=7, per_day=12, fill_rate=0.6, seed=1)
    near = F.censored_scores(_incumbent(0.6)["curves"], rows)
    far = F.censored_scores(_incumbent(0.1)["curves"], rows)
    assert near["brier"] < far["brier"] and near["log_loss"] < far["log_loss"]


def test_an_order_cancelled_before_the_horizon_says_nothing_about_it() -> None:
    # One order filled at 20m, one cancelled at 10m: at the 30m horizon only
    # the first is evidence, so a curve's fate there hangs on it alone.
    rows = [
        {"lead_band": "in_day", "won": True, "filled": True, "seconds": 1200.0, "event_date": "d"},
        {"lead_band": "in_day", "won": True, "filled": False, "seconds": 600.0, "event_date": "d"},
    ]
    certain = F.censored_scores(_incumbent(0.999999)["curves"], rows, horizons=(1800.0,))
    assert certain["brier"] == pytest.approx(0.0, abs=1e-6)


def test_a_candidate_that_scores_better_out_of_sample_is_installed() -> None:
    # Fill rate 0.6 throughout; the curves in service say 0.1.
    rows = _orders(days=31, per_day=12, fill_rate=0.6, seed=2)
    receipt, installed = F.decide_repin(rows, _incumbent(0.1), as_of_utc=AS_OF)
    assert receipt["verdict"] == "installed" and installed is not None
    assert receipt["holdout_event_dates"] == ["2026-09-25", "2026-10-01"]
    # The candidate never saw the week it was judged on.
    assert installed["trained_through_event_date"] == "2026-09-24"
    gate = installed["out_of_sample_gate"]
    assert gate["passed"] is True
    assert gate["candidate"]["brier"] <= gate["incumbent"]["brier"]
    assert gate["candidate"]["log_loss"] <= gate["incumbent"]["log_loss"]
    assert installed["content_sha256"] == F._sealed(installed)["content_sha256"]


def test_a_candidate_no_better_than_the_curves_in_service_is_not() -> None:
    rows = _orders(days=31, per_day=12, fill_rate=0.6, seed=3)
    served = F.build_artifact(rows, fit_from="2026-09-01")  # fitted on everything
    receipt, installed = F.decide_repin(rows, served, as_of_utc=AS_OF)
    assert installed is None
    assert receipt["verdict"] == "kept"
    assert receipt["candidate"] and receipt["incumbent"]


def test_a_thin_week_decides_nothing() -> None:
    rows = _orders(days=24, per_day=12, fill_rate=0.6, seed=4)  # nothing after 09-24
    receipt, installed = F.decide_repin(rows, _incumbent(0.1), as_of_utc=AS_OF)
    assert installed is None
    assert receipt["verdict"] == "accumulating"
    assert receipt["holdout_orders"] < F.MIN_HOLDOUT_ORDERS


def test_one_decision_stands_for_a_week_and_a_thin_week_retries() -> None:
    decided = [{"attempted_at_utc": "2026-09-25T04:10:00Z", "verdict": "seeded"}]
    assert F.repin_due(decided, as_of_utc="2026-10-01T23:00:00Z") is False
    assert F.repin_due(decided, as_of_utc="2026-10-02T04:10:00Z") is True
    retry = decided + [{"attempted_at_utc": "2026-10-02T04:10:00Z", "verdict": "accumulating"}]
    assert F.repin_due(retry, as_of_utc="2026-10-03T04:10:00Z") is True
    assert F.repin_due([], as_of_utc=AS_OF) is True


def test_the_weekly_step_writes_the_curves_only_on_a_pass(tmp_path, monkeypatch) -> None:
    curves = tmp_path / "curves.json"
    receipts = tmp_path / "repin.jsonl"
    curves.write_text(json.dumps(_incumbent(0.1)))
    rows = _orders(days=31, per_day=12, fill_rate=0.6, seed=5)
    monkeypatch.setattr(F, "real_basket_maker_orders", lambda _ssot: rows)
    first = F.weekly_repin(None, as_of_utc=AS_OF, curves_path=curves, receipts_path=receipts)
    assert first["verdict"] == "installed"
    assert json.loads(curves.read_text())["content_sha256"] == first["candidate_sha256"]
    again = F.weekly_repin(None, as_of_utc="2026-10-03T04:10:00Z", curves_path=curves, receipts_path=receipts)
    assert again["verdict"] == "not_due"
    assert len(receipts.read_text().splitlines()) == 1
