"""The pr12 projection is minted over the fleet, not over the registry table.

calibration_station_registry is append-only and onboarding writes a
station's row before the station is admitted, so since 2026-08-28 the table
has carried LLBG, LTFM and UUWW while the fleet was 45.  Minting from the
whole table produced a projection the validator refuses at any fleet size.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from bayes_basket_target.station_registry import coordinate_registry_manifest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "build_pr12_authority_scope_projection",
        ROOT / "scripts" / "build_pr12_authority_scope_projection.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database(path: Path, stations) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE calibration_station_registry("
        " station_id TEXT, settlement_definition TEXT, content_sha256 TEXT);"
        "CREATE TABLE basket_target_market_registry("
        " station_id TEXT, settlement_timezone TEXT, source_eligible INTEGER);"
        "CREATE TABLE calibration_station_market_timezone_binding("
        " station_id TEXT, settlement_timezone TEXT);"
    )
    for station in stations:
        conn.execute(
            "INSERT INTO calibration_station_registry VALUES(?,?,?)",
            (station, "official_daily_tmax_c", "a" * 64),
        )
        conn.execute(
            "INSERT INTO calibration_station_market_timezone_binding VALUES(?,?)",
            (station, "UTC"),
        )
    conn.commit()
    conn.close()
    return path


def _run(module, monkeypatch, tmp_path, database):
    seen: dict[str, object] = {}

    def fake_build(**kwargs):
        seen.update(kwargs)
        return {"state": "SEALED", "station_count": len(kwargs["station_registry"]),
                "content_sha256": "b" * 64}

    monkeypatch.setattr(module, "resolve_global_awc_source_authority",
                        lambda conn, station_id: {"station_id": station_id})
    monkeypatch.setattr(module, "build_pr12_authority_scope_projection", fake_build)
    monkeypatch.setattr(module, "write_bounded_json_if_changed", lambda *a, **k: None)
    doctrine = tmp_path / "doctrine"
    doctrine.mkdir(exist_ok=True)
    monkeypatch.setattr(sys, "argv", [
        "build_pr12_authority_scope_projection.py",
        "--database", str(database), "--doctrine-directory", str(doctrine),
        "--as-of-utc", "2026-09-21T00:00:00Z", "--output", str(tmp_path / "out.json"),
    ])
    assert module.main() == 0
    return seen


def test_rows_for_stations_outside_the_fleet_stay_out(tmp_path, monkeypatch):
    fleet = list(coordinate_registry_manifest()["station_ids"])
    database = _database(tmp_path / "ssot.db", fleet + ["LLBG", "ZZZZ"])

    seen = _run(_load(), monkeypatch, tmp_path, database)

    assert sorted(seen["station_registry"]) == sorted(fleet)
    assert sorted(seen["station_source_authorities"]) == sorted(fleet)
    assert sorted(seen["station_settlement_timezones"]) == sorted(fleet)


def test_a_fleet_station_without_a_registry_row_is_refused(tmp_path, monkeypatch):
    module = _load()
    fleet = list(coordinate_registry_manifest()["station_ids"])
    database = _database(tmp_path / "ssot.db", fleet[1:])

    with pytest.raises(module.ContractError, match=fleet[0]):
        _run(module, monkeypatch, tmp_path, database)
