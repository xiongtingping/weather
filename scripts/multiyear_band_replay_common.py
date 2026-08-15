#!/usr/bin/env python3
"""Shared structure for the multi-year band-reliability replay evidence lane.

Research-only.  This lane exists to widen the sample behind the claimed-
probability band factors (``BAND_RELIABILITY_FACTORS`` in
``bayes_basket_target/cross_consensus.py``) and to test whether month or
season stratification earns its own cells.  Everything here writes to a
standalone evidence database; nothing touches archive.db, the production
SSOT, facts, signals, or launchd.  Replay conclusions enter serving only
through the transfer gate: the replay geometry (two families, synthesized
bucket ladders) is not the serving geometry, so factors measured here must
re-prove themselves on served distributions before they change the table.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]

REPLAY_SCHEMA_VERSION = "multiyear_band_replay_evidence_v1"
DEFAULT_REPLAY_DIR = ROOT / "data" / "research" / "multiyear_band_replay"
DEFAULT_REPLAY_DB = DEFAULT_REPLAY_DIR / "replay_evidence.sqlite"

# The two families with deep public archives.  ECCC and ICON keep no
# archive, so the replay is a two-family mixture by construction.
REPLAY_FAMILIES = ("gefs", "ecmwf")
FAMILY_SYSTEM_IDS = {
    "gefs": "NOAA_GEFS_05",
    "ecmwf": "ECMWF_IFS_ENS_OPEN_PF50",
}

# Measured archive depth (probed 2026-08-15):
#   GEFS v12 layout (atmos/pgrb2ap5, 0.5 deg, 31 members): 2020-09-23 ->
#   present.  The pre-v12 layout (1.0 deg, 21 members) reaches back to
#   2018-07-28 but is a different extraction and is not captured here.
#   ECMWF open data 0p25 enfo carries mx2t3 only from IFS 49r1 on
#   2024-11-13; earlier objects (back to 2024-02-29 at 0p25, 2023-02 at
#   0p4-beta) hold instantaneous 2t only and are a different semantics,
#   so the ECMWF lane starts where the production field starts.
FAMILY_ARCHIVE_START = {
    "gefs": "2020-09-25",
    "ecmwf": "2024-11-14",
}

DDL = """
CREATE TABLE IF NOT EXISTS replay_truth_source (
  station_id TEXT PRIMARY KEY,
  ghcnh_station_id TEXT NOT NULL,
  mapping_method TEXT NOT NULL CHECK(
    mapping_method IN ('ICAO_EXACT','COORDINATE_NEAREST')
  ),
  distance_km REAL NOT NULL,
  settlement_timezone TEXT NOT NULL,
  acceptance_state TEXT NOT NULL CHECK(
    acceptance_state IN ('PROVISIONAL','ACCEPTED','REJECTED','UNRESOLVED')
  ),
  acceptance_receipt_json TEXT NOT NULL DEFAULT '{}',
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_daily_truth (
  station_id TEXT NOT NULL,
  local_date TEXT NOT NULL,
  tmax_c REAL NOT NULL,
  tmax_int INTEGER NOT NULL,
  observation_count INTEGER NOT NULL,
  distinct_hour_count INTEGER NOT NULL,
  peak_window_max_gap_minutes INTEGER,
  coverage_ok INTEGER NOT NULL CHECK(coverage_ok IN (0,1)),
  ghcnh_station_id TEXT NOT NULL,
  source_year_sha256 TEXT NOT NULL,
  ingested_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id, local_date)
);
CREATE INDEX IF NOT EXISTS idx_replay_daily_truth_date
  ON replay_daily_truth(local_date);

