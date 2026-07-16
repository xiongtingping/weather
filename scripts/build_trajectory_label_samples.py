#!/usr/bin/env python3
"""Build read-only ZUUU trajectory-label samples from archived raw lineage."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amsc_raw_history_archive import build_amsc_daily_coverage_manifest, load_amsc_daily_coverage_snapshot
from trajectory_label_sample_runtime import build_trajectory_label_sample, build_trajectory_label_sample_manifest
from resolution_final_source_runtime import PHYSICAL_SOURCE_ID
from resolution_final_source_shadow_writer import SOURCE_TABLE
from strong_observation_training_feature_runtime import TRAINING_SCOPE_PHYSICAL_X, TRAINING_SCOPE_SETTLEMENT


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_CUTOFFS = ("09:00", "11:00", "13:00", "15:00", "17:00")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-date", action="append", required=True, help="station-local YYYY-MM-DD; may be repeated")
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument("--archive-dir", default=str(ROOT / "data" / "amsc_raw_history"))
    parser.add_argument(
        "--physical-final-source-db",
        default=str(ROOT / "data" / "shadow" / "resolution_final_labels.db"),
        help="isolated final-X registry; read only",
    )
    parser.add_argument(
        "--training-scope",
        choices=(TRAINING_SCOPE_SETTLEMENT, TRAINING_SCOPE_PHYSICAL_X),
        default=TRAINING_SCOPE_SETTLEMENT,
        help="settlement requires X/S/B; physical_x requires only final AMSC X",
    )
    parser.add_argument(
        "--cutoff-local",
        default=",".join(DEFAULT_CUTOFFS),
        help="comma-separated local cutoffs, default: %(default)s",
    )
    return parser.parse_args()


def _cutoff_utc(event_date: str, local_hhmm: str) -> str:
    parsed = datetime.fromisoformat(f"{event_date}T{local_hhmm}:00").replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _source_rows(conn: sqlite3.Connection, *, event_date: str, station_id: str, allowed_hashes: set[str]) -> list[dict[str, Any]]:
    if not allowed_hashes:
        return []
    placeholders = ",".join("?" for _ in allowed_hashes)
    rows = conn.execute(
        f"""
        SELECT source_event_id, source_observation_id, source_id, source_class, station_id,
               event_date, valid_time, fetched_at, parsed_at, response_completed_at,
               temperature_c, dewpoint_c, raw_ref, raw_sha256, parser_version,
               observation_json
          FROM source_observations
         WHERE event_date = ?
           AND UPPER(station_id) = ?
           AND raw_sha256 IN ({placeholders})
         ORDER BY COALESCE(fetched_at, parsed_at, response_completed_at, valid_time), source_event_id
        """,
        [event_date, station_id.upper(), *sorted(allowed_hashes)],
    ).fetchall()
    output: list[dict[str, Any]] = []
    for sql_row in rows:
        row = dict(sql_row)
        try:
            embedded = json.loads(row.pop("observation_json") or "{}")
        except json.JSONDecodeError:
            embedded = {}
        row["payload"] = dict(embedded.get("payload") or {}) if isinstance(embedded, dict) else {}
        output.append(row)
    return output


def _formal_label(conn: sqlite3.Connection, *, event_date: str, station_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, settlement_actual, resolution_source, created_at
          FROM event_runs
         WHERE event_date = ?
           AND UPPER(station_id) = ?
           AND market_closed = 1
           AND settlement_actual IS NOT NULL
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        (event_date, station_id.upper()),
    ).fetchone()
    if row is None:
        return None
    return {
        "label_kind": "formal_settlement",
        "settlement_actual": row["settlement_actual"],
        "resolution_source": row["resolution_source"],
        "event_run_id": row["id"],
        "label_version": f"event_runs_market_closed:{row['created_at']}",
    }


def _resolution_source_wu_label(conn: sqlite3.Connection, *, event_date: str, station_id: str) -> dict[str, Any] | None:
    """Use WU only as a post-event label when it exactly matches market resolution."""

    try:
        event_day = datetime.fromisoformat(event_date).date()
    except ValueError:
        return None
    if event_day >= datetime.now(timezone.utc).astimezone(LOCAL_TZ).date():
        return None
    event_row = conn.execute(
        """
        SELECT id, resolution_source, created_at
          FROM event_runs
         WHERE event_date = ?
           AND UPPER(station_id) = ?
           AND resolution_source IS NOT NULL
           AND resolution_source <> ''
         ORDER BY market_closed DESC, created_at DESC, id DESC
         LIMIT 1
        """,
        (event_date, station_id.upper()),
    ).fetchone()
    if event_row is None:
        return None
    resolution_source = str(event_row["resolution_source"] or "").strip()
    if "wunderground" not in resolution_source.lower() and "weatherunderground" not in resolution_source.lower():
        return None
    rows = conn.execute(
        """
        SELECT id, value, unit, observed_at, created_at
          FROM station_observations
         WHERE event_date = ?
           AND UPPER(station_id) = ?
           AND resolution_source = ?
           AND LOWER(source_kind) = 'wunderground_history'
           AND trust_tier = 'settlement_source'
           AND value IS NOT NULL
         ORDER BY observed_at, created_at, id
        """,
        (event_date, station_id.upper(), resolution_source),
    ).fetchall()
    values_c: list[float] = []
    source_ids: list[str] = []
    observed_at: list[str] = []
    created_at: list[str] = []
    for row in rows:
        try:
            raw_value = float(row["value"])
        except (TypeError, ValueError):
            continue
        unit = str(row["unit"] or "").strip().upper()
        if unit in {"C", "°C", "CELSIUS"}:
            value_c = raw_value
        elif unit in {"F", "°F", "FAHRENHEIT"}:
            value_c = (raw_value - 32.0) * 5.0 / 9.0
        else:
            continue
        values_c.append(value_c)
        source_ids.append(f"stationobs_{row['id']}")
        observed_at.append(str(row["observed_at"] or ""))
        created_at.append(str(row["created_at"] or ""))
    if not values_c:
        return None
    return {
        "label_kind": "resolution_source_wu",
        "final_daily_max_c": max(values_c),
        "resolution_source": resolution_source,
        "event_run_id": event_row["id"],
        "label_version": f"station_observations.wunderground_history:{max(created_at) if created_at else event_row['created_at']}",
        "label_source_observation_ids": source_ids,
        "label_source_row_count": len(values_c),
        "label_observed_first_at": min((value for value in observed_at if value), default=""),
        "label_observed_last_at": max((value for value in observed_at if value), default=""),
        "label_finality": "source_matched_wu_history_post_event_max",
    }


def _physical_final_x_label(
    *,
    final_source_db_path: str | Path,
    event_date: str,
    station_id: str,
) -> dict[str, Any] | None:
    """Load independently captured final AMSC X; never wait for market finality.

    The final-source registry is intentionally outside ``archive.db``.  This
    reader is append-only and read-only: it turns a closed station-day X into
    a physical-training label, never into a market settlement label.
    """

    path = Path(final_source_db_path).expanduser().resolve()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                f"""
                SELECT source_observation_id, raw_ref, raw_sha256, observation_json, created_at
                  FROM {SOURCE_TABLE}
                 WHERE source_id = ? AND event_date = ? AND UPPER(station_id) = ?
                 ORDER BY created_at DESC, source_observation_id DESC
                 LIMIT 1
                """,
                (PHYSICAL_SOURCE_ID, event_date, station_id.upper()),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        observation = json.loads(row[3] or "{}")
    except json.JSONDecodeError:
        return None
    payload = observation.get("payload") if isinstance(observation, dict) else {}
    if not isinstance(payload, dict) or payload.get("label_finality") != "final":
        return None
    try:
        high = float(payload.get("final_physical_high_c"))
    except (TypeError, ValueError):
        return None
    available_at = str(observation.get("fetched_at") or row[4] or "")
    if not available_at:
        return None
    return {
        "label_kind": "amsc_physical_final_source_shadow",
        "final_physical_high_c": high,
        "strict_asof_training_eligible": payload.get("strict_asof_training_eligible") is True,
        "label_available_at": available_at,
        "label_finality": "final",
        "label_provenance": {
            "source_observation_id": str(row[0]),
            "raw_ref": str(row[1]),
            "raw_sha256": str(row[2]),
            "source_id": PHYSICAL_SOURCE_ID,
            "capture_scope": "physical_station_day_not_market_target",
        },
    }


def _fact_snapshot_ids(conn: sqlite3.Connection, *, event_date: str, cutoff_as_of: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT fact_snapshot_id
          FROM event_fact_history
         WHERE event_date = ?
           AND fact_as_of <= ?
           AND fact_snapshot_id IS NOT NULL
           AND fact_snapshot_id <> ''
         ORDER BY fact_as_of, id
        """,
        (event_date, cutoff_as_of),
    ).fetchall()
    return [str(row["fact_snapshot_id"]) for row in rows]


