"""A time-boxed research lane spends its own budget until its end day, then
falls back to the default (user 2026-10-20, tmax M1 ingest)."""

from datetime import datetime, timezone

import pytest

from bayes_basket_target import open_meteo_quota as quota


def test_temporary_lane_budget_applies_until_its_end_day(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    budget, until = quota.TEMPORARY_BUDGET["research_tmax_m1"]
    during = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    quota.charge("research_tmax_m1", budget, during, path=path)
    with pytest.raises(quota.QuotaRefused):
        quota.charge("research_tmax_m1", 1, during, path=path)
    after = datetime.fromisoformat(until + "T01:00:00+00:00")
    quota.charge("research_tmax_m1", quota.DEFAULT_LANE_BUDGET, after, path=path)
    with pytest.raises(quota.QuotaRefused):
        quota.charge("research_tmax_m1", 1, after, path=path)


def test_a_day_budget_overrides_that_day_only(tmp_path) -> None:
    path = tmp_path / "ledger.json"
    (lane, day), budget = next(iter(quota.DAY_BUDGET.items()))
    on = datetime.fromisoformat(day + "T12:00:00+00:00")
    quota.charge(lane, budget, on, path=path)
    with pytest.raises(quota.QuotaRefused):
        quota.charge(lane, 1, on, path=path)
