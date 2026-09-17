"""The H5-vs-H15 accuracy reading corrects the way the server does.

The server centres a system on ``mean + resolved bias``
(``native_ensemble_oos._corrected_center``).  The board's reading subtracted
``bias_c`` until 2026-09-17, which doubled the bias on both arms and put
each corrected error near 2.8C; the verdict it drew compared two wrong
corrections.  One station-day pins the direction: H15 corrects the forecast
onto truth exactly, H5 leaves it one degree short, so H15 must be the closer
one -- the subtracting version called H5 closer.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_native_ensemble_system_artifact("
        " station_id TEXT, ensemble_system_id TEXT, lead_bucket TEXT,"
        " run_cycle TEXT, valid_from_utc TEXT, bias_parameters_json TEXT);"
        "CREATE TABLE calibration_station_daily_high_truth("
        " station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        " qc_status TEXT);"
        "CREATE TABLE calibration_native_ensemble_historical_member_run("
        " station_id TEXT, ensemble_system_id TEXT, target_local_date TEXT,"
        " archive_lead_bucket TEXT, run_cycle TEXT, raw_tmax_c REAL,"
        " qc_status TEXT);"
    )
    return conn


def _artifact(conn, valid_from: str, bias_c: float) -> None:
    conn.execute(
        "INSERT INTO calibration_native_ensemble_system_artifact"
        " VALUES ('KAAA','SYS','h12_24','00',?,?)",
        (valid_from, json.dumps({"bias_c": bias_c})),
    )


def test_the_correction_adds_the_bias_the_way_the_server_does() -> None:
    conn = _store()
    day = "2026-09-10"
    # Under H15 the scope had learnt the whole +2C; the H5 refit, before the
    # event day, only +1C.
    _artifact(conn, "2026-08-20T00:00:00Z", 2.0)
    _artifact(conn, "2026-09-01T00:00:00Z", 1.0)
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth"
        " VALUES ('KAAA', ?, 22.0, 'ACCEPTED')", (day,),
    )
    for raw in (19.0, 20.0, 21.0):
        conn.execute(
            "INSERT INTO calibration_native_ensemble_historical_member_run"
            " VALUES ('KAAA','SYS',?,'h12_24','00',?,'ACCEPTED')",
            (day, raw),
        )

    result = C.collect_h5_bias_accuracy(conn)

    assert result["station_days"] == 1
    assert result["mae_h15_c"] == pytest.approx(0.0)
    assert result["mae_h5_c"] == pytest.approx(1.0)
    assert result["mean_delta_c"] == pytest.approx(1.0)
    assert (result["h5_better"], result["h5_worse"]) == (0, 1)
