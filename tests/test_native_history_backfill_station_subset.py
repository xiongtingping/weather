"""A station that joined the fleet after its dates were walked is walked alone.

2026-10-04: LLBG, LTFM and UUWW joined a fleet whose history had been walked
at 45 stations.  The driver counted presence over the whole roster, so every
such date read "45 present, 48 wanted" and was refused as a partial capture
that cannot be topped up -- true for a station already present, and false for
one that is not: the archive's unique key carries the station.  With
``--station-id`` presence counts the named stations alone.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_run_native_history_backfill_station_subset",
    ROOT / "scripts" / "run_native_history_backfill.py",
)
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)


def _db(tmp_path: Path, stations) -> Path:
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_historical_member_run("
        "ensemble_system_id TEXT, transport_provider_id TEXT, qc_status TEXT,"
        "target_local_date TEXT, extraction_version TEXT, station_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_historical_member_run "
        "VALUES('NOAA_GEFS_05','NOAA_GEFS_PDS_S3','ACCEPTED','2026-09-20','v',?)",
        [(s,) for s in stations],
    )
    conn.commit()
    conn.close()
    return path


def test_presence_counts_the_named_stations_alone(tmp_path) -> None:
    db = _db(tmp_path, ["LTAC", "EGLC", "LLBG"])
    scope = dict(system_id="NOAA_GEFS_05", transport="NOAA_GEFS_PDS_S3",
                 event_date="2026-09-20", extraction_version="v")
    assert driver._stations_present(db, **scope) == 3
    assert driver._stations_present(db, **scope, station_ids=["LTFM", "UUWW"]) == 0
    assert driver._stations_present(db, **scope, station_ids=["llbg", "LTFM"]) == 1


def test_the_ai_ensembles_are_walked_by_the_same_driver() -> None:
    """AIFS ENS and AIGEFS keep their history on official storage, so a late
    station can be walked back over its truth like GEFS and IFS; the driver
    reaches both through one script that takes the system as an argument."""

    for provider, system_id in (("aifs", "ECMWF_AIFS_ENS_GLOBAL"), ("aigefs", "NOAA_AIGEFS_OPS_025")):
        spec = driver.PROVIDERS[provider]
        assert spec["system_id"] == system_id
        assert spec["script"].name == "backfill_ai_ensemble_multistation_archive.py"
        assert spec["script"].exists()
        assert spec["script_args"] == ["--system", provider]
        assert spec["extraction_version"](2, 12).endswith("_12z_d2_v1")
