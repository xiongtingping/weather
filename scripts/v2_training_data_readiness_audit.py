#!/usr/bin/env python3
"""Read-only readiness gate for v2 strict-as-of training data."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from source_observation_replay_eligibility_runtime import (  # noqa: E402
    classify_source_observation_replay_eligibility,
)
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_STRICT_CAPTURE_ROOT = ROOT / "data" / "research" / "forecast_source_skill" / "raw"
DEFAULT_STRICT_CAPTURE_REQUIRED_SOURCES = ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak")
P1_TABLES = (
    "canonical_observation_events",
    "canonical_observation_event_sources",
    "strong_observation_updates",
    "posterior_checkpoints",
    "realization_states",
    "posterior_commit_outbox",
)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0] or "") for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _days_between(start: Any, end: Any) -> int | None:
    try:
        return (date.fromisoformat(str(end)[:10]) - date.fromisoformat(str(start)[:10])).days + 1
    except (TypeError, ValueError):
        return None


def build_resolution_label_registry_readiness(
    *,
    registry_path: Path | None,
    min_history_days: int,
) -> dict[str, Any]:
    """Read final X→S→B label coverage only from the isolated immutable registry.

    ``event_daily_temps`` is intentionally excluded: it holds legacy scalar
    values, not raw-object-backed X/S lineage or a frozen resolution contract.
    """

    if registry_path is None:
        return {"state": "not_configured", "strict_x_s_b_bridge_proven": False}
    path = Path(registry_path).expanduser().resolve()
    if not path.exists():
        return {
            "state": "missing",
            "path": str(path),
            "strict_x_s_b_bridge_proven": False,
        }
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            tables = _tables(conn)
            required = {"v2_shadow_runtime_manifest", "resolution_final_labels"}
            if not required.issubset(tables):
                return {
                    "state": "invalid_registry_schema",
                    "path": str(path),
                    "missing_tables": sorted(required - tables),
                    "strict_x_s_b_bridge_proven": False,
                }
            manifest = conn.execute(
                "SELECT activation_state FROM v2_shadow_runtime_manifest WHERE singleton=1"
            ).fetchone()
            if manifest is None or str(manifest[0] or "") != "isolated_shadow_only":
                return {
                    "state": "invalid_registry_manifest",
                    "path": str(path),
                    "strict_x_s_b_bridge_proven": False,
                }
            row = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT event_date), MIN(event_date), MAX(event_date),
                       SUM(CASE WHEN label_finality='final' THEN 1 ELSE 0 END)
                FROM resolution_final_labels
                """
            ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        return {
            "state": "unreadable",
            "path": str(path),
            "error": type(exc).__name__,
            "strict_x_s_b_bridge_proven": False,
        }
    row_count = int(row[0] or 0)
    event_days = int(row[1] or 0)
    final_rows = int(row[4] or 0)
    strict = row_count > 0 and row_count == final_rows and event_days >= int(min_history_days)
    return {
        "state": "ready" if strict else "insufficient",
        "path": str(path),
        "row_count": row_count,
        "distinct_event_days": event_days,
        "event_date_min": str(row[2] or ""),
        "event_date_max": str(row[3] or ""),
        "coverage_days_span": _days_between(row[2], row[3]),
        "final_rows": final_rows,
        "minimum_history_days": int(min_history_days),
        "strict_x_s_b_bridge_proven": strict,
        "registry_isolated_shadow_only": True,
    }


