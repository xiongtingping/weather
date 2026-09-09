"""Charter 125: the day's observed high as a selection-layer floor.

The settlement rule is the day's maximum over the whole local day, so a
report of X makes every bucket ending at or below X arithmetically dead --
whatever any model still claims about it.  Charter 61 put that predicate in
the posterior of the one stage that roots on an observation, and the
selection layer then inherited it by accident: ``_support_windows`` draws
its domain from ``probability > EPS`` and its own comment says the safety
property is that "a bucket the hard lower bound has excluded holds zero
probability".  That sentence is true at P1 and false everywhere else --
P0's posterior never sees an observation at all, and P0-M only computes the
bound inside its TAF-revision paths, so a day with no TAF revision never
recomputes it.

Measured 2026-09-09 on the store: of the 5,561 P0 envelopes minted *during*
their own local day since 09-01, the mean mass on buckets the day's own
thermometer had already ruled out is 10.7%, and 441 of them put more than
half their mass there.  ZUUU 2026-09-09 is the case that named this: the
00:00 local METAR read 27C and the day only cooled, P1 and P0-M both
re-enveloped onto 27/28C within six minutes, and the P0 envelope minted ten
hours later still called 24C the mode at 38.6%.  It cost nothing that day
only because ``select_current_stage`` hands authority to P1 whenever a P1
envelope exists -- an ordering accident, not a rule.

So the floor becomes a fact the selection layer reads for itself, at every
stage, from the same single predicate.  The running high is produced where
the canonical observation connection lives and travels on the market
snapshot; the arithmetic from a running high to a set of dead buckets is
here, so no caller re-derives it.

Absent a running high -- the pre-day case, which is most P0 decisions --
nothing is excluded and every downstream hash is byte-identical to before.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Sequence

from .contracts import ContractError
from .metar_trajectory import hard_bound_excluded_buckets


# Off switches exist so a measurement can run both arms (charter 80 (8)).
# ``on`` is the ruling; ``off`` is the pre-ruling behaviour exactly.
CHARTER_125_OBSERVED_HIGH_FLOOR = (
    os.environ.get("PM_CHARTER_125_OBSERVED_HIGH_FLOOR", "").strip().lower()
    or "on"
)
if CHARTER_125_OBSERVED_HIGH_FLOOR not in {"on", "off"}:
    raise RuntimeError(
        "unknown charter 125 observed-high floor mode: "
        f"{CHARTER_125_OBSERVED_HIGH_FLOOR}"
    )
OBSERVED_HIGH_FLOOR_RULE_VERSION = "observed_high_selection_floor_v1"
# The market snapshot key the running high travels on.  Named for the fact,
# not for the stage that happens to produce it: P1 already carries the same
# number inside its own posterior lineage, and this is the copy every stage
# may read.
MARKET_SNAPSHOT_KEY = "observed_running_high_c"


def observed_high_floor_enabled() -> bool:
    return CHARTER_125_OBSERVED_HIGH_FLOOR == "on"


def running_high_from_market_snapshot(
    market_snapshot: Mapping[str, Any] | None,
) -> float | None:
    """The running high a market snapshot carries, or None when it has none.

    Fails closed on a malformed value rather than reading it as "no
    observation": a snapshot that carries the key at all is asserting a
    fact, and a fact that will not parse is a bug, not an absence.
    """

    if not isinstance(market_snapshot, Mapping):
        return None
    raw = market_snapshot.get(MARKET_SNAPSHOT_KEY)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ContractError("observed_running_high_c_invalid")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError("observed_running_high_c_invalid") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise ContractError("observed_running_high_c_invalid")
    return value


def bucket_upper_bounds(
    bucket_intervals_c_json: Any,
    ordered_buckets: Sequence[str],
) -> dict[str, float] | None:
    """Upper edge per bucket, or None when the ladder cannot be read.

    ``None`` rather than an exception: a bucket map this function cannot
    parse is a vocabulary problem, and the caller's answer to that is to
    apply no floor and say so, not to refuse the whole decision.  That is
    the same choice ``_p0m_observed_hard_bound`` already makes.
    """

    try:
        intervals = json.loads(str(bucket_intervals_c_json or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(intervals, Mapping):
        return None
    ordered = list(ordered_buckets)
    if not ordered or set(ordered) - set(intervals):
        return None
    uppers: dict[str, float] = {}
    for bucket in ordered:
        interval = intervals[bucket]
        if not isinstance(interval, Sequence) or len(interval) != 2:
            return None
        upper = interval[1]
        uppers[bucket] = float("inf") if upper is None else float(upper)
    return uppers


def dead_buckets_for_upper_bounds(
    *,
    ordered_buckets: Sequence[str],
    upper_bounds: Mapping[str, float],
    running_high_c: float | None,
) -> tuple[str, ...]:
    """The floor, for a caller that already holds the ladder's edges.

    The paper ledger reads bucket edges off its own book rows rather than
    off a lineage JSON, so it enters here.  Same predicate, same
    exhaustion refusal; only the shape of the ladder differs.
    """

    if running_high_c is None:
        return ()
    ordered = list(ordered_buckets)
    if not ordered or set(ordered) - set(upper_bounds):
        return ()
    excluded = hard_bound_excluded_buckets(
        ordered, upper_bounds, float(running_high_c)
    )
    if len(excluded) >= len(ordered):
        return ()
    return tuple(excluded)


def dead_buckets_for_running_high(
    *,
    ordered_buckets: Sequence[str],
    bucket_intervals_c_json: Any,
    running_high_c: float | None,
) -> tuple[str, ...]:
    """Buckets the day's observed high has already ruled out.

    One predicate, shared with charter 61 -- ``hard_bound_excluded_buckets``
    is called, never restated -- so the selection floor and the posterior
    bound can never drift apart at the boundary.

    Returns ``()`` when there is no running high, when the ladder cannot be
    read, or when the bound would exhaust the support.  The last case is the
    same refusal P0-M already makes: a bound that deletes every outcome is
    evidence of a bucket-map problem, not a fact to trade on.

    The charter 125 switch is deliberately not read here.  This function is
    also charter 61's arithmetic, which predates the ruling and is not
    switchable; the new gate is the *selection* consumer, and that is where
    the switch belongs.
    """

    if running_high_c is None:
        return ()
    ordered = list(ordered_buckets)
    uppers = bucket_upper_bounds(bucket_intervals_c_json, ordered)
    if uppers is None:
        return ()
    return dead_buckets_for_upper_bounds(
        ordered_buckets=ordered,
        upper_bounds=uppers,
        running_high_c=running_high_c,
    )


__all__ = [
    "CHARTER_125_OBSERVED_HIGH_FLOOR",
    "MARKET_SNAPSHOT_KEY",
    "OBSERVED_HIGH_FLOOR_RULE_VERSION",
    "bucket_upper_bounds",
    "dead_buckets_for_running_high",
    "dead_buckets_for_upper_bounds",
    "observed_high_floor_enabled",
    "running_high_from_market_snapshot",
]
