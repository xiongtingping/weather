"""A replay scores past days under the tables that were serving then.

Model-layer review 2026-09-13, R4: ``build_native_ensemble_oos`` restricted
its cases to what was knowable at ``as_of_utc`` but loaded the band,
centre/width and station-width tables active now, so a replay of a past build
scored those days under tables fitted on them.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos, table_as_of  # noqa: E402
from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXIS_CENTRE_DISTANCE,
    ensure_band_table_schema,
    persist_and_activate_band_table,
    retire_band_table,
)
from bayes_basket_target.center_width_store import (  # noqa: E402
    persist_and_activate_center_width,
)

OLD = (((0.0, 1.0), 1.1), ((1.0, 99.0), 0.9))
NEW = (((0.0, 1.0), 1.2), ((1.0, 99.0), 0.8))


def _band(conn, factors, at):
    return persist_and_activate_band_table(
        conn, stage="P0", factors=factors, evidence={"schema": "test"},
        serving_day_count=20, axis=BAND_AXIS_CENTRE_DISTANCE,
        fitted_at_utc=at,
    )["band_table_sha256"]


def test_the_band_table_is_the_one_active_at_the_moment() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_band_table_schema(conn)
    assert table_as_of.band_table_as_of(
        conn, stage="P0", as_of_utc="2026-09-01T00:00:00Z"
    ) is None
    old = _band(conn, OLD, "2026-09-02T04:20:00.5Z")
    new = _band(conn, NEW, "2026-09-05T04:20:00Z")
    before = table_as_of.band_table_as_of(
        conn, stage="P0", as_of_utc="2026-09-02T04:20:00Z"
    )
    # Mixed precision: the ".5" activation is after the whole-second moment.
    assert before is None
    between = table_as_of.band_table_as_of(
        conn, stage="P0", as_of_utc="2026-09-04T00:00:00Z"
    )
    assert between == (OLD, old, BAND_AXIS_CENTRE_DISTANCE)
    after = table_as_of.band_table_as_of(
        conn, stage="P0", as_of_utc="2026-09-06T00:00:00Z"
    )
    assert after == (NEW, new, BAND_AXIS_CENTRE_DISTANCE)
    retire_band_table(conn, stage="P0", retired_at_utc="2026-09-07T00:00:00Z")
    # Retirement ends it from that moment, not retroactively.
    assert table_as_of.band_table_as_of(
        conn, stage="P0", as_of_utc="2026-09-06T23:59:59Z"
    ) == (NEW, new, BAND_AXIS_CENTRE_DISTANCE)
    assert table_as_of.band_table_as_of(
        conn, stage="P0", as_of_utc="2026-09-07T00:00:00Z"
    ) is None


def _journal_activation(conn, *, sha, at, delta_c, width_lambda):
    """What ``fit_center_width`` writes beside an activation.

    The table row of a superseded version has its ``activated_at_utc``
    cleared, so the journal is the only record of when it served.
    """

    conn.execute(
        "INSERT INTO calibration_center_width_fit_journal (fit_id, stage,"
        " fitted_at_utc, center_width_sha256, delta_c, width_lambda,"
        " serving_day_count, serving_cell_count, forward_nll_gain, outcome,"
        " evidence_json) VALUES (?, 'P0', ?, ?, ?, ?, 9, 120, 0.01,"
        " 'ACTIVATED', '{}')",
        ("fit-" + at, at, sha, delta_c, width_lambda),
    )
    conn.commit()


def test_the_centre_width_version_carries_its_own_floor_and_tiers() -> None:
    conn = sqlite3.connect(":memory:")
    first = persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.9,
        evidence={"schema": "test"}, serving_day_count=9,
        fitted_at_utc="2026-09-01T00:00:00Z", sigma_floor_rungs=1.0,
    )["center_width_sha256"]
    _journal_activation(
        conn, sha=first, at="2026-09-01T00:00:00Z",
        delta_c=0.0, width_lambda=0.9,
    )
    persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.1, width_lambda=0.75,
        evidence={"schema": "test"}, serving_day_count=9,
        fitted_at_utc="2026-09-03T00:00:00Z",
    )
    early = table_as_of.center_width_as_of(
        conn, stage="P0", as_of_utc="2026-09-02T00:00:00Z"
    )
    assert early["width_lambda"] == pytest.approx(0.9)
    assert early["sigma_floor_rungs"] == pytest.approx(1.0)
    late = table_as_of.center_width_as_of(
        conn, stage="P0", as_of_utc="2026-09-04T00:00:00Z"
    )
    assert (late["delta_c"], late["width_lambda"]) == pytest.approx((0.1, 0.75))
    assert late["sigma_floor_rungs"] == pytest.approx(0.0)


def test_a_replay_build_pins_the_table_that_was_active_then(monkeypatch) -> None:
    from tests.test_native_ensemble_p0 import (
        _insert_official_native_mixed_cycle_archive,
    )
    from tests.test_native_ensemble_oos_p0_pipeline import _insert_fixture

    conn = sqlite3.connect(":memory:")
    cycle_by_system = {
        "ECMWF_IFS_TEST": "18Z",
        "ECMWF_AIFS_TEST": "00Z",
        "NOAA_GEFS_TEST": "18Z",
    }
    _insert_fixture(conn, cycle_by_system=cycle_by_system, lead_bucket="d2")
    _insert_official_native_mixed_cycle_archive(conn, cycle_by_system)
    old = _band(conn, OLD, "2026-06-20T00:00:00Z")
    new = _band(conn, NEW, "2026-06-30T12:00:00Z")
    for system_id, cycle in cycle_by_system.items():
        monkeypatch.setitem(
            native_ensemble_oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS,
            system_id,
            {
                "transport_provider_id": {
                    "ECMWF_IFS_TEST": "ECMWF_OPEN_DATA_S3",
                    "ECMWF_AIFS_TEST": "ECMWF_OPEN_DATA_S3",
                    "NOAA_GEFS_TEST": "NOAA_GEFS_PDS_S3",
                }[system_id],
                "lead_bucket": "d2",
                "run_cycle": cycle,
            },
        )

    def _pins(**kwargs):
        report = native_ensemble_oos.build_native_ensemble_oos(
            conn, station_id="TEST", as_of_utc="2026-07-01T00:00:00Z",
            **kwargs,
        )
        return {
            candidate.get("band_table_sha256")
            for candidate in report["cross_candidates"]
        }

    assert _pins() == {new}
    assert _pins(tables_as_of_utc="2026-06-25T00:00:00Z") == {old}
    assert _pins(tables_as_of_utc="2026-06-10T00:00:00Z") == {None}


def test_the_served_set_is_read_up_to_the_moment_and_not_after() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_probability_envelope ("
        " station_id TEXT, stage TEXT, state TEXT, decision_at_utc TEXT,"
        " lineage_json TEXT);"
        "CREATE TABLE calibration_cross_native_ensemble_artifact ("
        " content_sha256 TEXT, ensemble_system_ids_json TEXT,"
        " run_cycle_by_system_json TEXT);"
    )
    conn.executemany(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES (?,?,?)",
        [
            ("early", '["A","B"]', '{"A":"00Z","B":"00Z"}'),
            ("late", '["A","C"]', '{"A":"00Z","C":"12Z"}'),
        ],
    )
    for decided, sha in (
        ("2026-09-09T05:00:00.000Z", "early"),
        ("2026-09-10T05:00:00.000Z", "late"),
    ):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES"
            " ('KAUS','P0','COMMITTED',?,?)",
            (decided, '{"season_bin":"DESCENT","training_lead_bucket":"h00_12",'
             '"native_cross_ensemble_artifact_sha256":"' + sha + '"}'),
        )
    at_nine = native_ensemble_oos._served_cross_identities(
        conn, station_id="KAUS", as_of_utc="2026-09-09T12:00:00Z"
    )
    assert at_nine == {("DESCENT", "h00_12"): (("A", "B"), {"A": "00Z", "B": "00Z"})}
    now = native_ensemble_oos._served_cross_identities(
        conn, station_id="KAUS", as_of_utc="2026-09-10T12:00:00Z"
    )
    assert now == {("DESCENT", "h00_12"): (("A", "C"), {"A": "00Z", "C": "12Z"})}
