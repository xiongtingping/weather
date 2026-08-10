"""A blocked scope must not end the poll.

The sweep budget in ``test_nwp_rotation_sweep_budget`` proves the *constants*
allow a full rotation inside the P0 freshness TTL.  It cannot see the advance
loop, and the loop used to stop at the first scope whose state began with
"blocked" -- which reinstated the one-scope-per-poll sweep the budget exists to
prevent, from a source the budget never considered.

Observed 2026-08-10: each scope tries its official native packet first and only
pays Open-Meteo when that is unavailable, so an exhausted Open-Meteo daily
quota blocked the first such scope and ended every poll there.  Official
captures are kicked off inside each scope's own turn, so the scopes behind the
blocker never submitted theirs.  The official inbox went unwritten for six
hours, all 72 packets aged past their 6h freshness, and both P0 and P1
fail-closed with the cause three layers up.
"""

from __future__ import annotations

from typing import Any

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.production_worker import (
    _is_republisher_daily_quota_exhausted,
)
from scripts import weather_basket_yes_daemon as daemon


class _RotatingWorker:
    """Advances one scope per ``refresh_resident_nwp`` call, as the real one does."""

    skip_nwp_refresh = False
    commit_conn = None
    taf_artifact_path = None
    taf_inbox_path = None

    def __init__(self, states: list[str]) -> None:
        self._states = list(states)
        self.calls = 0
        self._nwp_rotation_state = {
            "catalog_scope_keys": [f"scope-{index}" for index in range(90)],
        }

    def refresh_resident_nwp(self, *, as_of_utc: str) -> dict[str, Any]:
        state = self._states[min(self.calls, len(self._states) - 1)]
        self.calls += 1
        return {
            "schema": "bayes_basket_target_resident_native_ensemble_cycle_v1",
            "state": state,
            "scope_index": self.calls - 1,
            "calls_order_gateway": False,
            "places_orders": False,
        }

    def run(self) -> dict[str, Any]:
        return {
            "ok": True,
            "calls_order_gateway": False,
            "places_orders": False,
        }


def _advance(states: list[str]) -> tuple[_RotatingWorker, dict[str, Any]]:
    """One due poll; its receipt is the advance result itself.

    ``last_attempt`` only appears on later not-due heartbeats, which reuse the
    receipt this first call stores.
    """

    worker = _RotatingWorker(states)
    receipt = daemon.run_target_forecast_refresh_cycle(worker)
    return worker, receipt["resident_nwp_capture"]


def test_a_blocked_first_scope_does_not_end_the_poll() -> None:
    worker, attempt = _advance(["blocked_fail_closed"])
    assert worker.calls == attempt["scopes_per_poll_target"] > 1
    assert attempt["scopes_advanced_this_poll"] == worker.calls
    assert attempt["blocked_scope_count_this_poll"] == worker.calls


def test_scopes_behind_a_blocker_still_get_their_turn() -> None:
    """The reason this matters: their official captures start on their turn."""

    worker, attempt = _advance(
        ["blocked_fail_closed", "blocked_fail_closed", "official_native_reused"]
    )
    assert worker.calls > 2
    assert attempt["blocked_scope_count_this_poll"] == 2
    assert not str(attempt["state"]).startswith("blocked")


def test_the_poll_reports_a_scope_that_got_somewhere_when_one_did() -> None:
    """Otherwise the reported state flaps on which scope happened to be last."""

    _, attempt = _advance(
        ["official_native_reused", "blocked_fail_closed", "blocked_fail_closed"]
    )
    assert attempt["state"] == "official_native_reused"
    assert attempt["blocked_scope_count_this_poll"] > 0


def test_a_wholly_blocked_poll_still_reports_blocked() -> None:
    """A poll that achieved nothing is the condition worth paging on."""

    _, attempt = _advance(["blocked_fail_closed"])
    assert str(attempt["state"]).startswith("blocked")


@pytest.mark.parametrize(
    ("message", "exhausted"),
    [
        pytest.param(
            "open_meteo_ensemble_rate_limited:429:"
            "Daily API request limit exceeded. Please try again tomorrow.",
            True,
            id="daily_cap_is_spent_for_the_day",
        ),
        pytest.param(
            "open_meteo_ensemble_rate_limited:429:"
            "Minutely API request limit exceeded.",
            False,
            id="short_window_limit_is_worth_retrying",
        ),
        pytest.param(
            "official_native_packet_unavailable:timeout",
            False,
            id="unrelated_failure",
        ),
    ],
)
def test_only_a_daily_cap_suppresses_the_republisher_for_the_day(
    message: str, exhausted: bool
) -> None:
    """Advancing past blockers must not turn into hammering a spent API.

    Both cases share a 429, so the provider's own wording is what separates
    "come back in a minute" from "come back tomorrow".  Treating the first as
    a daily cap would drop the fallback for hours it was still allowed to use.
    """

    assert _is_republisher_daily_quota_exhausted(
        ContractError(message)
    ) is exhausted


@pytest.mark.parametrize(
    "budget_fraction", [pytest.param(0.5, id="half_the_poll_interval")]
)
def test_the_advance_budget_leaves_room_inside_one_poll(
    budget_fraction: float,
) -> None:
    """Wall clock is the real constraint the loop has to respect.

    A poll that overruns its interval delays the next one and the sweep falls
    behind the TTL -- which is the same failure the blocked-break caused, by a
    slower route.
    """

    assert daemon.TARGET_NWP_ADVANCE_BUDGET_SEC == (
        daemon.TARGET_NWP_POLL_INTERVAL_SEC * budget_fraction
    )
    assert 0 < daemon.TARGET_NWP_ADVANCE_BUDGET_SEC < (
        daemon.TARGET_NWP_POLL_INTERVAL_SEC
    )
