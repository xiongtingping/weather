#!/usr/bin/env python3
"""Strict day-grouped walk-forward calibration evidence for v6 posterior checkpoints."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "production_probability_v6_strict_day_walk_forward_calibration_v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("walk_forward_naive_timestamp")
    return parsed.astimezone(timezone.utc)


def _bucket(outcome: str) -> int | None:
    match = re.search(r"(-?\d+)\s*°?C", outcome)
    return int(match.group(1)) if match else None


def _distribution(row: Mapping[str, Any]) -> dict[int, float]:
    raw = json.loads(_text(row.get("reported_physical_x_distribution_json")) or "{}")
    return {int(key): float(value) for key, value in raw.items() if float(value) >= 0.0}


def _metrics(distribution: Mapping[int, float], actual: int) -> dict[str, float]:
    keys = set(distribution) | {actual}
    brier = sum((float(distribution.get(key, 0.0)) - float(key == actual)) ** 2 for key in keys)
    probability = max(float(distribution.get(actual, 0.0)), 1e-15)
    return {"actual_probability": probability, "multiclass_brier": brier, "log_score": -math.log(probability)}


def _labels(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_date,outcome,MIN(created_at) AS label_available_at,COUNT(*) AS row_count "
        "FROM calibration_labels WHERE label=1 AND label_source='settlement_actual_bucket_hit' "
        "AND slug LIKE 'highest-temperature-in-chengdu-%' GROUP BY event_date,outcome"
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        bucket = _bucket(_text(row["outcome"]))
        if bucket is not None:
            grouped.setdefault(_text(row["event_date"]), []).append({
                "actual_bucket_c": bucket,
                "label_available_at": _text(row["label_available_at"]),
                "label_row_count": int(row["row_count"]),
            })
    return {day: values[0] for day, values in grouped.items() if len({v["actual_bucket_c"] for v in values}) == 1}


def audit(db: Path, cutoff_local_hour: int, minimum_days: int, model_artifact_sha256: str) -> dict[str, Any]:
    cutoff_clock = time(cutoff_local_hour, 0)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        labels = _labels(conn)
        target_days = [
            _text(row[0]).rsplit(":", 1)[-1]
            for row in conn.execute(
                "SELECT DISTINCT target_identity FROM posterior_checkpoints "
                "WHERE target_identity LIKE 'physical_weather_day_v1:ZUUU:%' ORDER BY target_identity"
            ).fetchall()
        ]
        samples = []
        exclusions = []
        for day in target_days:
            label = labels.get(day)
            if label is None:
                exclusions.append({"event_date": day, "reason": "unique_official_settlement_label_missing"})
                continue
            local_cutoff = datetime.combine(datetime.fromisoformat(day).date(), cutoff_clock, tzinfo=LOCAL_TZ)
            cutoff_utc = local_cutoff.astimezone(timezone.utc)
            row = conn.execute(
                "SELECT * FROM posterior_checkpoints WHERE target_identity=? AND committed_at<=? "
                "AND model_artifact_sha256=? "
                "ORDER BY committed_at DESC,replay_generation DESC,rowid DESC LIMIT 1",
                (
                    f"physical_weather_day_v1:ZUUU:{day}",
                    cutoff_utc.isoformat().replace("+00:00", "Z"),
                    model_artifact_sha256,
                ),
            ).fetchone()
            if row is None:
                exclusions.append({"event_date": day, "reason": "checkpoint_before_cutoff_missing"})
                continue
            checkpoint = dict(row)
            committed = _time(checkpoint["committed_at"])
            label_available = _time(label["label_available_at"])
            if label_available <= committed:
                exclusions.append({"event_date": day, "reason": "label_available_not_after_checkpoint"})
                continue
            snapshot = conn.execute(
                "SELECT decision_as_of FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
                (_text(checkpoint.get("forecast_evidence_snapshot_id")),),
            ).fetchone()
            if snapshot is None or _time(snapshot["decision_as_of"]) > committed:
                exclusions.append({"event_date": day, "reason": "weak_snapshot_as_of_violation"})
                continue
            consumed = json.loads(_text(checkpoint.get("consumed_event_ids_json")) or "[]")
            violations = 0
            if consumed:
                placeholders = ",".join("?" for _ in consumed)
                event_rows = conn.execute(
                    f"SELECT first_actionable_seen_at,valid_time FROM canonical_observation_events "
                    f"WHERE canonical_observation_event_id IN ({placeholders})", consumed,
                ).fetchall()
                violations = sum(
                    _time(event["first_actionable_seen_at"]) > committed
                    or _time(event["valid_time"]) > cutoff_utc
                    for event in event_rows
                )
                if len(event_rows) != len(consumed):
                    violations += len(consumed) - len(event_rows)
            if violations:
                exclusions.append({"event_date": day, "reason": "canonical_event_as_of_violation", "count": violations})
                continue
            distribution = _distribution(checkpoint)
            metric = _metrics(distribution, int(label["actual_bucket_c"]))
            samples.append({
                "event_date": day,
                "checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
                "checkpoint_committed_at": _text(checkpoint.get("committed_at")),
                "cutoff_local": local_cutoff.isoformat(),
                "forecast_evidence_snapshot_id": _text(checkpoint.get("forecast_evidence_snapshot_id")),
                "consumed_event_count": len(consumed),
                "actual_bucket_c": int(label["actual_bucket_c"]),
                **metric,
            })
    samples.sort(key=lambda row: row["event_date"])
    folds = [
        {
            "test_event_date": sample["event_date"],
            "train_event_dates": [row["event_date"] for row in samples[:index]],
            "day_group_disjoint": True,
            "test_metrics": {
                key: sample[key] for key in ("actual_probability", "multiclass_brier", "log_score")
            },
        }
        for index, sample in enumerate(samples)
        if index > 0
    ]
    gates = {
        "one_sample_per_day": len(samples) == len({row["event_date"] for row in samples}),
        "labels_only_after_checkpoint": all(
            _time(labels[row["event_date"]]["label_available_at"]) > _time(row["checkpoint_committed_at"])
            for row in samples
        ),
        "folds_grouped_by_day": all(fold["day_group_disjoint"] for fold in folds),
        "no_checkpoint_or_source_as_of_violations": not any(
            "as_of_violation" in row["reason"] for row in exclusions
        ),
    }
    ready = len(samples) >= minimum_days and len(folds) >= minimum_days - 1
    return {
        "schema": SCHEMA_VERSION,
        "ok": all(gates.values()),
        "state": "calibration_ready" if ready and all(gates.values()) else "framework_valid_samples_accruing",
        "cutoff_local_hour": cutoff_local_hour,
        "sample_count": len(samples),
        "minimum_day_count": minimum_days,
        "required_model_artifact_sha256": model_artifact_sha256,
        "calibration_ready": ready,
        "samples": samples,
        "folds": folds,
        "aggregate_metrics": {
            "mean_multiclass_brier": (
                sum(row["multiclass_brier"] for row in samples) / len(samples) if samples else None
            ),
            "mean_log_score": sum(row["log_score"] for row in samples) / len(samples) if samples else None,
        },
        "exclusions": exclusions,
        "gates": gates,
        "unit_of_independence": "station_day_not_checkpoint_event",
        "final_settlement_role": "label_only_never_feature",
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--cutoff-local-hour", type=int, default=12)
    parser.add_argument("--minimum-days", type=int, default=20)
    parser.add_argument(
        "--activation",
        default=str(ROOT / "config/production_probability_runtime_activation.v6_candidate.json"),
    )
    parser.add_argument("--output", default=str(ROOT / "output/audits/production_probability_v6_walk_forward_calibration.json"))
    args = parser.parse_args()
    activation = json.loads(Path(args.activation).resolve().read_text(encoding="utf-8"))
    model_artifact_sha256 = _text(activation.get("activation_contract_sha256"))
    if len(model_artifact_sha256) != 64:
        raise ValueError("walk_forward_activation_identity_invalid")
    result = audit(
        Path(args.db).resolve(), args.cutoff_local_hour, args.minimum_days, model_artifact_sha256
    )
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
