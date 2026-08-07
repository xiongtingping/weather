"""The rotation sweep must finish inside the P0 freshness TTL.

One `refresh_resident_nwp` call advances the rotation by exactly one scope, so
the full sweep costs `scope_count / scopes_per_poll * poll_interval`.  At one
scope per poll that was 77 x 10min = 12.8h against a ~6h TTL: every scope's
probability expired before its turn came round again, all scopes went
`fresh=False`, and the mainline fail-closed on
`resident_target_ws_actionable_scope_missing` with no new P0 for hours.

This is a budget invariant, not a style preference -- if a later change raises
the scope count or the poll interval, the sweep silently stops keeping up.
"""

import pytest

from bayes_basket_target.production_worker import (
    OFFICIAL_NATIVE_CAPTURE_DEADLINE_SEC,
    OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY,
)
from scripts.weather_basket_yes_daemon import (
    TARGET_NWP_POLL_INTERVAL_SEC,
    TARGET_NWP_SCOPES_PER_POLL,
)


# Observed in production: decision_at 18:22Z, fresh_until 00:19Z.
OBSERVED_P0_TTL_HOURS = 5.95
# 45 stations across the open event days currently in the forward registry.
OBSERVED_SCOPE_COUNT = 77


def _sweep_hours(scope_count: int) -> float:
    polls = scope_count / TARGET_NWP_SCOPES_PER_POLL
    return polls * TARGET_NWP_POLL_INTERVAL_SEC / 3600.0


def test_sweep_fits_inside_the_observed_p0_ttl() -> None:
    assert _sweep_hours(OBSERVED_SCOPE_COUNT) < OBSERVED_P0_TTL_HOURS


def test_sweep_keeps_headroom_rather_than_only_just_fitting() -> None:
    # A sweep that only just fits leaves nothing for a slow poll or a capture
    # deadline, and scopes start expiring again at the first hiccup.
    assert _sweep_hours(OBSERVED_SCOPE_COUNT) < OBSERVED_P0_TTL_HOURS * 0.85


@pytest.mark.parametrize("scope_count", [45, 60, 77, 90])
def test_sweep_fits_across_plausible_scope_counts(scope_count: int) -> None:
    # Scope count grows with the number of open event days; the budget must
    # not depend on today's exact catalogue.
    assert _sweep_hours(scope_count) < OBSERVED_P0_TTL_HOURS


def test_more_than_one_scope_advances_per_poll() -> None:
    assert TARGET_NWP_SCOPES_PER_POLL > 1


def test_captures_run_concurrently_off_the_rotation_thread() -> None:
    # Captures no longer occupy the rotation, so coverage growth is bounded by
    # concurrency rather than by how rarely a capture may start.
    assert OFFICIAL_NATIVE_CAPTURE_MAX_CONCURRENCY > 1


def test_capture_deadline_stays_hard_bounded() -> None:
    # Even off-thread, a capture must not run unbounded: the pool has a fixed
    # number of slots and a wedged child would hold one forever.
    assert 0 < OFFICIAL_NATIVE_CAPTURE_DEADLINE_SEC <= TARGET_NWP_POLL_INTERVAL_SEC
