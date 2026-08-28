"""How many stations the fleet has, derived rather than declared.

The coordinate registry has always been the single source of truth for
*which* stations exist -- ``canonical_station_ids`` reads it and nothing
else does -- but *how many* was a literal ``45`` repeated across some
thirty modules.  Adding a station therefore meant finding every one of
them, and missing one meant a guard that refuses the new fleet on the
station it was never told about.

So the count comes from the registry too.  Nothing here changes what any
guard checks: a guard that demanded exactly 45 still demands exactly the
fleet, which is the thing it was always trying to say.

What a fleet change actually costs
----------------------------------

Deriving the count does not make onboarding free, and this module is the
right place to say why.  Several of the guards that read this constant do
not merely compare against it -- they write it into content-addressed
manifests (``canonical_activation_release``, ``model_system_coverage``,
``icon_eps_native_arrival_index``, ``station_policy_exact_stage_scorecard``
among them).  Changing the fleet changes those manifests' bytes, which
changes their hashes, which unbinds every artifact pinned to them.  That is
a deliberate re-baseline with its own review, not a side effect anyone
should discover from a red test.

The other half of the cost is evidence: a new station has no arrival
corpus, no OOS history, no season anchor derived from its own climatology,
and no settled truth.  ``scripts/onboard_station.py`` walks those stages
and refuses to skip them.
"""

from __future__ import annotations

from .station_registry import coordinate_registry_manifest


def expected_station_count() -> int:
    """The fleet's size, as the coordinate registry currently states it."""

    return int(coordinate_registry_manifest()["station_count"])


def is_exact_fleet(stations) -> bool:
    """Whether a station collection is exactly the fleet, once each.

    The predicate three dozen guards were spelling out by hand as
    ``len(x) != 45 or len(set(x)) != 45`` -- which checks the size twice
    and the membership never, so a 45-long list of the wrong stations
    passed.  Membership is the thing worth checking; the count follows.
    """

    values = [str(value).upper() for value in stations]
    if len(values) != len(set(values)):
        return False
    return set(values) == set(
        str(value).upper()
        for value in coordinate_registry_manifest()["station_ids"]
    )


__all__ = ["expected_station_count", "is_exact_fleet"]
