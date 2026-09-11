"""The band lane's station dimension, and its route back to identity.

Two defects, both structural rather than numerical (2026-09-11).

The interval that decides how hard a cell may pull was resampled over days
only, so a factor earned on four stations repeating themselves priced as
confidently as one the whole fleet agreed on -- and the shadow lane's own
numbers say the fleet does not always agree (distance [2,3) realises 1.272
warm against 0.876 cold on the same cell).  The interval now resamples the
station-days within each drawn day as well, a cell reports how many stations
it is made of, and a cell made of too few is refused.

And an active table was held unconditionally while the lane accumulated,
with the only route back to identity a fit that does not run while
accumulating.  The width lane found that trap on 2026-09-10 and the band
lane had the same one, made worse by pinning: deactivating a band table
leaves every artifact pinned to its sha still serving it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target import band_reliability_store as store
from bayes_basket_target.band_reliability_store import (
    BAND_AXIS_CENTRE_DISTANCE,
    BAND_AXIS_CLAIM,
    BAND_VETO_MIN_GAIN,
    MIN_CELL_STATIONS,
    _band_incumbent_veto,
    band_lane_report,
    load_band_table,
    persist_and_activate_band_table,
    retire_band_table,
)
from bayes_basket_target.contracts import ContractError

BANDS = ((0.0, 0.5), (0.5, 1.01))


def _lane(
    *,
    days: int,
    stations: int,
    claim: float = 0.6,
    hit_every: int = 1,
) -> tuple[dict[str, list], dict[str, list[str]]]:
    """A lane whose top cell over-claims: ``claim`` asserted, one hit in
    ``hit_every`` station-days realised."""

    by_day: dict[str, list] = {}
    stations_by_day: dict[str, list[str]] = {}
    index = 0
    for day_index in range(days):
        day = f"2026-08-{day_index + 1:02d}"
        by_day[day] = []
        stations_by_day[day] = []
        for station_index in range(stations):
            hit = 1 if index % hit_every == 0 else 0
            by_day[day].append([(claim, hit), (1.0 - claim, 1 - hit)])
            stations_by_day[day].append(f"ST{station_index:02d}")
            index += 1
    return by_day, stations_by_day


def test_the_station_dimension_widens_the_interval() -> None:
    """Resampling stations as well as days prices a variance the day-only
    bootstrap cannot see, so the interval can only widen -- and a factor is
    the interval end nearest one, so a wider interval pulls less hard."""

    by_day, stations = _lane(days=12, stations=40, hit_every=3)
    day_only = band_lane_report(by_day, bands=BANDS, bootstrap_samples=400)
    two_way = band_lane_report(
        by_day, bands=BANDS, bootstrap_samples=400,
        stations_by_day=stations,
    )
    for band in BANDS:
        one = day_only[band]["interval"]
        two = two_way[band]["interval"]
        assert one[0] == pytest.approx(one[0])
        assert (two[1] - two[0]) >= (one[1] - one[0]) - 1e-9, band
        # The point estimate is the same sample either way; only the
        # uncertainty around it changes.
        assert two_way[band]["ratio"] == pytest.approx(day_only[band]["ratio"])


def test_a_cell_made_of_too_few_stations_is_refused() -> None:
    """Same days, same claimed mass, same realised ratio -- four stations
    instead of forty.  The evidence for the fleet is not there."""

    wide_day, wide_stations = _lane(days=12, stations=40, hit_every=3)
    narrow_day, narrow_stations = _lane(days=12, stations=4, hit_every=3)
    wide = band_lane_report(
        wide_day, bands=BANDS, bootstrap_samples=400,
        stations_by_day=wide_stations, min_cell_stations=MIN_CELL_STATIONS,
    )
    narrow = band_lane_report(
        narrow_day, bands=BANDS, bootstrap_samples=400,
        stations_by_day=narrow_stations, min_cell_stations=MIN_CELL_STATIONS,
    )
    top = BANDS[1]
    assert wide[top]["station_count"] == 40
    assert narrow[top]["station_count"] == 4
    assert narrow[top]["blocked_by"] == "too_few_stations"
    assert not narrow[top]["qualified"]
    assert narrow[top]["conservative_factor"] == 1.0


def test_the_floor_is_off_for_a_lane_that_names_no_stations() -> None:
    """The gate archive and the replay lanes pass no stations, and a floor
    that refused them would refuse everything they have ever fitted."""

    by_day, _stations = _lane(days=12, stations=4, hit_every=3)
    report = band_lane_report(by_day, bands=BANDS, bootstrap_samples=400)
    for band in BANDS:
        assert report[band]["station_count"] is None
        assert report[band]["blocked_by"] != "too_few_stations"


def test_a_cell_reports_what_one_station_and_one_day_are_worth() -> None:
    """Three questions a pooled factor should have to answer, recorded on
    every cell: do the stations agree on the sign, and does the verdict
    survive losing its largest station or its largest day."""

    by_day, stations = _lane(days=12, stations=40, hit_every=3)
    report = band_lane_report(
        by_day, bands=BANDS, bootstrap_samples=200,
        stations_by_day=stations,
    )
    cell = report[BANDS[1]]
    assert cell["station_day_count"] == 12 * 40
    # Every station is built from the same over-claim here, so they agree.
    assert cell["leave_one_station_sign_agreement"] == pytest.approx(1.0)
    assert cell["drop_largest_station_ratio"] == pytest.approx(
        cell["ratio"], abs=0.05
    )
    assert cell["drop_largest_day_ratio"] == pytest.approx(
        cell["ratio"], abs=0.05
    )


def test_the_veto_reads_only_station_days_decided_after_the_activation() -> None:
    """Event date will not answer the question: a decision for an event two
    days out is taken two days early, so a day after an activation routinely
    carries a decision from before it -- a cell the incumbent never served."""

    by_day, stations = _lane(days=4, stations=40, hit_every=3)
    # The lane already over-claims its top cell (0.6 asserted, one hit in
    # three realised), so a factor that pushes it UP is a table that hurts.
    factors = {BANDS[0]: 1.0, BANDS[1]: 1.4}
    before = {
        (station, day): "2026-08-01T00:00:00Z"
        for day, names in stations.items() for station in names
    }
    verdict = _band_incumbent_veto(
        by_day, stations, before, factors=factors,
        axis=BAND_AXIS_CLAIM,
        activated_at_utc="2026-08-30T00:00:00Z",
    )
    assert verdict["station_day_count"] == 0
    assert not verdict["vetoed"]

    after = {key: "2026-08-31T00:00:00Z" for key in before}
    fired = _band_incumbent_veto(
        by_day, stations, after, factors=factors,
        axis=BAND_AXIS_CLAIM,
        activated_at_utc="2026-08-30T00:00:00Z",
    )
    assert fired["station_day_count"] == 4 * 40
    assert fired["undo_gain"] > BAND_VETO_MIN_GAIN
    assert fired["vetoed"]


def test_an_incumbent_that_is_helping_is_not_vetoed() -> None:
    """The question is "is this hurting", never "is this best".  A table
    that wins on what it served stays, and so does one with nothing to
    say."""

    by_day, stations = _lane(days=4, stations=40, hit_every=3)
    decisions = {
        (station, day): "2026-08-31T00:00:00Z"
        for day, names in stations.items() for station in names
    }
    helping = _band_incumbent_veto(
        by_day, stations, decisions,
        # The lane realises one hit in three on a 0.6 claim, so pulling the
        # top cell DOWN is the correction it wants.
        factors={BANDS[0]: 1.0, BANDS[1]: 0.5},
        axis=BAND_AXIS_CLAIM,
        activated_at_utc="2026-08-30T00:00:00Z",
    )
    assert helping["undo_gain"] < 0
    assert not helping["vetoed"]

    identity = _band_incumbent_veto(
        by_day, stations, decisions,
        factors={BANDS[0]: 1.0, BANDS[1]: 1.0},
        axis=BAND_AXIS_CLAIM,
        activated_at_utc="2026-08-30T00:00:00Z",
    )
    assert not identity["vetoed"]


def test_a_veto_needs_its_own_window_before_it_fires() -> None:
    conn = sqlite3.connect(":memory:")
    by_day, stations = _lane(days=2, stations=10, hit_every=3)
    decisions = {
        (station, day): "2026-08-31T00:00:00Z"
        for day, names in stations.items() for station in names
    }
    verdict = _band_incumbent_veto(
        by_day, stations, decisions,
        factors={BANDS[0]: 1.0, BANDS[1]: 1.4},
        axis=BAND_AXIS_CLAIM,
        activated_at_utc="2026-08-30T00:00:00Z",
    )
    assert verdict["day_count"] == 2
    assert not verdict["vetoed"]
    assert conn  # the verdict is pure; the connection is only the fixture


def _persist(conn: sqlite3.Connection, factors, stage: str = "P0") -> str:
    outcome = persist_and_activate_band_table(
        conn, stage=stage, factors=factors, evidence={},
        serving_day_count=10, axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    return str(outcome["band_table_sha256"])


def test_a_retired_table_serves_identity_to_the_artifacts_pinned_to_it() -> None:
    """Deactivating stops the unpinned readers; the retirement stamp stops
    the pinned ones.  Without the second, a vetoed table keeps serving every
    artifact minted before the veto until that artifact expires."""

    conn = sqlite3.connect(":memory:")
    store.ensure_band_table_schema(conn)
    factors = (((0.0, 1.0), 1.0), ((1.0, 99.0), 0.5))
    sha = _persist(conn, factors)
    assert load_band_table(conn, stage="P0", table_sha256=sha)[0] == factors

    outcome = retire_band_table(conn, stage="P0")
    assert outcome["state"] == "DEACTIVATED"
    assert outcome["band_table_sha256"] == sha
    # Unpinned: nothing active any more.
    assert load_band_table(conn, stage="P0") is None
    # Pinned: identity, with the sha kept so a receipt can say which
    # version was retired out from under it.
    pinned = load_band_table(conn, stage="P0", table_sha256=sha)
    assert pinned == ((), sha)


def test_a_table_merely_superseded_keeps_serving_its_pins() -> None:
    """Retirement is a finding that the table was WRONG.  Being replaced by
    a better one is not that, and charter 60 says the pin reproduces the
    exact math its holdout was scored against."""

    conn = sqlite3.connect(":memory:")
    store.ensure_band_table_schema(conn)
    first = (((0.0, 1.0), 1.0), ((1.0, 99.0), 0.5))
    second = (((0.0, 1.0), 1.0), ((1.0, 99.0), 0.8))
    old = _persist(conn, first)
    _persist(conn, second)
    assert load_band_table(conn, stage="P0", table_sha256=old)[0] == first


def test_a_pinned_read_survives_a_store_the_migration_has_not_reached() -> None:
    """The resident serves without running the schema, so between a deploy
    and the next sweep the column is absent.  That is not a missing table."""

    conn = sqlite3.connect(":memory:")
    store.ensure_band_table_schema(conn)
    sha = _persist(conn, (((0.0, 1.0), 1.0), ((1.0, 99.0), 0.5)))
    rows = conn.execute(
        "SELECT band_table_sha256, stage, factors_json, evidence_json,"
        " fitted_at_utc, serving_day_count, activated, activated_at_utc, axis"
        " FROM calibration_band_reliability_table"
    ).fetchall()
    conn.execute("DROP TABLE calibration_band_reliability_table")
    conn.execute(
        "CREATE TABLE calibration_band_reliability_table ("
        " band_table_sha256 TEXT PRIMARY KEY, stage TEXT, factors_json TEXT,"
        " evidence_json TEXT, fitted_at_utc TEXT, serving_day_count INTEGER,"
        " activated INTEGER, activated_at_utc TEXT, axis TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_band_reliability_table VALUES"
        " (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    loaded = load_band_table(conn, stage="P0", table_sha256=sha)
    assert loaded is not None and loaded[1] == sha
    assert loaded[0]


def test_a_missing_pin_still_fails_closed() -> None:
    conn = sqlite3.connect(":memory:")
    store.ensure_band_table_schema(conn)
    with pytest.raises(ContractError):
        load_band_table(conn, stage="P0", table_sha256="0" * 64)


def test_a_read_only_store_reads_rather_than_raising() -> None:
    """Every read path calls the schema, and some of those connections are
    opened read-only.  A migration such a connection cannot run is the next
    writer's job, not a reason to fail the reader."""

    import tempfile
    import pathlib

    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / "store.db"
        writable = sqlite3.connect(str(path))
        store.ensure_band_table_schema(writable)
        sha = _persist(writable, (((0.0, 1.0), 1.0), ((1.0, 99.0), 0.5)))
        writable.execute(
            "ALTER TABLE calibration_band_reliability_table"
            " RENAME COLUMN retired_at_utc TO retired_at_utc_old"
        )
        writable.commit()
        writable.close()
        reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        # Neither the migration nor the pinned read may raise.
        store.ensure_band_table_schema(reader)
        loaded = load_band_table(reader, stage="P0", table_sha256=sha)
        assert loaded is not None and loaded[1] == sha


def test_the_journal_can_say_a_table_was_vetoed() -> None:
    conn = sqlite3.connect(":memory:")
    store.ensure_band_table_schema(conn)
    live = str(conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='calibration_band_reliability_fit_journal'"
    ).fetchone()[0])
    assert "'INCUMBENT_VETOED'" in live
    fit_id = store.journal_band_fit(
        conn,
        stage="P0",
        fitted={
            "factors": (((0.0, 1.0), 1.0),),
            "evidence": {"basis": "INCUMBENT_VETOED"},
            "serving_day_count": 4,
            "serving_station_day_count": 164,
        },
        outcome="INCUMBENT_VETOED",
        axis=BAND_AXIS_CENTRE_DISTANCE,
    )
    row = conn.execute(
        "SELECT outcome, evidence_json FROM"
        " calibration_band_reliability_fit_journal WHERE fit_id=?",
        (fit_id,),
    ).fetchone()
    assert row[0] == "INCUMBENT_VETOED"
    assert json.loads(row[1])["basis"] == "INCUMBENT_VETOED"
