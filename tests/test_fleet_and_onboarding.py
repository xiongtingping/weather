"""The fleet's size is the registry's, and onboarding is a staged pipeline.

``45`` was a literal in some thirty modules while the registry it was
describing sat one import away.  Adding a station meant finding every
copy, and a copy missed is a guard that refuses the fleet on the station
nobody told it about.
"""

import pytest

from bayes_basket_target.fleet import expected_station_count, is_exact_fleet
from bayes_basket_target.model_system_coverage import canonical_station_ids
from bayes_basket_target.station_registry import coordinate_registry_manifest


def test_count_is_the_registry_not_a_literal():
    manifest = coordinate_registry_manifest()
    assert expected_station_count() == len(manifest["station_ids"])
    assert expected_station_count() == int(manifest["station_count"])


def test_exact_fleet_accepts_the_fleet_in_any_order():
    stations = list(canonical_station_ids())
    assert is_exact_fleet(stations)
    assert is_exact_fleet(list(reversed(stations)))
    assert is_exact_fleet([s.lower() for s in stations])


def test_exact_fleet_rejects_a_right_sized_wrong_set():
    """The failure the hand-written predicate could not see.

    ``len(x) != N or len(set(x)) != N`` checks the size twice and the
    membership never, so a full-length list carrying a duplicate in place
    of a missing station passed every one of those guards.
    """

    stations = list(canonical_station_ids())
    duplicated = stations[:-1] + [stations[0]]
    assert len(duplicated) == len(stations)
    assert not is_exact_fleet(duplicated)


def test_exact_fleet_rejects_a_station_the_registry_does_not_carry():
    stations = list(canonical_station_ids())
    assert not is_exact_fleet(stations + ["LTFM"])
    assert not is_exact_fleet(stations[:-1])


def test_onboarding_discovery_finds_listings_this_fleet_lacks():
    """Discovery reads the market registry, not a curated city list.

    A curated list can only find cities somebody already thought of, which
    is the failure mode this replaces.
    """

    from scripts.onboard_station import discover

    found = discover()
    known = {
        str(v).upper()
        for v in coordinate_registry_manifest()["station_ids"]
    }
    # Nothing already in the fleet may appear as a candidate.
    assert all(
        (row["station_id"] or "") not in known for row in found
    ), [r for r in found if (r["station_id"] or "") in known]
    states = {row["state"] for row in found}
    assert states <= {"CANDIDATE", "NO_STATION_RESOLVED"}


def test_a_listing_with_no_resolved_station_is_its_own_state():
    """Hong Kong is not a coordinate away from being onboardable.

    Its market does not resolve off a NOAA site, so no station id is ever
    parsed and this system's truth lane cannot settle it.  Reporting that
    as a missing coordinate would send someone to fetch one.
    """

    from scripts.onboard_station import discover

    by_city: dict[str, set] = {}
    for row in discover():
        by_city.setdefault(str(row["city_slug"]), set()).add(row["state"])
    if "hong-kong" in by_city:
        assert by_city["hong-kong"] == {"NO_STATION_RESOLVED"}


def test_rebaseline_names_the_manifest_writers_rather_than_editing_them():
    """The cost of a fleet change, stated where it will be read.

    Deriving the count removes the thirty edits; it does not remove the
    re-baseline, because several guards stamp the count into
    content-addressed manifests whose hashes then move.
    """

    from scripts.onboard_station import rebaseline

    report = rebaseline()
    assert report["current_fleet_size"] == expected_station_count()
    assert report["manifest_writers"]
    assert report["evidence_still_required_per_station"]