CREATE TABLE IF NOT EXISTS replay_member_run (
  ensemble_system_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  target_local_date TEXT NOT NULL,
  archive_lead_bucket TEXT NOT NULL,
  run_cycle TEXT NOT NULL,
  native_run_init_utc TEXT NOT NULL,
  member_id TEXT NOT NULL,
  member_role TEXT NOT NULL CHECK(member_role IN ('CONTROL','PERTURBED')),
  raw_tmax_c REAL NOT NULL,
  extraction_version TEXT NOT NULL,
  transport_provider_id TEXT NOT NULL,
  member_set_sha256 TEXT NOT NULL,
  archive_query_at_utc TEXT NOT NULL,
  PRIMARY KEY(
    ensemble_system_id, station_id, target_local_date,
    archive_lead_bucket, run_cycle, member_id, extraction_version
  )
);
CREATE INDEX IF NOT EXISTS idx_replay_member_run_station_date
  ON replay_member_run(station_id, target_local_date);

CREATE TABLE IF NOT EXISTS replay_capture_ledger (
  family TEXT NOT NULL,
  event_date TEXT NOT NULL,
  lead_days INTEGER NOT NULL,
  run_cycle_hour INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('DONE','FAILED')),
  station_count INTEGER NOT NULL DEFAULT 0,
  member_row_count INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  completed_at_utc TEXT NOT NULL,
  PRIMARY KEY(family, event_date, lead_days, run_cycle_hour)
);

