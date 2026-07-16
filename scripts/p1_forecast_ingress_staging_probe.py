#!/usr/bin/env python3
"""Prove Tier-A forecast raw capture -> P1 ingress -> ForecastRevision in memory.

The probe intentionally recreates only the P1 source-observation surface in
SQLite memory.  It never opens the archive writable, does not write research
files and cannot create a posterior, fact/latest pointer, decision or order.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_revision_canonical_runtime import canonicalize_forecast_revisions  # noqa: E402
from scripts.append_strict_asof_forecast_p1_ingress import (  # noqa: E402
    DEFAULT_ROOT,
    collect_tier_a_forecast_p1_proposals,
)
from sequential_bayes_schema_contract import apply_forward_schema  # noqa: E402
from strict_asof_forecast_p1_ingress_writer import persist_tier_a_forecast_p1_proposal  # noqa: E402
from strict_asof_forecast_p1_reader import load_strict_forecast_p1_source_observations  # noqa: E402


SCHEMA_VERSION = "p1_forecast_ingress_staging_probe_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _initialize_source_observations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT PRIMARY KEY, source_id TEXT, source_class TEXT,
            station_id TEXT, event_date TEXT, valid_time TEXT, fetched_at TEXT,
            temperature_c REAL, raw_ref TEXT, raw_sha256 TEXT, parser_version TEXT,
            observation_json TEXT, created_at TEXT, source_observation_id TEXT UNIQUE,
            role TEXT, valid_time_local TEXT, parsed_at TEXT,
            response_completed_at TEXT, response_duration_ms REAL, dewpoint_c REAL,
            observed_today_max_c REAL, forecast_peak_c REAL, hard_eligible INTEGER,
            demotion_reason TEXT, quality_flags_json TEXT
        )
        """
    )


def build_p1_forecast_ingress_staging_probe(
    *,
    root: Path = DEFAULT_ROOT,
    event_date: str,
    station_id: str = "ZUUU",
    target_identity: str = "strict-asof-forecast-ingress-probe",
) -> dict[str, Any]:
    """Run the complete ingress/revision proof in SQLite memory only."""

    day = _text(event_date)[:10]
    scan = collect_tier_a_forecast_p1_proposals(root=Path(root), station_id=station_id)
    selected = [item for item in scan["eligible"] if _text(item.get("event_date")) == day]
    if not day or not selected:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "eligible_count": len(selected),
            "quarantined": scan["blocked"],
            "block_reasons": ["tier_a_forecast_proposals_missing_for_event_date"],
            "guards": {"writes_archive_db": False, "writes_research_files": False, "creates_posterior": False, "places_orders": False},
        }
    conn = sqlite3.connect(":memory:")
    try:
        _initialize_source_observations(conn)
        apply_forward_schema(conn)
        persisted = [persist_tier_a_forecast_p1_proposal(conn, item["proposal"]) for item in selected]
        loaded = load_strict_forecast_p1_source_observations(conn, event_date=day)
        times = [_parse_time(row.get("fetched_at")) for row in loaded.get("source_observations") or []]
        latest = max((value for value in times if value is not None), default=None)
        if loaded.get("ok") is not True or latest is None:
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "event_date": day,
                "persisted": persisted,
                "loaded": loaded,
                "block_reasons": ["p1_ingress_reader_not_available"],
                "guards": {"writes_archive_db": False, "writes_research_files": False, "creates_posterior": False, "places_orders": False},
            }
        decision_as_of = (latest + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        revisions = canonicalize_forecast_revisions(
            loaded["source_observations"],
            target_identity=target_identity,
            event_date=day,
            decision_as_of=decision_as_of,
        )
    except Exception as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "event_date": day,
            "error": type(exc).__name__,
            "error_detail": str(exc),
            "block_reasons": ["p1_forecast_ingress_memory_probe_failed"],
            "guards": {"writes_archive_db": False, "writes_research_files": False, "creates_posterior": False, "places_orders": False},
        }
    finally:
        conn.close()
    return {
        "schema": SCHEMA_VERSION,
        "ok": revisions.get("ok") is True,
        "state": "proved" if revisions.get("ok") is True else "blocked",
        "event_date": day,
        "decision_as_of": decision_as_of,
        "eligible_count": len(selected),
        "persisted": persisted,
        "loaded_source_observation_count": loaded["source_observation_count"],
        "forecast_revision_result": revisions,
        "quarantined": scan["blocked"],
        "guards": {
            "writes_archive_db": False,
            "writes_research_files": False,
            "writes_staging_db": False,
            "creates_posterior": False,
            "places_orders": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--station-id", default="ZUUU")
    parser.add_argument("--target-identity", default="strict-asof-forecast-ingress-probe")
    args = parser.parse_args(argv)
    result = build_p1_forecast_ingress_staging_probe(
        root=args.root,
        event_date=args.event_date,
        station_id=args.station_id,
        target_identity=args.target_identity,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
