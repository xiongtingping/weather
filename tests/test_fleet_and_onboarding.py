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
    # ZZZZ is ICAO's "no designator" placeholder: unlike a real candidate
    # (this test used LTFM until Istanbul joined the fleet), it can never be
    # onboarded out from under the assertion.
    stations = list(canonical_station_ids())
    assert not is_exact_fleet(stations + ["ZZZZ"])
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


class _AwcResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _AwcClient:
    """Every AWC question answered as a healthy station would answer it."""

    def get(self, url):
        if "/stationinfo?" in url:
            return _AwcResponse([{"lat": 34.52, "lon": 113.834}])
        if "/metar?" in url:
            return _AwcResponse([{}, {}, {}, {}])
        if "/taf?" in url:
            return _AwcResponse([{}])
        raise AssertionError(url)


@pytest.mark.parametrize(
    ("offered", "admissible"),
    [(("", 0), False), (("Asia/Shanghai", 1), True),
     (("Asia/Shanghai", 2), False)],
)
def test_admission_refuses_a_station_without_exactly_one_timezone(
    monkeypatch, offered, admissible,
):
    """Admission has to refuse what the row commit will refuse.

    Zhengzhou's listings carry no timezone.  The audit never asked, so it
    read ADMISSIBLE; onboarding wrote its coordinate -- the registry is the
    fleet -- and only then did the row commit refuse it, leaving a fleet
    member with no settlement day (2026-09-21).  The commit cannot move
    ahead of the registry write, so admission is the last place to say no.
    """

    import scripts.audit_new_market_station_admission as audit

    monkeypatch.setattr(audit, "market_timezone", lambda station: offered)
    result = audit._assess(
        _AwcClient(),
        {"station_id": "ZHCC", "city_slug": "zhengzhou"},
        known={"ZHCC"},
    )
    assert result["admissible"] is admissible
    assert (
        "settlement_timezone_not_unique" in result["blockers"]
    ) is (not admissible)


def test_onboarding_refuses_a_fleet_the_pr12_projection_does_not_carry(
    tmp_path, monkeypatch,
):
    """The projection is a precondition of the move, not a follow-up.

    The pr12 sweep validates its sealed projection against the live fleet
    every round.  2026-08-31 and 2026-09-21 both wrote the registry first;
    the first stopped the sweep and every P1 atomic write behind it, the
    second was reverted within the hour, before the resident loaded it.
    """

    import json

    import scripts.onboard_station as onboard

    fleet = list(coordinate_registry_manifest()["station_ids"])
    projection = tmp_path / "projection.json"
    projection.write_text(
        json.dumps({"rows": [{"station_id": s} for s in fleet]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(onboard, "_pr12_projection_path", lambda: projection)

    assert onboard._sealed_fleet_blockers(fleet) == []
    refused = onboard._sealed_fleet_blockers(fleet + ["ZZZZ"])
    assert [b["state"] for b in refused] == ["SEALED_AT_ANOTHER_FLEET"]
    assert refused[0]["missing_from_projection"] == ["ZZZZ"]

    monkeypatch.setattr(onboard, "_pr12_projection_path", lambda: None)
    assert [b["state"] for b in onboard._sealed_fleet_blockers(fleet)] == [
        "PIN_UNREADABLE"
    ]


def test_the_projection_pin_is_the_one_the_sweep_reads():
    import scripts.onboard_station as onboard

    path = onboard._pr12_projection_path()
    assert path is not None and path.exists(), path
    assert onboard._sealed_fleet_blockers(
        coordinate_registry_manifest()["station_ids"]
    ) == []
