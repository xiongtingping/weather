#!/usr/bin/env python3
"""Build a shadow v6 weak-source bias candidate from resolved market buckets.

Only forecast snapshots captured before the local target day are eligible.
The settlement result is represented as a rounding interval, never as an
exact physical-X observation.  This script cannot update the active artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_forecast_weak_likelihood_oos_runtime import evaluate_joint_weak_walk_forward  # noqa: E402
from joint_forecast_weak_likelihood_training_runtime import (  # noqa: E402
    build_joint_weak_training_dataset,
    train_joint_weak_source_set_artifact,
)


SCHEMA_VERSION = "joint_weak_settlement_interval_candidate_v1"
MODEL_SOURCE_IDS = {"ecmwf_ifs025": "ecmwf", "icon_seamless": "icon", "gfs_seamless": "gfs"}
SOURCE_CATALOG = {
    "ecmwf": {"provider_family": "official_nwp"},
    "icon": {"provider_family": "official_nwp"},
    "gfs": {"provider_family": "official_nwp"},
}
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _round_whole(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def settlement_interval(value_c: float, rounding_rule: str) -> dict[str, Any]:
    if str(rounding_rule or "whole_degrees") != "whole_degrees":
        raise ValueError("unsupported_settlement_rounding_rule")
    bucket = _round_whole(value_c)
    return {"bucket_c": float(bucket), "lower_c": bucket - 0.5, "upper_c": bucket + 0.5}


def _d1_cutoff(event_date: str) -> datetime:
    local_start = datetime.combine(date.fromisoformat(event_date), time.min, tzinfo=LOCAL_ZONE)
    return local_start.astimezone(timezone.utc)


def build_events(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    settlements = conn.execute(
        """
        SELECT event_date, MAX(settlement_actual) AS settlement_actual,
               COALESCE(MAX(rounding_rule), 'whole_degrees') AS rounding_rule,
               MAX(resolution_source) AS resolution_source
        FROM event_runs
        WHERE settlement_actual IS NOT NULL
          AND LOWER(COALESCE(city,'')) IN ('chengdu','zuuu')
        GROUP BY event_date
        ORDER BY event_date
        """
    ).fetchall()
    events: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for settlement in settlements:
        day = str(settlement["event_date"] or "")[:10]
        try:
            cutoff = _d1_cutoff(day)
            interval = settlement_interval(float(settlement["settlement_actual"]), str(settlement["rounding_rule"] or ""))
        except (TypeError, ValueError):
            excluded["invalid_settlement_identity_or_rounding"] = excluded.get("invalid_settlement_identity_or_rounding", 0) + 1
            continue
        cutoff_text = cutoff.isoformat().replace("+00:00", "Z")
        rows = conn.execute(
            """
            SELECT * FROM model_member_cache
            WHERE event_date=? AND official_updated_source='observed'
              AND datetime(created_at) < datetime(?)
              AND datetime(official_updated_at) < datetime(?)
            ORDER BY model, datetime(created_at) DESC, id DESC
            """,
            (day, cutoff_text, cutoff_text),
        ).fetchall()
        selected: dict[str, sqlite3.Row] = {}
        for row in rows:
            model = str(row["model"] or "")
            if model in MODEL_SOURCE_IDS and model not in selected:
                selected[model] = row
        if set(selected) != set(MODEL_SOURCE_IDS):
            excluded["complete_three_model_d1_snapshot_missing"] = excluded.get("complete_three_model_d1_snapshot_missing", 0) + 1
            continue
        revisions = []
        for model, source_id in MODEL_SOURCE_IDS.items():
            row = selected[model]
            try:
                members = [float(item) for item in json.loads(str(row["members_json"] or "[]"))]
            except (TypeError, ValueError, json.JSONDecodeError):
                members = []
            if not members or any(not math.isfinite(item) for item in members):
                revisions = []
                break
            raw = {
                "table": "model_member_cache", "id": int(row["id"]), "model": model,
                "event_date": day, "members_signature": row["members_signature"],
                "created_at": row["created_at"], "official_updated_at": row["official_updated_at"],
            }
            revisions.append({
                "source_id": source_id,
                "source_revision_id": f"mmc_{int(row['id'])}_{_sha(raw)[:16]}",
                "source_class": "forecast_weak_source",
                "strict_asof_archive_replay": True,
                "archive_replay_kind": "model_member_cache_observed_before_cutoff",
                "capture_created_at": str(row["created_at"]),
                "official_updated_at": str(row["official_updated_at"]),
                "available_at": str(row["created_at"]),
                "forecast_peak_c": fmean(members),
                "raw_sha256": _sha(raw),
            })
        if not revisions:
            excluded["model_members_missing_or_invalid"] = excluded.get("model_members_missing_or_invalid", 0) + 1
            continue
        label_raw = {
            "event_date": day, "settlement_actual": settlement["settlement_actual"],
            "rounding_rule": settlement["rounding_rule"], "resolution_source": settlement["resolution_source"],
            **interval,
        }
        core = {
            "event_date": day,
            "decision_as_of": cutoff_text,
            "forecast_context": {"season": "summer", "weather_regime": "unknown_forecast_regime", "lead_bucket": "d1_pre_local_day"},
            "settlement_bucket_final": {
                "label_kind": "settlement_bucket_interval", "final": True,
                "rounding_rule": "whole_degrees", "raw_sha256": _sha(label_raw), **interval,
            },
            "source_revisions": revisions,
        }
        events.append({"event_id": f"settlement_interval_{day}_{_sha(core)[:16]}", **core})
    return events, {"settled_rows": len(settlements), "accepted_event_days": len(events), "excluded_counts": dict(sorted(excluded.items()))}


def build_candidate(*, db_path: Path, minimum_train_days: int = 30) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        events, bridge = build_events(conn)
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=SOURCE_CATALOG)
    days = sorted({str(row["event_date"]) for row in dataset.get("samples") or []})
    evaluation_start = days[minimum_train_days] if len(days) > minimum_train_days else None
    training = train_joint_weak_source_set_artifact(
        dataset={"samples": [row for row in dataset.get("samples") or [] if not evaluation_start or row["event_date"] < evaluation_start], "source_catalog": SOURCE_CATALOG},
        minimum_event_days_per_surface=minimum_train_days,
        minimum_pair_samples=20,
    ) if evaluation_start else {"state": "blocked", "blockers": ["insufficient_event_days_for_frozen_oos"]}
    oos = evaluate_joint_weak_walk_forward(
        dataset=dataset, evaluation_start_date=evaluation_start,
        minimum_event_days_per_surface=minimum_train_days, minimum_pair_samples=20,
    ) if evaluation_start else {"state": "blocked", "blockers": ["insufficient_event_days_for_frozen_oos"]}
    report = {
        "schema": SCHEMA_VERSION, "state": "shadow_candidate_built" if training.get("artifact") else "blocked",
        "db_path": str(db_path), "bridge": bridge, "dataset_manifest": dataset.get("manifest"),
        "evaluation_start_date": evaluation_start, "training": training, "oos": oos,
        "boundary": {"research_only": True, "settlement_bucket_is_interval_not_exact_x": True, "writes_active_pointer": False, "writes_posterior": False, "places_orders": False},
    }
    report["report_sha256"] = _sha(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "archive.db")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "research" / "joint_forecast_weak_likelihood" / "settlement_interval_candidate_latest.json")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = build_candidate(db_path=args.db.resolve())
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        existing_sha = None
        try:
            existing_sha = json.loads(args.output.read_text(encoding="utf-8")).get("report_sha256")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            pass
        if existing_sha != report.get("report_sha256"):
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": report["schema"], "state": report["state"],
        "report_sha256": report["report_sha256"], "bridge": report["bridge"],
        "dataset_manifest": report["dataset_manifest"],
        "artifact_sha256": (report.get("training") or {}).get("artifact", {}).get("artifact_sha256"),
        "oos_safety": (report.get("oos") or {}).get("oos_safety"),
        "output": str(args.output.resolve()) if args.write else None,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["state"] == "shadow_candidate_built" else 2


if __name__ == "__main__":
    raise SystemExit(main())