def _production_baseline_at_cutoff(
    conn: sqlite3.Connection,
    *,
    event_date: str,
    cutoff_as_of: str,
) -> dict[str, Any]:
    """Read the persisted production strong-likelihood map, never recompute it."""

    row = conn.execute(
        """
        SELECT fact_snapshot_id, fact_as_of, fact_json
          FROM event_fact_history
         WHERE event_date = ?
           AND fact_as_of <= ?
         ORDER BY fact_as_of DESC, id DESC
         LIMIT 1
        """,
        (event_date, cutoff_as_of),
    ).fetchone()
    if row is None:
        return {"available": False, "reason": "fact_snapshot_missing_at_or_before_cutoff"}
    try:
        fact = json.loads(row["fact_json"] or "{}")
    except json.JSONDecodeError:
        return {"available": False, "reason": "fact_snapshot_json_invalid", "fact_snapshot_id": row["fact_snapshot_id"]}
    likelihood = fact.get("metar_strong_likelihood") if isinstance(fact, dict) else None
    if not isinstance(likelihood, dict) or not likelihood:
        return {"available": False, "reason": "persisted_metar_strong_likelihood_missing", "fact_snapshot_id": row["fact_snapshot_id"]}
    return {
        "available": True,
        "fact_snapshot_id": str(row["fact_snapshot_id"] or ""),
        "fact_as_of": str(row["fact_as_of"] or ""),
        "likelihood_map": dict(likelihood),
        "source": "event_fact_history.persisted_metar_strong_likelihood",
    }


