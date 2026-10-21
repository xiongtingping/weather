"""KDEN's days before its first market (ruling 2026-10-21) have no registry
row, so the truth sweep finds them through the prelisting allowance."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from bayes_basket_target import settlement_basis

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("settlement_truth_sweep", ROOT / "scripts" / "settlement_truth_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


def test_the_allowance_days_without_truth_are_owed(tmp_path, monkeypatch) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({
        "prelisting_truth_backfill": {"stations": [
            {"station_id": "KDEN", "from": "2026-09-21", "through": "2026-09-24"}]},
        "settlement_source_contract": {"cities": {"denver": {"station_id": "KDEN"}}},
    }))
    monkeypatch.setattr(settlement_basis, "CONFIG_PATH", path)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT, local_date TEXT, source_lane TEXT, qc_status TEXT)"
    )
    conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES('KDEN','2026-09-22','AWC','ACCEPTED')")
    owed = sweep._prelisting_backlog(conn, "2026-09-22")
    assert owed == [("KDEN", "2026-09-23"), ("KDEN", "2026-09-24")]
