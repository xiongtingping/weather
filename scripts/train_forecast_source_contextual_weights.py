#!/usr/bin/env python3
"""Train a reproducible external-only contextual source-weight challenger.

The command is dry-run by default.  It never reads the product's local weather
history and can only write an artifact below the dedicated research directory
when ``--allow-research-write`` is supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_source_contextual_weight_model import fit_contextual_stacking


SCHEMA_VERSION = "forecast_source_contextual_weight_training_report_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_training_samples(
    score_rows: Sequence[Mapping[str, Any]],
    *,
    immutable_only: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collapse source score rows into one mixture sample per event/cutoff."""

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    excluded: dict[str, int] = defaultdict(int)
    for row in score_rows:
        if row.get("state") != "available":
            excluded["score_unavailable"] += 1
            continue
        if immutable_only and not str(row.get("lineage_quality") or "").startswith("immutable_"):
            excluded["lineage_not_immutable"] += 1
            continue
        event_id = str(row.get("event_id") or "")
        cutoff_id = str(row.get("cutoff_id") or "")
        if not event_id or not cutoff_id:
            excluded["event_or_cutoff_missing"] += 1
            continue
        grouped[(event_id, cutoff_id)].append(row)

    samples: list[dict[str, Any]] = []
    lineage_ids: set[str] = set()
    for (event_id, cutoff_id), rows in sorted(grouped.items()):
        winners = {str(row.get("winning_bucket") or "") for row in rows}
        dates = {str(row.get("event_date_local") or "") for row in rows}
        if len(winners) != 1 or "" in winners:
            excluded["winning_bucket_conflict"] += len(rows)
            continue
        if len(dates) != 1 or "" in dates:
            excluded["event_date_conflict"] += len(rows)
            continue
        distributions: dict[str, dict[str, float]] = {}
        provider_families: dict[str, str] = {}
        for row in rows:
            source = str(row.get("source_id") or "")
            distribution = row.get("bucket_probabilities")
            if not source or not isinstance(distribution, Mapping):
                excluded["source_distribution_missing"] += 1
                continue
            distributions[source] = {str(key): float(value) for key, value in distribution.items()}
            provider_families[source] = str(row.get("provider_family") or source)
            lineage_ids.update(str(value) for value in row.get("forecast_snapshot_ids") or [] if value)
        if not distributions:
            excluded["sample_without_sources"] += 1
            continue
        context = next((dict(row.get("context") or {}) for row in rows if isinstance(row.get("context"), Mapping)), {})
        lead_hours = next((row.get("lead_hours") for row in rows if row.get("lead_hours") is not None), None)
        settlement_at = max((str(row.get("settlement_at") or "") for row in rows), default="")
        bucket_orders = [list(row.get("bucket_order") or []) for row in rows if row.get("bucket_order")]
        bucket_order = bucket_orders[0] if bucket_orders else list(next(iter(distributions.values())).keys())
        if any(order != bucket_order for order in bucket_orders):
            excluded["bucket_order_conflict"] += len(rows)
            continue
        samples.append(
            {
                "event_id": event_id,
                "cutoff_id": cutoff_id,
                "event_date_local": next(iter(dates)),
                "settlement_at": settlement_at,
                "lead_hours": lead_hours,
                "context": context,
                "winning_bucket": next(iter(winners)),
                "winning_bucket_edge": any(bool(row.get("winning_bucket_edge")) for row in rows),
                "bucket_order": bucket_order,
                "distributions": dict(sorted(distributions.items())),
                "provider_families": dict(sorted(provider_families.items())),
            }
        )
    samples.sort(key=lambda row: (row["event_date_local"], row["event_id"], row["cutoff_id"]))
    samples_per_day: dict[str, int] = defaultdict(int)
    for sample in samples:
        samples_per_day[str(sample["event_date_local"])] += 1
    for sample in samples:
        sample["sample_weight"] = 1.0 / samples_per_day[str(sample["event_date_local"])]
    event_days = {str(row["event_date_local"]) for row in samples}
    source_days: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        for source in sample["distributions"]:
            source_days[source].add(str(sample["event_date_local"]))
    manifest = {
        "source_data_policy": "external_only",
        "immutable_only": bool(immutable_only),
        "sample_count": len(samples),
        "paired_event_day_count": len(event_days),
        "effective_event_day_weight_sum": sum(float(row["sample_weight"]) for row in samples),
        "paired_event_count_by_source": {source: len(days) for source, days in sorted(source_days.items())},
        "lineage_id_count": len(lineage_ids),
        "lineage_ids_sha256": _sha256(sorted(lineage_ids)),
        "excluded_counts": dict(sorted(excluded.items())),
    }
    manifest["training_manifest_sha256"] = _sha256({"samples": samples, "manifest": manifest})
    return samples, manifest