CREATE TABLE IF NOT EXISTS replay_bucket_geometry (
  station_id TEXT PRIMARY KEY,
  geometry_class TEXT NOT NULL CHECK(
    geometry_class IN ('F_TWO_DEGREE','C_ONE_DEGREE')
  ),
  interior_bucket_count INTEGER NOT NULL,
  reference_event_date TEXT NOT NULL,
  reference_intervals_json TEXT NOT NULL,
  captured_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_oos_daily (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  lead_days INTEGER NOT NULL,
  run_cycle_hour INTEGER NOT NULL,
  family_set TEXT NOT NULL,
  train_days INTEGER NOT NULL,
  bucket_probabilities_json TEXT NOT NULL,
  integer_probabilities_json TEXT NOT NULL,
  truth_bucket TEXT NOT NULL,
  truth_tmax_int INTEGER NOT NULL,
  nll REAL NOT NULL,
  crps REAL NOT NULL,
  parameters_json TEXT NOT NULL,
  build_version TEXT NOT NULL,
  built_at_utc TEXT NOT NULL,
  PRIMARY KEY(station_id, event_date, lead_days, run_cycle_hour, build_version)
);
CREATE INDEX IF NOT EXISTS idx_replay_oos_daily_date
  ON replay_oos_daily(event_date);
"""


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def open_replay_db(path: Path | str = DEFAULT_REPLAY_DB) -> sqlite3.Connection:
    """Open (creating if needed) the standalone replay evidence database."""

    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def open_production_db_readonly(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


F_STEP_C = 10.0 / 9.0  # two Fahrenheit degrees
C_TO_F = 9.0 / 5.0


def classify_geometry(intervals: Mapping[str, Sequence[float | None]]) -> str:
    """Read the geometry class off a station's live market ladder."""

    widths = [
        float(upper) - float(lower)
        for lower, upper in intervals.values()
        if lower is not None and upper is not None
    ]
    if not widths:
        raise ValueError("bucket_geometry_no_interior_buckets")
    width = sorted(widths)[len(widths) // 2]
    if abs(width - F_STEP_C) < 0.05:
        return "F_TWO_DEGREE"
    if abs(width - 1.0) < 0.05:
        return "C_ONE_DEGREE"
    raise ValueError(f"bucket_geometry_unrecognised_width:{width}")


def synthesize_bucket_intervals(
    *,
    geometry_class: str,
    center_c: float,
    interior_bucket_count: int,
) -> dict[str, tuple[float | None, float | None]]:
    """A market-shaped ladder centered where a lister would center it.

    Markets list buckets around the forecast level, so the replay centers
    each day's ladder on the uncorrected two-family ensemble mean -- the
    same information a lister has, and no more.  US-geometry stations get
    two-Fahrenheit buckets on odd-degree half boundaries exactly like the
    live ladders; everywhere else gets one-Celsius buckets on half-degree
    boundaries.
    """

    if interior_bucket_count < 3:
        raise ValueError("bucket_geometry_interior_count_invalid")
    if geometry_class == "F_TWO_DEGREE":
        center_f = center_c * C_TO_F + 32.0
        # Live boundaries sit at odd Fahrenheit degrees + 0.5 (79.5, 81.5,
        # ...).  Snap the center into that lattice.
        low_odd = int(round((center_f - 0.5) / 2.0)) * 2 - 1
        half = interior_bucket_count // 2
        edges_f = [
            low_odd + 0.5 + 2.0 * (index - half)
            for index in range(interior_bucket_count + 1)
        ]
        edges = [(value - 32.0) / C_TO_F for value in edges_f]
        labels = [
            f"{int(edges_f[i] + 0.5)}-{int(edges_f[i + 1] - 0.5)}F"
            for i in range(interior_bucket_count)
        ]
        bottom = f"{int(edges_f[0] - 0.5)}F or below"
        top = f"{int(edges_f[-1] + 0.5)}F or higher"
    else:
        center_int = int(round(center_c))
        half = interior_bucket_count // 2
        edges = [
            center_int - half + index - 0.5
            for index in range(interior_bucket_count + 1)
        ]
        labels = [
            f"{int(edges[i] + 0.5)}C"
            for i in range(interior_bucket_count)
        ]
        bottom = f"{int(edges[0] - 0.5)}C or below"
        top = f"{int(edges[-1] + 0.5)}C or higher"
    intervals: dict[str, tuple[float | None, float | None]] = {
        bottom: (None, edges[0])
    }
    for index in range(interior_bucket_count):
        intervals[labels[index]] = (edges[index], edges[index + 1])
    intervals[top] = (edges[-1], None)
    return intervals


def project_integer_probability_to_intervals(
    integer_probability: Mapping[str, float],
    intervals: Mapping[str, tuple[float | None, float | None]],
) -> dict[str, float]:
    """Sum integer-degree mass into interval buckets, exactly as P0 does.

    An integer bucket ``t`` carries the mass of [t-0.5, t+0.5); it belongs
    to the interval that contains its center ``t``.
    """

    result = {bucket: 0.0 for bucket in intervals}
    for key, mass in integer_probability.items():
        center = float(int(key))
        for bucket, (lower, upper) in intervals.items():
            if (lower is None or center > lower) and (
                upper is None or center <= upper
            ):
                result[bucket] += float(mass)
                break
    total = sum(result.values())
    if total <= 0:
        raise ValueError("bucket_projection_empty")
    return {bucket: value / total for bucket, value in result.items()}


def truth_bucket_for_intervals(
    truth_int: int,
    intervals: Mapping[str, tuple[float | None, float | None]],
) -> str:
    center = float(truth_int)
    for bucket, (lower, upper) in intervals.items():
        if (lower is None or center > lower) and (
            upper is None or center <= upper
        ):
            return bucket
    raise ValueError("truth_bucket_not_found")


def load_station_timezones(
    production: sqlite3.Connection,
) -> dict[str, str]:
    """Settlement timezone per station, via the binding table when present.

    The registry's raw timezone column is wrong for 33 of 45 stations; the
    binding table is the corrected authority.
    """

    binding_exists = production.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND "
        "name='calibration_station_market_timezone_binding'"
    ).fetchone()
    if binding_exists is not None:
        rows = production.execute(
            "SELECT station_id, settlement_timezone "
            "FROM calibration_station_market_timezone_binding"
        ).fetchall()
        if rows:
            return {str(r[0]): str(r[1]) for r in rows}
    result: dict[str, str] = {}
    for station, tz_name in production.execute(
        "SELECT DISTINCT station_id, settlement_timezone "
        "FROM basket_target_market_registry "
        "WHERE settlement_timezone<>''"
    ):
        result.setdefault(str(station), str(tz_name))
    return result


def write_json_report(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
