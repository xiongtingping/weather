#!/usr/bin/env python3
"""Exercise build_cases_ssot.py end to end on a synthetic two-table fixture.

The production SSOT was not reachable where the study was written, so this
builds the smallest target/archive pair carrying every column the builder
reads (one station, one settled day, one P0 envelope with a radiation
receipt and a curve, one forward TAF with its raw-archive row, a day of
METARs) and checks the emitted rows.  It proves the queries and the
plumbing, not the numbers.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build_fixture(root: Path) -> tuple[Path, Path, Path]:
    target = root / "ssot.db"
    archive = root / "archive.db"
    taf_dir = root / "taf_raw_archive"
    taf_dir.mkdir()
    conn = sqlite3.connect(target)
    conn.executescript(
        """
        CREATE TABLE basket_probability_envelope (checkpoint_id TEXT, station_id TEXT,
          event_date TEXT, stage TEXT, state TEXT, decision_at_utc TEXT,
          probabilities_json TEXT, bucket_ordinals_json TEXT, lineage_json TEXT);
        CREATE TABLE calibration_station_market_timezone_binding (station_id TEXT, settlement_timezone TEXT);
        CREATE TABLE calibration_station_daily_high_truth (station_id TEXT, local_date TEXT,
          official_tmax_c REAL, qc_status TEXT, revision_no INTEGER);
        CREATE TABLE calibration_station_forecast_physics_version (station_id TEXT, local_date TEXT,
          captured_at_utc TEXT, cloudcover_mean REAL, precipitation_sum REAL,
          windspeed_10m_max REAL, capture_mode TEXT);
        CREATE TABLE calibration_forward_taf_capture (station_id TEXT, event_date TEXT,
          settlement_timezone TEXT, issued_at_utc TEXT, provider_available_at_utc TEXT,
          received_at_utc TEXT, tx_c REAL, source_object_sha256 TEXT, revision_id TEXT);
        """
    )
    labels = [f"{t}°C" for t in range(20, 31)]
    ordinals = {label: i for i, label in enumerate(labels)}
    intervals = {label: [t - 0.5, t + 0.5] for label, t in zip(labels, range(20, 31))}
    probs = {label: 0.0 for label in labels}
    probs.update({"24°C": 0.2, "25°C": 0.5, "26°C": 0.3})
    day_start = datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)  # 2026-09-11 00:00 +08
    curve = {"points": [
        {"valid_time_utc": (day_start + timedelta(hours=h)).isoformat(),
         "temperature_c": 20.0 + 5.0 * max(0.0, 1.0 - abs(h - 14) / 8.0)}
        for h in range(-1, 26)
    ]}
    lineage = {
        "model_center_c": "25.1",
        "bucket_intervals_c_json": json.dumps(intervals),
        "synoptic_cooling_json": json.dumps({
            "state": "RESOLVED", "standardised": -0.8, "shift_c": -0.06,
            "clearness_index": 0.45, "history_clearness_sd": 0.12,
        }),
        "conditional_future_temperature_curve_json": json.dumps(curve),
    }
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?)",
        ("p0-a", "ZUUU", "2026-09-11", "P0", "COMMITTED", "2026-09-10T12:00:00Z",
         json.dumps(probs), json.dumps(ordinals), json.dumps(lineage)),
    )
    conn.execute("INSERT INTO calibration_station_market_timezone_binding VALUES ('ZUUU','Asia/Shanghai')")
    conn.execute("INSERT INTO calibration_station_daily_high_truth VALUES ('ZUUU','2026-09-11',26.0,'ACCEPTED',1)")
    conn.execute(
        "INSERT INTO calibration_station_forecast_physics_version VALUES "
        "('ZUUU','2026-09-11','2026-09-10T06:00:00Z',55.0,0.0,12.0,'forward')"
    )
    conn.execute(
        "INSERT INTO calibration_forward_taf_capture VALUES "
        "('ZUUU','2026-09-11','Asia/Shanghai','2026-09-10T15:00:00Z','2026-09-10T15:05:00Z',"
        "'2026-09-10T15:06:00Z',27.0,'sha-taf-1','rev-1')"
    )
    conn.commit()
    conn.close()
    start = int((day_start - timedelta(hours=2)).timestamp())
    awc_row = {
        "icaoId": "ZUUU", "issueTime": "2026-09-10T15:00:00Z",
        "rawTAF": "TAF ZUUU 101500Z 1018/1118 22003MPS 6000 SCT040 TX27/1107Z TN20/1022Z",
        "fcsts": [{
            "timeFrom": start, "timeTo": start + 36 * 3600, "fcstChange": None,
            "probability": None, "wspd": 6, "wxString": None,
            "clouds": [{"cover": "SCT"}],
        }],
    }
    (taf_dir / "2026-09-10.jsonl").write_text(
        json.dumps({"raw_taf_sha256": "sha-taf-1", "awc_row": awc_row}) + "\n"
    )
    conn = sqlite3.connect(archive)
    conn.execute(
        "CREATE TABLE reported_trajectory_observations (observation_id TEXT, station_id TEXT,"
        " valid_at TEXT, reported_temperature_c REAL, raw_text TEXT, source_lane TEXT,"
        " correction_suspected INTEGER)"
    )
    for hour in range(0, 24):
        when = day_start + timedelta(hours=hour)
        temp = round(20.0 + 6.0 * max(0.0, 1.0 - abs(hour - 14) / 8.0))
        cover = "BKN030" if hour < 11 else "FEW040"
        raw = f"METAR ZUUU {when:%d%H}00Z 02003MPS 8000 {cover} {temp:02d}/18 Q1010 NOSIG"
        conn.execute(
            "INSERT INTO reported_trajectory_observations VALUES (?,?,?,?,?,?,0)",
            (f"obs-{hour}", "ZUUU", when.strftime("%Y-%m-%dT%H:%M:%S.000Z"), float(temp), raw, "awc"),
        )
    conn.commit()
    conn.close()
    return target, archive, taf_dir


def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        target, archive, taf_dir = build_fixture(root)
        out = root / "out"
        subprocess.run(
            [sys.executable, str(HERE / "build_cases_ssot.py"),
             "--target-db", str(target), "--archive-db", str(archive),
             "--taf-archive-dir", str(taf_dir), "--from", "2026-09-01",
             "--through", "2026-09-20", "--out", str(out)],
            check=True,
        )
        days = [json.loads(line) for line in (out / "derived" / "days.jsonl").open()]
        p1 = [json.loads(line) for line in (out / "derived" / "p1.jsonl").open()]
        assert len(days) == 1, days
        day = days[0]
        assert day["unit"] == "C" and day["truth_rep"] == 26.0, day
        assert abs(day["p0_mu_c"] - 25.1) < 1e-9 and day["p0_radiation_c"] == -0.06, day
        assert day["kt_z"] == -0.8 and abs(day["model_cloud"] - 0.55) < 1e-9, day
        taf = day["taf_morning"]
        assert taf is not None and taf["tx_c"] == 27.0 and abs(taf["cloud"] - 0.375) < 1e-9, taf
        assert len(p1) == 5 and {row["hour"] for row in p1} == {8, 10, 12, 14, 16}, p1
        noon = next(row for row in p1 if row["hour"] == 12)
        assert noon["state_cloud"] == 0.25 and noon["obs_cloud_el"] is not None, noon
        assert noon["taf_rem"] is not None and abs(noon["taf_rem"][0] - 0.375) < 1e-9, noon
        print("smoke ok:", json.dumps({"day": day["date"], "p1_rows": len(p1)}))


if __name__ == "__main__":
    main()