def build_samples_from_db(
    *,
    db_path: str | Path,
    archive_dir: str | Path,
    event_dates: list[str],
    station_id: str,
    cutoff_locals: list[str],
    training_scope: str = TRAINING_SCOPE_SETTLEMENT,
    physical_final_source_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read archived source lineage and persisted labels/baselines into samples."""

    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    samples: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    try:
        for event_date in sorted(set(event_dates)):
            coverage = load_amsc_daily_coverage_snapshot(
                archive_dir,
                event_date=event_date,
                station_id=station_id,
            )
            if coverage is None:
                coverage = build_amsc_daily_coverage_manifest(
                    archive_dir,
                    event_date=event_date,
                    station_id=station_id,
                )
                coverage["coverage_snapshot_persisted"] = False
            else:
                coverage["coverage_snapshot_persisted"] = True
            hashes = {str(value) for value in coverage.get("raw_object_sha256_list") or [] if str(value)}
            source_rows = _source_rows(conn, event_date=event_date, station_id=station_id, allowed_hashes=hashes)
            if not hashes:
                excluded.append({"event_date": event_date, "reason": "raw_archive_metar_missing"})
                continue
            if not source_rows:
                excluded.append({"event_date": event_date, "reason": "archived_raw_not_materialized_as_source_observation"})
                continue
            if training_scope == TRAINING_SCOPE_PHYSICAL_X:
                label = _physical_final_x_label(
                    final_source_db_path=physical_final_source_db_path or ROOT / "data" / "shadow" / "resolution_final_labels.db",
                    event_date=event_date,
                    station_id=station_id,
                )
            else:
                label = _formal_label(conn, event_date=event_date, station_id=station_id)
                if label is None:
                    label = _resolution_source_wu_label(conn, event_date=event_date, station_id=station_id)
            for cutoff_local in cutoff_locals:
                cutoff_as_of = _cutoff_utc(event_date, cutoff_local)
                sample = build_trajectory_label_sample(
                    station_id=station_id,
                    event_date=event_date,
                    cutoff_as_of=cutoff_as_of,
                    source_observations=source_rows,
                    label=label,
                    coverage_manifest=coverage,
                    fact_snapshot_ids=_fact_snapshot_ids(conn, event_date=event_date, cutoff_as_of=cutoff_as_of),
                )
                sample["production_baseline"] = _production_baseline_at_cutoff(
                    conn,
                    event_date=event_date,
                    cutoff_as_of=cutoff_as_of,
                )
                samples.append(sample)
    finally:
        conn.close()
    manifest = build_trajectory_label_sample_manifest(
        requested_event_dates=event_dates,
        samples=samples,
        excluded=excluded,
    )
    return {
        "training_scope": training_scope,
        "manifest": manifest,
        "samples": samples,
        "boundary": {
            "physical_x_scope_does_not_require_market_finality_or_bridge": training_scope == TRAINING_SCOPE_PHYSICAL_X,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


def main() -> int:
    args = parse_args()
    try:
        cutoffs = [item.strip() for item in str(args.cutoff_local).split(",") if item.strip()]
        for value in cutoffs:
            datetime.strptime(value, "%H:%M")
    except ValueError:
        print("invalid --cutoff-local; use HH:MM,HH:MM", file=sys.stderr)
        return 2
    result = build_samples_from_db(
        db_path=args.db,
        archive_dir=args.archive_dir,
        event_dates=list(args.event_date),
        station_id=args.station_id,
        cutoff_locals=cutoffs,
        training_scope=args.training_scope,
        physical_final_source_db_path=args.physical_final_source_db,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