def train_contextual_weight_challenger(
    score_rows: Sequence[Mapping[str, Any]],
    *,
    context_fields: Sequence[str] = (),
    minimum_training_event_days: int = 30,
    parent_artifact_sha256: str = "",
    model_options: Mapping[str, Any] | None = None,
    specification: str = "weather_context",
) -> dict[str, Any]:
    samples, manifest = build_training_samples(score_rows, immutable_only=True)
    paired_days = int(manifest["paired_event_day_count"])
    blockers: list[str] = []
    if paired_days < int(minimum_training_event_days):
        blockers.append("paired_event_days_below_contextual_training_minimum")
    provider_families: dict[str, str] = {}
    for sample in samples:
        provider_families.update(sample["provider_families"])
    artifact = None
    if not blockers:
        options = dict(model_options or {})
        artifact = fit_contextual_stacking(
            samples,
            provider_families=provider_families,
            context_fields=context_fields,
            **options,
        )
        artifact.update(
            {
                "trained_through_settlement_at": max((str(row.get("settlement_at") or "") for row in samples), default=""),
                "market_event_count": len({str(row["event_id"]) for row in samples}),
                "paired_event_count_by_source": manifest["paired_event_count_by_source"],
                "context_schema": {"fields": [str(value) for value in context_fields], "missing_policy": "training_mean_parent_fallback"},
                "training_manifest_sha256": manifest["training_manifest_sha256"],
                "parent_artifact_sha256": str(parent_artifact_sha256 or ""),
                "selected_specification": str(specification),
            }
        )
        artifact["artifact_sha256"] = _sha256({key: value for key, value in artifact.items() if key != "artifact_sha256"})
    return {
        "schema": SCHEMA_VERSION,
        "state": "trained" if artifact is not None else "blocked",
        "state_reason": "" if artifact is not None else ",".join(blockers),
        "training_manifest": manifest,
        "artifact": artifact,
        "release_gate": {
            "research_training_eligible": not blockers,
            "strict_oos_evaluated": False,
            "shadow_eligible": False,
            "production_promotion_eligible": False,
            "blockers": blockers,
        },
        "guards": {
            "reads_existing_local_history": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("contextual_weight_output_outside_research_root")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-dataset", required=True, type=Path)
    parser.add_argument("--context-field", action="append", default=[])
    parser.add_argument("--specification", choices=("global", "seasonal_lead", "weather_context"), default="weather_context")
    parser.add_argument("--minimum-training-event-days", type=int, default=30)
    parser.add_argument("--parent-artifact-sha256", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifact-output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    payload = _load_json(args.score_dataset)
    rows = payload.get("score_rows") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("score_dataset_rows_missing")
    context_fields = args.context_field if args.specification == "weather_context" else []
    model_options = (
        {"seasonal_order": 0, "include_lead_time": False}
        if args.specification == "global"
        else {"seasonal_order": 2, "include_lead_time": True}
    )
    report = train_contextual_weight_challenger(
        rows,
        context_fields=context_fields,
        minimum_training_event_days=args.minimum_training_event_days,
        parent_artifact_sha256=args.parent_artifact_sha256,
        model_options=model_options,
        specification=args.specification,
    )
    if args.output or args.artifact_output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
    if args.output:
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_canonical_bytes(report) + b"\n")
    if args.artifact_output:
        if not isinstance(report.get("artifact"), Mapping):
            raise ValueError("trained_artifact_missing")
        artifact_output = _safe_output(args.artifact_output)
        artifact_output.parent.mkdir(parents=True, exist_ok=True)
        artifact_output.write_bytes(_canonical_bytes(report["artifact"]) + b"\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
