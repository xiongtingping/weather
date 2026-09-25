"""The radiation a holdout day reads is the one known at its cutoff.

2026-09-21: the physics table is rewritten until each day's own evening, so a
holdout reading it scored a same-day radiation (P0 residual correlation +0.237
against +0.112 for the value actually served).  The resolver reads the
append-only versions; the backfill recovers them from served P0 receipts.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import forecast_physics_asof as ASOF

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _versions_db(tmp_path):
    capture = _script("capture_station_forecast_physics")
    conn = sqlite3.connect(tmp_path / "ssot.db")
    capture.ensure_schema(conn)
    return capture, conn


def _version(conn, day, stamp, value, *, mode="forward", source=ASOF.FORWARD_SOURCE_ID):
    conn.execute(
        f"INSERT INTO {ASOF.VERSION_TABLE} VALUES "
        "('ZUUU',?,?,?,NULL,NULL,NULL,30.6,104.0,'Asia/Shanghai',?,?,1,?)",
        (day, source, value, mode, stamp, f"sha-{stamp}"),
    )


def test_the_script_and_the_resolver_name_the_same_tables(tmp_path) -> None:
    capture = _script("capture_station_forecast_physics")
    assert capture.TABLE == ASOF.TABLE
    assert capture.VERSION_TABLE == ASOF.VERSION_TABLE
    assert capture.SOURCE_ID == ASOF.FORWARD_SOURCE_ID


def test_a_version_captured_after_the_cutoff_is_not_known(tmp_path) -> None:
    _, conn = _versions_db(tmp_path)
    _version(conn, "2026-09-22", "2026-09-20T12:00:00.000Z", 18.0)
    _version(conn, "2026-09-22", "2026-09-21T06:00:00.123456Z", 15.0)
    _version(conn, "2026-09-22", "2026-09-22T12:00:00Z", 9.0)   # the day's own evening
    value, receipt = ASOF.resolve_radiation_as_of(
        conn, station_id="ZUUU", local_date="2026-09-22", cutoff_utc="2026-09-21T07:00:00Z",
    )
    assert value == 15.0 and receipt["state"] == "RESOLVED"
    assert receipt["version_age_hours"] < 1.0
    value, _ = ASOF.resolve_radiation_as_of(
        conn, station_id="ZUUU", local_date="2026-09-22", cutoff_utc="2026-09-21T05:59:59Z",
    )
    assert value == 18.0
    value, receipt = ASOF.resolve_radiation_as_of(
        conn, station_id="ZUUU", local_date="2026-09-22", cutoff_utc="2026-09-19T00:00:00Z",
    )
    assert value is None and receipt["state"] == "NOT_KNOWN_AT_CUTOFF"


def test_instants_not_strings_decide_the_order(tmp_path) -> None:
    _, conn = _versions_db(tmp_path)
    # As strings "…12.230Z" sorts after "…12.230000Z"; as instants they are equal,
    # and a version stamped exactly at the cutoff is known.
    _version(conn, "2026-09-22", "2026-09-21T06:00:12.230000Z", 11.0)
    value, _ = ASOF.resolve_radiation_as_of(
        conn, station_id="ZUUU", local_date="2026-09-22", cutoff_utc="2026-09-21T06:00:12.230Z",
    )
    assert value == 11.0


def test_historical_rows_are_never_an_as_of_forecast(tmp_path) -> None:
    _, conn = _versions_db(tmp_path)
    _version(conn, "2026-08-01", "2026-07-20T00:00:00Z", 20.0, mode="historical")
    value, receipt = ASOF.resolve_radiation_as_of(
        conn, station_id="ZUUU", local_date="2026-08-01", cutoff_utc="2026-07-31T00:00:00Z",
    )
    assert value is None and receipt["state"] == "NO_VERSIONS"


def test_receipts_become_one_version_per_change_stamped_at_first_sight(tmp_path) -> None:
    backfill = _script("backfill_radiation_versions_from_p0_receipts")
    receipt = lambda v: json.dumps({"radiation_sum": v, "state": "RESOLVED"})  # noqa: E731
    rows = [
        ("2026-09-02", "2026-09-01T00:10:00.000Z", receipt(20.0)),
        ("2026-09-02", "2026-09-01T00:20:00.000Z", receipt(20.0)),
        ("2026-09-02", "2026-09-01T06:40:00.000Z", receipt(17.5)),
        ("2026-09-02", "2026-09-01T12:40:00.000Z", receipt(20.0)),
        ("2026-09-02", "2026-09-01T13:00:00.000Z", None),
        ("2026-09-02", "2026-09-01T13:10:00.000Z", json.dumps({"state": "NO_FORECAST_RADIATION"})),
        ("2026-09-02", "2026-09-01T13:20:00.000Z", "@ref/abc"),
    ]
    assert backfill.receipt_versions(rows) == {
        "2026-09-02": [
            ("2026-09-01T00:10:00.000Z", 20.0),
            ("2026-09-01T06:40:00.000Z", 17.5),
            ("2026-09-01T12:40:00.000Z", 20.0),
        ]
    }


def _holdout_db(tmp_path, *, day_value: float, day_captured: str):
    from datetime import date, timedelta

    capture, conn = _versions_db(tmp_path)
    event = date(2026, 9, 19)
    for i in range(1, 41):
        d = (event - timedelta(days=i)).isoformat()
        conn.execute(
            f"INSERT INTO {ASOF.TABLE}({capture.COLUMNS}) VALUES "
            "('ZUUU',?,?,?,NULL,NULL,NULL,30.6,104.0,'Asia/Shanghai','forward',?,1,?)",
            (d, ASOF.FORWARD_SOURCE_ID, 12.0 + (i % 7) * 0.5, d + "T12:00:00Z", f"h{i}"),
        )
    # Today's table row is the evening value; the version known earlier differs.
    conn.execute(
        f"INSERT INTO {ASOF.TABLE}({capture.COLUMNS}) VALUES "
        "('ZUUU','2026-09-19',?,9.0,NULL,NULL,NULL,30.6,104.0,'Asia/Shanghai','forward',"
        "'2026-09-19T12:00:00Z',1,'evening')",
        (ASOF.FORWARD_SOURCE_ID,),
    )
    _version(conn, "2026-09-19", day_captured, day_value)
    _version(conn, "2026-09-19", "2026-09-19T12:00:00Z", 9.0)
    conn.commit()
    return conn


def _predictions(centers, decided="2026-09-18T06:00:00Z"):
    return {
        system: {"2026-09-19": {"calibrated_center_c": value, "decision_at_utc": decided}}
        for system, value in centers.items()
    }


def test_the_holdout_shift_is_the_served_shift_on_the_same_distribution(tmp_path) -> None:
    from bayes_basket_target import native_ensemble_oos as OOS
    from bayes_basket_target import native_ensemble_p0 as P0

    conn = _holdout_db(tmp_path, day_value=19.0, day_captured="2026-09-17T18:00:00Z")
    # Both systems at 25C: the retired proxy (half the centre range) was 0 here
    # and certified no shift while serving shifted by the distribution's spread.
    signals = OOS._holdout_conditioning_signals(
        conn, station_id="ZUUU", holdout_days=["2026-09-19"],
        predictions_by_system=_predictions({"NOAA_GEFS_05": 25.0, "ECMWF_IFS_ENS_OPEN_PF50": 25.0}),
        radiation_shift_sigmas=(0.15, 0.15),
    )["2026-09-19"]
    assert signals["radiation_as_of_state"] == "RESOLVED"
    shift_sigma = signals["synoptic_cooling_shift_sigma"]
    assert shift_sigma and shift_sigma > 0.0
    grid = [str(v) for v in range(20, 31)]
    base = {k: 0.0 for k in grid}
    base.update({"24": 0.25, "25": 0.5, "26": 0.25})
    held = OOS._holdout_shape_stack(
        base, grid=grid, cooling_shift_sigma=shift_sigma, station_latitude=30.6,
        neighbour_center_c=25.0, center_width_layer=None, band_layer=None,
    )
    intervals = {k: [int(k) - 0.5, int(k) + 0.5] for k in grid}
    centers = [float(int(k)) for k in grid]
    spread = P0._distribution_sigma_c(base, centers)
    served = P0._shift_probability_c(
        base, ordered=grid, bucket_centers_c=[c + shift_sigma * spread for c in centers],
        intervals=intervals,
    )
    assert held == {k: pytest.approx(v, abs=1e-12) for k, v in served.items()}
    assert held != base


def test_a_radiation_known_only_after_the_cutoff_leaves_the_day_unshifted(tmp_path) -> None:
    from bayes_basket_target import native_ensemble_oos as OOS

    conn = _holdout_db(tmp_path, day_value=19.0, day_captured="2026-09-18T09:00:00Z")
    signals = OOS._holdout_conditioning_signals(
        conn, station_id="ZUUU", holdout_days=["2026-09-19"],
        predictions_by_system=_predictions({"NOAA_GEFS_05": 24.0, "ECMWF_IFS_ENS_OPEN_PF50": 26.0}),
        radiation_shift_sigmas=(0.15, 0.15),
    ).get("2026-09-19") or {}
    assert signals.get("synoptic_cooling_shift_sigma") is None
    assert signals.get("radiation_as_of_state") in (None, "NOT_KNOWN_AT_CUTOFF")


def test_what_came_after_the_cutoff_cannot_move_the_holdout(tmp_path) -> None:
    from bayes_basket_target import native_ensemble_oos as OOS

    predictions = _predictions({"NOAA_GEFS_05": 24.0, "ECMWF_IFS_ENS_OPEN_PF50": 26.0})
    outs = []
    for evening in (9.0, 25.0):
        folder = tmp_path / f"e{evening:g}"
        folder.mkdir()
        conn = _holdout_db(folder, day_value=19.0, day_captured="2026-09-17T18:00:00Z")
        conn.execute(f"DROP TRIGGER {ASOF.VERSION_TABLE}_no_update")
        conn.execute(
            f"UPDATE {ASOF.VERSION_TABLE} SET shortwave_radiation_sum=? "
            "WHERE captured_at_utc='2026-09-19T12:00:00Z'", (evening,),
        )
        conn.execute(f"UPDATE {ASOF.TABLE} SET shortwave_radiation_sum=? WHERE local_date='2026-09-19'", (evening,))
        outs.append(OOS._holdout_conditioning_signals(
            conn, station_id="ZUUU", holdout_days=["2026-09-19"],
            predictions_by_system=predictions, radiation_shift_sigmas=(0.15, 0.15),
        )["2026-09-19"]["synoptic_cooling_shift_sigma"])
    assert outs[0] == outs[1] and outs[0] is not None
