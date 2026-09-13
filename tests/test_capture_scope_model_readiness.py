"""Capture scopes whose backing runs have published come first.

A run existing is not a run being complete: members land on the object store
over the following hours.  Capture slots are scarce and a capture costs
minutes, so spending one on a scope whose fields cannot resolve yet delays
every scope that was actually ready.

An open station-day appears once per lead, because P0 is recomputed as the
models update rather than decided once.  Readiness outranks the timezone order;
inside a readiness group the east-to-west day order still holds, and inside one
station-day the shortest lead -- the freshest run -- leads.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target.native_ensemble_contract import (
    MIN_CROSS_ENSEMBLE_FAMILIES,
)
from bayes_basket_target.native_ensemble_oos import (
    OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
)
from bayes_basket_target.official_nwp_capture_scheduler import (
    D2_PUBLICATION_MARGIN_HOURS,
    SUPPORTED_LEAD_DAYS,
    contracted_run_inits,
    registry_capture_scopes,
    run_ready_at_utc,
    scope_ready_at_utc,
)


def _registry(rows):
    return {"rows": rows}


def _row(station, event_date, tz, lat=0.0, lon=0.0):
    return {
        "station_id": station,
        "event_date": event_date,
        "settlement_timezone": tz,
        "outcome": f"{station}-{event_date}-yes",
        "source_eligible": 1,
        "market_active": 1,
        "market_accepting_orders": 1,
        "latitude": lat,
        "longitude": lon,
    }


def _first_per_station(scopes):
    """The scope each station is served first, in the order stations appear."""

    seen: dict[str, dict] = {}
    for item in scopes:
        seen.setdefault(item["station_ids"][0], item)
    return seen


def test_ready_at_is_the_run_plus_a_publication_margin() -> None:
    run_init = datetime(2026, 8, 7, 18, tzinfo=timezone.utc)

    assert run_ready_at_utc(run_init) == run_init + timedelta(
        hours=D2_PUBLICATION_MARGIN_HOURS
    )


def test_margin_is_non_zero_so_a_just_initialised_run_is_not_ready() -> None:
    # The pre-existing guard only rejected `query_at <= run_init`, which treats
    # a run as usable the instant it initialises.
    run_init = datetime(2026, 8, 7, 18, tzinfo=timezone.utc)

    assert D2_PUBLICATION_MARGIN_HOURS > 0
    assert run_ready_at_utc(run_init) > run_init


@pytest.mark.parametrize(
    "run_hour,expected",
    [(0, True), (12, True), (18, False)],
)
def test_readiness_flag_tracks_the_clock(run_hour: int, expected: bool) -> None:
    # 01:00Z, an hour short of the 18Z run's 02:00Z readiness, so the newest
    # cycle is the one still pending rather than one sitting on the boundary.
    as_of = datetime(2026, 8, 8, 1, tzinfo=timezone.utc)
    run_init = datetime(2026, 8, 7, run_hour, tzinfo=timezone.utc)

    assert (run_ready_at_utc(run_init) <= as_of) is expected


def test_runs_come_from_the_archive_contracts_not_a_fixed_cycle() -> None:
    # The archives publish the fields this extraction needs on different
    # cycles.  A schedule that assumed one cycle would name runs three of the
    # four collectors never read.
    runs = contracted_run_inits("2026-08-09", lead_days=2)

    assert set(runs) == set(OFFICIAL_ARCHIVE_SCOPE_CONTRACTS)
    for system_id, run in runs.items():
        contract = OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[system_id]["leads"]["d2"]
        # A lead contracts a list of cycles (ECCC reads 00Z and 12Z at d2);
        # the schedule names the latest, its conservative readiness bound.
        cycles = [contract] if isinstance(contract, dict) else list(contract)
        assert run.strftime("%HZ") == max(
            (entry["run_cycle"] for entry in cycles),
            key=lambda cycle: int(cycle[:-1]),
        )
        # The lead is the number of days between the run and the event date.
        assert run.date().isoformat() == "2026-08-07"
    assert len({run.hour for run in runs.values()}) > 1


def test_scope_is_ready_once_two_families_have_landed() -> None:
    # One family cannot produce a cross-family P0, and waiting for the last
    # would hold d0 back until after the event day had ended for most stations.
    ready = sorted(
        run_ready_at_utc(run)
        for run in contracted_run_inits("2026-08-09", lead_days=2).values()
    )

    assert MIN_CROSS_ENSEMBLE_FAMILIES == 2
    assert scope_ready_at_utc("2026-08-09", lead_days=2) == ready[1]
    # ECCC and ICON both publish their latest d2 run at 12Z, so the first two
    # families can land together; what must hold is that the scope does not
    # wait for the last one.
    assert ready[0] <= scope_ready_at_utc("2026-08-09", lead_days=2) < ready[-1]


def test_a_station_day_is_captured_once_per_lead() -> None:
    # P0 is recomputed as the models update: the same day is taken at d2, again
    # at d1, and again at d0.  Collapsing it to one scope is what used to leave
    # P0 frozen on the forecast made two days out.
    rows = [_row("KMIA", "2026-08-09", "America/New_York")]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-08T06:00:00Z", max_scopes=500
    )

    assert [item["lead_days"] for item in scopes] == [2, 0, 1]
    assert {item["event_date"] for item in scopes} == {"2026-08-09"}
    assert len(SUPPORTED_LEAD_DAYS) == 3


def test_shortest_ready_lead_of_a_station_day_is_served_first() -> None:
    # Late on the event day itself: KMIA's 08-09 runs to 08-10 04:00Z, and by
    # 22:00Z even the d0 runs have landed, so nothing is deprioritised on
    # readiness and the freshest run decides the order.
    rows = [_row("KMIA", "2026-08-09", "America/New_York")]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-09T22:00:00Z", max_scopes=500
    )

    assert all(item["model_ready"] for item in scopes)
    assert [item["lead_days"] for item in scopes] == [0, 1, 2]


def test_ready_scopes_sort_ahead_of_unready_ones() -> None:
    # KMIA's event day opens earlier in UTC than NZWN's, but its runs are
    # further from publication; the ready scope must still be served first.
    rows = [
        _row("KMIA", "2026-08-09", "America/New_York"),
        _row("NZWN", "2026-08-08", "Pacific/Auckland"),
    ]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=500
    )
    first = _first_per_station(scopes)
    order = [item["station_ids"][0] for item in scopes]

    assert order.index("NZWN") < order.index("KMIA")
    assert first["NZWN"]["model_ready"] is True
    assert first["KMIA"]["model_ready"] is False


def test_timezone_order_is_preserved_within_a_readiness_group() -> None:
    rows = [
        _row("KLGA", "2026-08-08", "America/New_York"),
        _row("NZWN", "2026-08-08", "Pacific/Auckland"),
        _row("ZBAA", "2026-08-08", "Asia/Shanghai"),
    ]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=500
    )
    ready = [item for item in scopes if item["model_ready"]]
    order = list(dict.fromkeys(item["station_ids"][0] for item in ready))

    # East-to-west day-start order decides between stations: +12, +8, -4.
    assert order == ["NZWN", "ZBAA", "KLGA"]


def test_every_scope_publishes_its_readiness_timestamp_and_runs() -> None:
    rows = [_row("WSSS", "2026-08-08", "Asia/Singapore")]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=500
    )
    by_lead = {item["lead_days"]: item for item in scopes}

    # Operators need to see why a scope was deprioritised, not just that it
    # was, and which run each family will be read at.
    assert by_lead[2]["model_ready_at_utc"] == scope_ready_at_utc(
        "2026-08-08", lead_days=2
    ).isoformat().replace("+00:00", "Z")
    assert by_lead[2]["run_init_utc_by_system"]["NOAA_GEFS_05"] == (
        "2026-08-06T18:00:00Z"
    )
    assert all(item.get("model_ready_at_utc") for item in scopes)
    assert all(item.get("run_init_utc_by_system") for item in scopes)


def test_unready_scopes_are_deprioritised_not_dropped() -> None:
    # Dropping them would make the catalogue shrink and grow across polls and
    # would starve a scope whose run publishes mid-sweep.
    rows = [_row("KMIA", "2026-08-09", "America/New_York")]

    scopes = registry_capture_scopes(
        _registry(rows), as_of_utc="2026-08-07T06:00:00Z", max_scopes=500
    )

    assert len(scopes) == len(SUPPORTED_LEAD_DAYS)
    assert any(item["model_ready"] is False for item in scopes)
