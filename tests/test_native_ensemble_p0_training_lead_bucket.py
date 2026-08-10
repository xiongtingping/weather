"""P0 asks for weights under the key they were fitted under.

OOS moved training groups from the calendar label (``d0``/``d1``/``d2``) to the
lead measured in hours to the station's own local day, because the calendar
groups overlap: d1 and d2 both hold runs issued 18-31h ahead, and d2 alone
spans 37h.  P0 kept asking under the calendar label, so every artifact fitted
after the move was invisible to it -- production coasted on calendar-keyed
artifacts already inside their validity window and would have failed closed
when they expired, with no replacement reachable.

The calendar label still means something, and it is a different thing: how far
from the event day the decision is being made.  Station policy and the TAF
fingerprint identity are keyed by it.  So the two travel separately rather than
one replacing the other.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.native_ensemble_p0 import (
    _activated_lead_bucket,
    _hour_lead_bucket,
)


SYSTEMS = ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"]
DECISION_AT = "2026-08-10T02:30:00.000Z"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE calibration_native_ensemble_activation (
          activation_id TEXT PRIMARY KEY,
          artifact_kind TEXT NOT NULL,
          artifact_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          season_bin TEXT NOT NULL,
          lead_bucket TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          generation INTEGER NOT NULL,
          valid_from_utc TEXT NOT NULL,
          valid_until_utc TEXT NOT NULL
        );
        CREATE TABLE calibration_cross_native_ensemble_artifact (
          cross_ensemble_artifact_id TEXT PRIMARY KEY,
          ensemble_system_ids_json TEXT NOT NULL
        );
        """
    )
    return conn


def _activate(
    conn: sqlite3.Connection,
    *,
    activation_id: str,
    lead_bucket: str,
    system_ids: list[str],
    station_id: str = "KLGA",
    valid_until_utc: str = "2026-09-08T00:00:00.000Z",
) -> None:
    artifact_id = f"artifact-{activation_id}"
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES (?,?)",
        (artifact_id, '["' + '","'.join(system_ids) + '"]'),
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation VALUES "
        "(?,?,?,?,?,?,?,?,?,?)",
        (
            activation_id,
            "CROSS_NATIVE_ENSEMBLE",
            artifact_id,
            station_id,
            "JJA",
            lead_bucket,
            "PRODUCTION_NO_LIVE_ONLY",
            1,
            "2026-08-09T00:00:00.000Z",
            valid_until_utc,
        ),
    )


def _resolve(
    conn: sqlite3.Connection,
    candidates: tuple[str, ...],
    *,
    observed: list[str] | None = None,
) -> str:
    return _activated_lead_bucket(
        conn,
        station_id="KLGA",
        season_bin="JJA",
        decision_at_utc=DECISION_AT,
        observed_system_ids=SYSTEMS if observed is None else observed,
        candidates=candidates,
    )


def test_hour_bucket_wins_over_the_calendar_label() -> None:
    """Both fitted: the run's own lead is the more exact grouping."""

    conn = _conn()
    _activate(conn, activation_id="h", lead_bucket="h24_48", system_ids=SYSTEMS)
    _activate(conn, activation_id="d", lead_bucket="d2", system_ids=SYSTEMS)
    assert _resolve(conn, ("h24_48", "d2")) == "h24_48"


def test_calendar_label_still_serves_stations_without_a_refit() -> None:
    """Only 37 of the 45 stations had been refitted when this landed.

    Preferring the hour bucket must not take P0 away from the rest: their
    calendar-keyed artifact is a real fit over the same rows, just grouped more
    coarsely, and it stays reachable until their own refit arrives.
    """

    conn = _conn()
    _activate(conn, activation_id="d", lead_bucket="d2", system_ids=SYSTEMS)
    assert _resolve(conn, ("h24_48", "d2")) == "d2"


def test_artifact_naming_an_unobserved_system_is_not_admissible() -> None:
    """An hour-bucket fit for systems today's packet lacks cannot be used.

    Falling through to the calendar label here is the point: the alternative is
    selecting a grouping whose systems are absent and failing closed deeper in,
    which loses P0 for a station that had a usable artifact all along.
    """

    conn = _conn()
    _activate(
        conn,
        activation_id="h",
        lead_bucket="h24_48",
        system_ids=[*SYSTEMS, "ECCC_GEM_GLOBAL_ENS"],
    )
    _activate(conn, activation_id="d", lead_bucket="d2", system_ids=SYSTEMS)
    assert _resolve(conn, ("h24_48", "d2")) == "d2"


def test_expired_hour_bucket_activation_is_not_selected() -> None:
    conn = _conn()
    _activate(
        conn,
        activation_id="h",
        lead_bucket="h24_48",
        system_ids=SYSTEMS,
        valid_until_utc="2026-08-09T12:00:00.000Z",
    )
    _activate(conn, activation_id="d", lead_bucket="d2", system_ids=SYSTEMS)
    assert _resolve(conn, ("h24_48", "d2")) == "d2"


def test_last_candidate_is_returned_when_nothing_is_activated() -> None:
    """Resolution picks a key to ask under; it does not decide usability.

    Returning the calendar label rather than raising keeps the fail-closed
    reporting where the contract checks already are, so an operator reading the
    error sees which scope was missing instead of a resolution failure.
    """

    assert _resolve(_conn(), ("h24_48", "d2")) == "d2"


def test_hour_bucket_is_measured_to_the_local_day_not_utc_midnight() -> None:
    """KLGA is UTC-4 in August, so its 2026-08-11 begins at 04:00Z.

    The 8/9 18Z run is 34h ahead of that, which is h24_48.  Measured to UTC
    midnight it would be 30h -- the same bucket here, but the four hours are
    enough to cross a boundary for other station-days, which is the distinction
    the bucket exists to draw.
    """

    assert _hour_lead_bucket(
        event_date="2026-08-11",
        timezone_name="America/New_York",
        run_init_utc_by_system={
            system_id: "2026-08-09T18:00:00Z" for system_id in SYSTEMS
        },
        observed_system_ids=SYSTEMS,
    ) == "h24_48"


@pytest.mark.parametrize(
    "run_init_utc_by_system",
    [
        pytest.param({}, id="aggregator_packet_states_no_run"),
        pytest.param(
            # 34h ahead of the 04:00Z local day start, against 16h: two
            # groups.  Runs a few hours apart would share one, which is why
            # this needs a pair that actually straddles a boundary.
            {
                "ECMWF_IFS_ENS_OPEN_PF50": "2026-08-09T18:00:00Z",
                "NOAA_GEFS_05": "2026-08-10T12:00:00Z",
            },
            id="systems_issued_into_different_groups",
        ),
        pytest.param(
            {system_id: "" for system_id in SYSTEMS},
            id="run_stated_but_empty",
        ),
    ],
)
def test_no_hour_bucket_when_the_packet_does_not_support_one(
    run_init_utc_by_system: dict[str, str],
) -> None:
    """A cross artifact holds systems that share one bucket by construction.

    So a packet whose systems straddle two groups cannot match one, and saying
    so is different from guessing which half to believe.
    """

    assert _hour_lead_bucket(
        event_date="2026-08-11",
        timezone_name="America/New_York",
        run_init_utc_by_system=run_init_utc_by_system,
        observed_system_ids=SYSTEMS,
    ) is None
