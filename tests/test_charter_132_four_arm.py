"""Charter 132 stage C: the walk-forward that decides a promotion.

What is pinned is what makes the verdict a walk-forward rather than a
reading of the whole record: a fold never trains on its own test day or
after it, never on a label that was not knowable when the test day began,
and never on a decision taken after that; each training station-day counts
once; and the verdict is ``admitted`` only when every condition holds.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import valuation_four_arm as F  # noqa: E402


def _row(station, day, *, known_at=None, at=None, label="a", m=0.5, p=0.5):
    return {
        "station": station, "date": day, "stage": "P0",
        "at": at or f"{day}T06:00:00Z",
        "label": label, "label_known_at": known_at or f"{day}T23:00:00Z",
        "m_label": m, "p_label": p, "brier_m": 0.25, "brier_p": 0.25,
        "m": {"a": m, "b": 1 - m}, "p": {"a": p, "b": 1 - p}, "weight": 1.0,
    }


def test_a_fold_trains_only_on_what_was_knowable_before_its_day(monkeypatch):
    seen = {}

    def spy(_conn, *, stage_scope, rows, meta, draws):
        seen[meta["as_of_event_date"]] = [(r["station"], r["date"]) for r in rows]
        return {
            "accumulating": True, "evidence": {"beta": None, "beta_draws": []},
            "station_day_count": 0,
        }

    monkeypatch.setattr(F.V, "fit_valuation", spy)
    rows = [
        _row("S1", "2026-09-01"),
        # Its label became knowable only after the test day began.
        _row("S2", "2026-09-01", known_at="2026-09-03T01:00:00Z"),
        # Decided before, but for the test day itself.
        _row("S3", "2026-09-02"),
        # After the test day.
        _row("S4", "2026-09-03"),
    ]
    F.fold_fits(rows, ["2026-09-02"], stage="P0")
    assert seen["2026-09-02"] == [("S1", "2026-09-01")]


def test_each_training_station_day_counts_once(monkeypatch):
    weights = {}

    def spy(_conn, *, stage_scope, rows, meta, draws):
        weights.update({(r["station"], r["at"]): r["weight"] for r in rows})
        return {
            "accumulating": True, "evidence": {"beta": None, "beta_draws": []},
            "station_day_count": 0,
        }

    monkeypatch.setattr(F.V, "fit_valuation", spy)
    rows = [
        _row("S1", "2026-09-01", at="2026-09-01T01:00:00Z"),
        _row("S1", "2026-09-01", at="2026-09-01T02:00:00Z"),
        _row("S1", "2026-09-01", at="2026-09-01T03:00:00Z"),
        _row("S2", "2026-09-01"),
    ]
    F.fold_fits(rows, ["2026-09-02"], stage="P0")
    assert sum(w for (s, _a), w in weights.items() if s == "S1") == 1.0
    assert weights[("S2", "2026-09-01T06:00:00Z")] == 1.0


def _information(**over):
    base = {
        "test_days": 20, "station_days": 500,
        "pool_minus_market_log_ci": [0.01, 0.03],
    }
    base.update(over)
    return base


def _strategy(**over):
    pool = {"admitted_station_days": 40, "roi_ci": [0.01, 0.08]}
    pool.update(over)
    return {"arms": {"pool": pool}}


def test_every_condition_met_is_admitted():
    assert F.verdict(_information(), _strategy()) == ("admitted", [])


def test_the_verdict_names_every_condition_it_fails():
    decided, reasons = F.verdict(
        _information(test_days=3, pool_minus_market_log_ci=[-0.01, 0.02]),
        _strategy(admitted_station_days=4, roi_ci=[None, None]),
    )
    assert decided == "hold"
    assert set(reasons) == {
        f"test_days<{F.MIN_TEST_DAYS}",
        "pool_log_gain_over_market_lcb<=0",
        f"admitted_station_days<{F.MIN_ADMITTED_STATION_DAYS}",
        "pool_roi_lcb<=0",
    }


def test_information_alone_does_not_admit():
    """A pool that predicts better but does not pay after the ask holds."""

    decided, reasons = F.verdict(_information(), _strategy(roi_ci=[-0.02, 0.05]))
    assert decided == "hold" and reasons == ["pool_roi_lcb<=0"]


def test_return_alone_does_not_admit():
    """A lucky book without a better forecast behind it holds."""

    decided, reasons = F.verdict(
        _information(pool_minus_market_log_ci=[0.0, 0.01]), _strategy(),
    )
    assert decided == "hold" and reasons == ["pool_log_gain_over_market_lcb<=0"]


def test_a_bootstrap_over_too_few_clusters_says_nothing():
    assert F._cluster_bootstrap({"a": [1.0], "b": [2.0]}, lambda picked: 0.0) == (None, None)