def build_v2_training_data_readiness_audit(
    *,
    db_path: Path = DEFAULT_DB,
    min_history_days: int = 365,
    strict_capture_root: Path | None = None,
    strict_capture_required_sources: Sequence[str] = DEFAULT_STRICT_CAPTURE_REQUIRED_SOURCES,
    resolution_label_registry_path: Path | None = None,
) -> dict[str, Any]:
    """Assess data sufficiency without training, writing, or backfilling."""

    db = Path(db_path).expanduser().resolve()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        tables = _tables(conn)
        p1_missing = sorted(set(P1_TABLES) - tables)
        source_summary: dict[str, Any] = {}
        source_rows: list[dict[str, Any]] = []
        if "source_observations" in tables:
            source_columns = {
                str(row[1] or "")
                for row in conn.execute("PRAGMA table_info(source_observations)").fetchall()
            }
            parser_select = "parser_version" if "parser_version" in source_columns else "'' AS parser_version"
            source_records = [
                {
                    "source_id": str(row[0] or ""),
                    "source_class": str(row[1] or ""),
                    "event_date": str(row[2] or ""),
                    "valid_time": str(row[3] or ""),
                    "fetched_at": str(row[4] or ""),
                    "parser_version": str(row[5] or ""),
                }
                for row in conn.execute(
                    "SELECT source_id,source_class,event_date,valid_time,fetched_at,"
                    f"{parser_select} FROM source_observations"
                )
            ]
            strict_records = [
                row
                for row in source_records
                if classify_source_observation_replay_eligibility(row).get("strict_source_lineage_candidate") is True
            ]
            event_dates = [row["event_date"] for row in strict_records if row["event_date"]]
            source_summary = {
                "row_count": len(source_records),
                "strict_source_lineage_candidate_count": len(strict_records),
                "legacy_derived_excluded_count": len(source_records) - len(strict_records),
                "event_date_min": min(event_dates) if event_dates else "",
                "event_date_max": max(event_dates) if event_dates else "",
                "coverage_days": _days_between(min(event_dates), max(event_dates)) if event_dates else None,
                "missing_valid_time_count": sum(not row["valid_time"] for row in strict_records),
                "missing_fetched_at_count": sum(not row["fetched_at"] for row in strict_records),
            }
            grouped: dict[tuple[str, str], list[str]] = {}
            for row in strict_records:
                grouped.setdefault((row["source_id"], row["source_class"]), []).append(row["event_date"])
            source_rows = [
                {
                    "source_id": source_id,
                    "source_class": source_class,
                    "row_count": len(event_days),
                    "event_date_min": min((day for day in event_days if day), default=""),
                    "event_date_max": max((day for day in event_days if day), default=""),
                    "coverage_days": _days_between(
                        min((day for day in event_days if day), default=""),
                        max((day for day in event_days if day), default=""),
                    ),
                }
                for (source_id, source_class), event_days in sorted(grouped.items())
            ]
        resolution_summary: dict[str, Any] = {}
        if "event_daily_temps" in tables:
            row = conn.execute("SELECT COUNT(*), MIN(event_date), MAX(event_date) FROM event_daily_temps").fetchone()
            resolution_summary["event_daily_temps"] = {
                "row_count": int(row[0] or 0),
                "event_date_min": str(row[1] or ""),
                "event_date_max": str(row[2] or ""),
                "coverage_days": _days_between(row[1], row[2]),
                "strict_x_s_b_bridge_proven": False,
            }
        if "event_runs" in tables:
            row = conn.execute("SELECT COUNT(*), MIN(event_date), MAX(event_date) FROM event_runs").fetchone()
            resolution_summary["event_runs"] = {
                "row_count": int(row[0] or 0),
                "event_date_min": str(row[1] or ""),
                "event_date_max": str(row[2] or ""),
                "coverage_days": _days_between(row[1], row[2]),
            }
    strict_capture_readiness: dict[str, Any] = {}
    if strict_capture_root is not None:
        from scripts.strict_asof_forecast_capture_readiness import build_strict_asof_forecast_capture_readiness

        strict_capture_readiness = build_strict_asof_forecast_capture_readiness(
            Path(strict_capture_root),
            required_source_ids=[str(source) for source in strict_capture_required_sources if str(source or "").strip()],
            minimum_complete_event_days=max(1, int(min_history_days)),
        )
    resolution_label_registry = build_resolution_label_registry_readiness(
        registry_path=resolution_label_registry_path,
        min_history_days=min_history_days,
    )
    blockers: list[str] = []
    if p1_missing:
        blockers.append("p1_canonical_and_sequential_schema_not_deployed")
    if not source_summary:
        blockers.append("source_observations_table_missing")
    else:
        if (source_summary.get("coverage_days") or 0) < int(min_history_days):
            blockers.append("source_observation_history_below_minimum_seasonal_coverage")
        if source_summary.get("missing_valid_time_count"):
            blockers.append("source_observation_valid_time_incomplete")
        if source_summary.get("missing_fetched_at_count"):
            blockers.append("source_observation_first_seen_or_fetched_at_incomplete")
    forecast_sources = [row for row in source_rows if row["source_class"] == "forecast_weak_source"]
    if not forecast_sources:
        blockers.append("forecast_weak_source_history_missing")
    elif any((row.get("coverage_days") or 0) < int(min_history_days) for row in forecast_sources):
        blockers.append("forecast_source_family_history_below_minimum_seasonal_coverage")
    if not resolution_summary.get("event_daily_temps"):
        blockers.append("physical_high_label_history_missing")
    if resolution_label_registry.get("strict_x_s_b_bridge_proven") is not True:
        blockers.append("strict_x_s_b_resolution_label_bridge_not_yet_proven")
    if strict_capture_readiness and strict_capture_readiness.get("ok") is not True:
        blockers.append("strict_asof_tier_a_capture_coverage_not_ready")
    return {
        "schema": "v2_training_data_readiness_audit_v1",
        "ok": not blockers,
        "state": "ready_for_oos_training" if not blockers else "blocked",
        "db": str(db),
        "minimum_history_days": int(min_history_days),
        "p1_missing_tables": p1_missing,
        "source_observations": source_summary,
        "source_by_id": source_rows,
        "resolution_history": resolution_summary,
        "resolution_label_registry": resolution_label_registry,
        "strict_capture_readiness": strict_capture_readiness,
        "blockers": sorted(set(blockers)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "historical_backfill": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": (
            "build_strict_asof_training_dataset_and_oos_artifacts"
            if not blockers
            else "close_data_identity_history_and_resolution_coverage_before_training"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only v2 training-data readiness audit.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--min-history-days", type=int, default=365)
    parser.add_argument("--strict-capture-root", default=str(DEFAULT_STRICT_CAPTURE_ROOT))
    parser.add_argument("--strict-capture-required-source", action="append", default=list(DEFAULT_STRICT_CAPTURE_REQUIRED_SOURCES))
    parser.add_argument("--resolution-label-registry", default="")
    args = parser.parse_args()
    report = build_v2_training_data_readiness_audit(
        db_path=Path(args.db),
        min_history_days=max(1, int(args.min_history_days)),
        strict_capture_root=Path(args.strict_capture_root) if args.strict_capture_root else None,
        strict_capture_required_sources=args.strict_capture_required_source,
        resolution_label_registry_path=Path(args.resolution_label_registry) if args.resolution_label_registry else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
