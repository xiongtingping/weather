"""The trajectory scope key must speak the vocabulary the consumer asks in.

Charter 62 moved the P0 root's ``season_bin`` to station-local labels on
2026-08-25.  The consumer builds the trajectory scope key from that
lineage; the training packet still stamped the calendar trigram ``JJA``.
Neither side raised -- a scope-key miss falls back to applied strength 0 --
so P1's soft likelihood went from 72% coverage on 08-23 to 14.5% on the
25th and 0% from the 26th, with no blocker recorded anywhere.
"""

import pytest

from bayes_basket_target.station_season import (
    STATION_SEASON_BINS,
    station_season_bin,
)
from bayes_basket_target.probability_evidence_store import (
    metar_trajectory_scope_key,
)


def test_scope_key_season_field_is_the_station_local_vocabulary():
    """What the consumer asks for, spelled out.

    The reader passes the P0 root's lineage ``season_bin`` straight into
    this builder, so whatever the packet stamps has to come from the same
    function that produced that lineage.
    """

    key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin=station_season_bin("OEJN", "2026-08-27"),
        local_time_bucket="MORNING",
        maturity_bin="D0",
        settlement_definition="official_daily_tmax_c",
    )
    assert key.split("|")[1] in STATION_SEASON_BINS


def test_the_two_vocabularies_are_disjoint():
    """Which is why the mismatch could never have matched by luck.

    station_season.py says the disjointness exists so a cross-vocabulary
    join fails loudly.  It failed silently instead, because the miss has a
    fallback -- but the disjointness itself is what this pins: if a future
    label ever collided with a trigram, a stale artifact would start
    answering lookups it was not fitted for, which is worse than the
    outage.
    """

    calendar = {"DJF", "MAM", "JJA", "SON"}
    english = {"winter", "spring", "summer", "autumn"}
    assert not (set(STATION_SEASON_BINS) & calendar)
    assert not (set(STATION_SEASON_BINS) & english)


def test_low_amplitude_stations_have_one_season_all_year():
    """The user's own requirement, and charter 62's: no phase, no seasons.

    WMKK and WSSS sit essentially on the equator; a quartered calendar
    assigns them four seasons they do not have.
    """

    for station in ("WMKK", "WSSS"):
        labels = {
            station_season_bin(station, day)
            for day in (
                "2026-01-15", "2026-04-15", "2026-07-15", "2026-10-15",
            )
        }
        assert labels == {"ALLYEAR"}, (station, labels)


def test_peak_follows_each_station_own_hottest_month_not_the_calendar():
    """Ten of forty-five peak outside June-August.

    Southern (SAEZ, FACT, NZWN, SBGR) in the austral summer, the
    pre-monsoon group (VILK, OPKC) before the rains, the marine-lagged
    Californians (KSFO, KLAX) in September.  A fixed Jul-Sep "summer"
    would put every one of them in the wrong bin.
    """

    # Austral: hottest in January, so mid-July is their TROUGH.
    for station in ("SAEZ", "FACT", "NZWN", "SBGR"):
        assert station_season_bin(station, "2026-01-15") == "PEAK", station
        assert station_season_bin(station, "2026-07-15") == "TROUGH", station
    # Pre-monsoon: hottest in May, so July is already DESCENT and the
    # trough sits in the autumn, not the boreal winter a calendar would
    # assign.  January is on the way up, which is the whole point -- the
    # cycle is rotated onto the station's own anchor, not onto the year.
    for station in ("VILK", "OPKC"):
        assert station_season_bin(station, "2026-05-15") == "PEAK", station
        assert station_season_bin(station, "2026-07-15") == "DESCENT", station
        assert station_season_bin(station, "2026-11-15") == "TROUGH", station
        assert station_season_bin(station, "2026-01-15") == "ASCENT", station
    # Marine-lagged: September, not July.
    for station in ("KSFO", "KLAX"):
        assert station_season_bin(station, "2026-09-15") == "PEAK", station


def test_a_northern_station_leaves_peak_after_august():
    """The 9/1 step this vocabulary makes real rather than calendrical."""

    assert station_season_bin("KLGA", "2026-08-27") == "PEAK"
    assert station_season_bin("KLGA", "2026-10-15") == "DESCENT"


def test_packet_builder_no_longer_hardcodes_a_calendar_trigram():
    """Pin the literal out of existence.

    The defect was one string in one dict, invisible to every test that
    exercised the pipeline end to end, because both sides of a broken join
    still produce well-formed keys.
    """

    import pathlib

    import bayes_basket_target

    root = pathlib.Path(bayes_basket_target.__file__).resolve().parents[1]
    source = (
        root
        / "scripts"
        / "build_bayes_basket_target_metar_trajectory_training_packet.py"
    ).read_text(encoding="utf-8")
    assert '"season_bin": "JJA"' not in source
    assert "station_season_bin(" in source


def test_activation_id_is_unique_per_fit_not_per_day():
    """The primary key that silently ate six activations.

    The id carried the training date, so two fits of one station and bucket
    on the same day want the same id.  The activation store keys on it, and
    the artifact is inserted first -- so the second fit lands its artifact
    and loses its pointer, which reads downstream as an artifact nobody
    activated rather than as a collision.  On 2026-08-28 the resident refit
    after a restart and produced six artifacts with zero activations, every
    id already held by that morning's row.
    """

    import pathlib

    import bayes_basket_target

    root = pathlib.Path(bayes_basket_target.__file__).resolve().parents[1]
    source = (
        root
        / "scripts"
        / "build_bayes_basket_target_metar_trajectory_training_packet.py"
    ).read_text(encoding="utf-8")
    start = source.index('"activation_id": (')
    body = source[start:start + 900]
    # The date alone is not identity; the fit's own content has to be in it.
    assert "content_sha256(" in body, body[:300]
    assert "trained_through_utc" in body
    assert "valid_from_utc" in body


def test_two_fits_the_same_day_get_different_activation_ids():
    """Same station, same bucket, same day, different fit -> different id."""

    from bayes_basket_target.contracts import content_sha256
    from bayes_basket_target.station_season import station_season_bin

    def activation_id(*, trained_through, valid_from):
        return (
            "metar-trajectory-CYYZ-evening-physical-"
            + trained_through[:10].replace("-", "")
            + "-"
            + content_sha256({
                "station_id": "CYYZ",
                "bucket": "EVENING",
                "method": "physical",
                "season_bin": station_season_bin("CYYZ", valid_from[:10]),
                "trained_through_utc": trained_through,
                "valid_from_utc": valid_from,
            })[:12]
        )

    morning_run = activation_id(
        trained_through="2026-08-28T03:59:59.000Z",
        valid_from="2026-08-28T05:27:57.712Z",
    )
    evening_run = activation_id(
        trained_through="2026-08-28T03:59:59.000Z",
        valid_from="2026-08-28T06:29:15.923Z",
    )
    assert morning_run != evening_run
    # And an identical re-fit stays a no-op rather than a new row.
    assert morning_run == activation_id(
        trained_through="2026-08-28T03:59:59.000Z",
        valid_from="2026-08-28T05:27:57.712Z",
    )
