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

import json
import sqlite3

import pytest

from bayes_basket_target.native_ensemble_p0 import (
    _activated_lead_bucket,
    _hour_lead_buckets,
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
          valid_until_utc TEXT NOT NULL,
          run_cycle_by_system_json TEXT
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
    cycles: dict[str, str] | None = None,
) -> None:
    artifact_id = f"artifact-{activation_id}"
    conn.execute(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES (?,?)",
        (artifact_id, '["' + '","'.join(system_ids) + '"]'),
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?)",
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
            json.dumps(
                cycles if cycles is not None
                else {system_id: "00Z" for system_id in system_ids},
                sort_keys=True,
            ),
        ),
    )


def _resolve(
    conn: sqlite3.Connection,
    candidates: tuple[str, ...],
    *,
    observed: list[str] | None = None,
    cycles: dict[str, str] | None = None,
    fresh: list[str] | None = None,
) -> str:
    return _activated_lead_bucket(
        conn,
        station_id="KLGA",
        season_bin="JJA",
        decision_at_utc=DECISION_AT,
        observed_system_ids=SYSTEMS if observed is None else observed,
        candidates=candidates,
        packet_cycle_by_system=cycles,
        freshest_bucket_system_ids=fresh or (),
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

    buckets, by_system = _hour_lead_buckets(
        event_date="2026-08-11",
        timezone_name="America/New_York",
        run_init_utc_by_system={
            system_id: "2026-08-09T18:00:00Z" for system_id in SYSTEMS
        },
        observed_system_ids=SYSTEMS,
    )
    assert buckets == ("h24_48",)
    assert set(by_system.values()) == {"h24_48"}


@pytest.mark.parametrize(
    "run_init_utc_by_system",
    [
        pytest.param({}, id="aggregator_packet_states_no_run"),
        pytest.param(
            {system_id: "" for system_id in SYSTEMS},
            id="run_stated_but_empty",
        ),
    ],
)
def test_no_hour_bucket_when_the_packet_states_no_runs(
    run_init_utc_by_system: dict[str, str],
) -> None:
    """No run stated means no bucket claim at all -- an empty resolution."""

    assert _hour_lead_buckets(
        event_date="2026-08-11",
        timezone_name="America/New_York",
        run_init_utc_by_system=run_init_utc_by_system,
        observed_system_ids=SYSTEMS,
    ) == ((), {})


def test_a_split_packet_names_every_bucket_it_touches_freshest_first() -> None:
    """Captures read each family at its own newest published run, so a packet
    that straddles two adjacent groups is the ordinary daytime state.  Both
    groups are real trained keys; the caller tries them in freshness order
    and the per-system map names who sits where."""

    buckets, by_system = _hour_lead_buckets(
        event_date="2026-08-11",
        timezone_name="America/New_York",
        run_init_utc_by_system={
            "ECMWF_IFS_ENS_OPEN_PF50": "2026-08-09T18:00:00Z",
            "NOAA_GEFS_05": "2026-08-10T12:00:00Z",
        },
        observed_system_ids=SYSTEMS,
    )
    assert buckets == ("h12_24", "h24_48")
    assert by_system == {
        "ECMWF_IFS_ENS_OPEN_PF50": "h24_48",
        "NOAA_GEFS_05": "h12_24",
    }


def test_a_system_stating_no_run_does_not_veto_the_others() -> None:
    """An excluded bystander must not decide the key the others ask under.

    A system with no run initialisation cannot be in a cross-ensemble artifact
    -- that requires a native run -- so it has no bucket to disagree about.
    ZUUU carried ECCC as observed while excluding it for having no exact OOS
    activation; treating its silence as a straddle sent the packet back to the
    calendar label, under which nothing is fitted any more.
    """

    buckets, by_system = _hour_lead_buckets(
        event_date="2026-08-10",
        timezone_name="Asia/Shanghai",
        run_init_utc_by_system={
            "ECMWF_IFS_ENS_OPEN_PF50": "2026-08-08T18:00:00Z",
            "NOAA_GEFS_05": "2026-08-08T18:00:00Z",
        },
        observed_system_ids=["ECCC_GEM_GLOBAL_ENS", *SYSTEMS],
    )
    assert buckets == ("h12_24",)
    assert "ECCC_GEM_GLOBAL_ENS" not in by_system


def test_a_real_straddle_is_reported_whole_when_a_bystander_is_present() -> None:
    # Skipping the silent system must not blur what the speaking systems
    # said: both touched buckets are reported, and the bystander stays out
    # of the map.
    buckets, by_system = _hour_lead_buckets(
        event_date="2026-08-11",
        timezone_name="America/New_York",
        run_init_utc_by_system={
            "ECMWF_IFS_ENS_OPEN_PF50": "2026-08-09T18:00:00Z",
            "NOAA_GEFS_05": "2026-08-10T12:00:00Z",
        },
        observed_system_ids=["ECCC_GEM_GLOBAL_ENS", *SYSTEMS],
    )
    assert buckets == ("h12_24", "h24_48")
    assert "ECCC_GEM_GLOBAL_ENS" not in by_system


# Charter 88 (2026-09-06): h00_12 may borrow the h12_24 activation of the
# same run cycle.  The ZGSZ 2026-09-06 shape: ICON/ECCC ride the 00Z run of
# the day before (h12_24 by the hour), ECMWF/GEFS the 18Z run issued two
# hours into the local day (h00_12); h00_12 was only ever fitted at 00Z.
ICON, ECCC = "DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"
ECMWF, GEFS = SYSTEMS
FOUR = [ICON, ECCC, ECMWF, GEFS]
PACKET_CYCLES = {ICON: "00Z", ECCC: "00Z", ECMWF: "18Z", GEFS: "18Z"}


def test_h00_12_chain_borrows_h12_24_by_ruling() -> None:
    from bayes_basket_target.native_ensemble_p0 import HOUR_LEAD_FALLBACK_CHAIN

    assert HOUR_LEAD_FALLBACK_CHAIN["h00_12"] == ("h00_12", "h12_24")


def test_the_borrow_is_taken_when_only_h12_24_seats_the_last_run() -> None:
    """h00_12 seats yesterday's riders only; h12_24 seats ECMWF/GEFS at 18Z."""

    conn = _conn()
    _activate(conn, activation_id="h0-old", lead_bucket="h00_12",
              system_ids=[ICON, ECCC], cycles={ICON: "00Z", ECCC: "00Z"})
    _activate(conn, activation_id="h12-18z", lead_bucket="h12_24",
              system_ids=[ECMWF, GEFS], cycles={ECMWF: "18Z", GEFS: "18Z"})
    assert _resolve(
        conn, ("h00_12", "h12_24"), observed=FOUR,
        cycles=PACKET_CYCLES, fresh=[ECMWF, GEFS],
    ) == "h12_24"


def test_the_exact_bucket_takes_over_once_it_seats_the_last_run() -> None:
    conn = _conn()
    _activate(conn, activation_id="h0-old", lead_bucket="h00_12",
              system_ids=[ICON, ECCC], cycles={ICON: "00Z", ECCC: "00Z"})
    _activate(conn, activation_id="h0-18z", lead_bucket="h00_12",
              system_ids=[ECMWF, GEFS], cycles={ECMWF: "18Z", GEFS: "18Z"})
    _activate(conn, activation_id="h12-18z", lead_bucket="h12_24",
              system_ids=[ECMWF, GEFS], cycles={ECMWF: "18Z", GEFS: "18Z"})
    assert _resolve(
        conn, ("h00_12", "h12_24"), observed=FOUR,
        cycles=PACKET_CYCLES, fresh=[ECMWF, GEFS],
    ) == "h00_12"


def test_a_borrow_must_be_the_same_run_cycle() -> None:
    """An h12_24 fit at 00Z for ECMWF/GEFS is another run: no borrow."""

    conn = _conn()
    _activate(conn, activation_id="h0-old", lead_bucket="h00_12",
              system_ids=[ICON, ECCC], cycles={ICON: "00Z", ECCC: "00Z"})
    _activate(conn, activation_id="h12-00z", lead_bucket="h12_24",
              system_ids=[ECMWF, GEFS], cycles={ECMWF: "00Z", GEFS: "00Z"})
    assert _resolve(
        conn, ("h00_12", "h12_24"), observed=FOUR,
        cycles=PACKET_CYCLES, fresh=[ECMWF, GEFS],
    ) == "h00_12"


def test_without_a_cycle_map_the_old_rule_stands() -> None:
    conn = _conn()
    _activate(conn, activation_id="h0-old", lead_bucket="h00_12",
              system_ids=[ICON, ECCC])
    _activate(conn, activation_id="h12-18z", lead_bucket="h12_24",
              system_ids=[ECMWF, GEFS], cycles={ECMWF: "18Z", GEFS: "18Z"})
    assert _resolve(conn, ("h00_12", "h12_24"), observed=FOUR) == "h00_12"


def test_the_tie_break_counts_only_freshest_systems_at_their_own_run() -> None:
    from bayes_basket_target.native_ensemble_p0 import _same_run_fresh_count

    fitted = {ICON: "00Z", ECMWF: "18Z", GEFS: "00Z"}
    assert _same_run_fresh_count(
        [ICON, ECMWF, GEFS], fitted, PACKET_CYCLES, [ECMWF, GEFS]
    ) == 1
    assert _same_run_fresh_count([ICON, ECCC], fitted, PACKET_CYCLES, [ECMWF, GEFS]) == 0
    assert _same_run_fresh_count([ECMWF], fitted, PACKET_CYCLES, []) == 0

