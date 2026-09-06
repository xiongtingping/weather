"""Every P0 seat is won; nothing is granted, and the forecaster never runs.

The provisional equal-family-share seat for ICON was removed by the
2026-08-15 ruling, which clarified the earlier "must not sit at zero weight"
as *eligibility*, not a mandate: a system without an exact shared holdout
accumulates its forward record at zero weight until it can compete.  The
graft's fingerprint -- ICON at exactly 1/n while its peers carried fitted
weights -- appeared on every selection row it touched, which is what a
mandate looks like.

The same ruling fixed the forecaster's stage: TAF TX is P0-M and envelope
evidence, never a P0 combination member.  Its per-system evaluation lane
stays; its candidacy in the cross-system subset contest does not.
"""

from __future__ import annotations

import inspect

import bayes_basket_target.native_ensemble_oos as oos


def test_the_provisional_seat_is_gone_entirely() -> None:
    assert not hasattr(oos, "_provisional_family_share_admission")
    assert not hasattr(oos, "PROVISIONAL_CROSS_ENSEMBLE_SYSTEM_ID")


def test_ready_rows_exclude_point_forecasts_from_the_contest() -> None:
    # The exclusion must sit in the cohort assembly itself, before the
    # minimum-two check, so a point forecast can never even pad the count.
    source = inspect.getsource(oos.build_native_ensemble_oos)
    ready = source.split("ready_rows = [", 1)[1][:900]
    assert "OOS_READY" in ready
    # Charter 95 (2026-09-06): the exclusion is the one predicate that keeps
    # the TAF TX and the METAR path out while the regional deterministic
    # models may contest.
    assert "is_fusion_excluded_point_system(system_id)" in ready


def test_certified_weights_are_the_served_weights() -> None:
    # With no admission path after the contest, the two persisted sets can
    # no longer diverge; the fields remain so old rows stay interpretable.
    source = inspect.getsource(oos.build_native_ensemble_oos)
    assert "certified_weights = dict(weights)" in source
    assert "provisional_system_ids: list[str] = []" in source
    assert "_provisional_family_share_admission" not in source
