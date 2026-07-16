#!/usr/bin/env python3
"""Materialize shadow-only weak-bias research from immutable station-day events.

This is the sole automated consumer of closed physical-X station-day candidate
files.  It writes only under ``data/research`` and cannot activate an artifact,
write an archive DB, touch posterior state, or place orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_forecast_weak_likelihood_oos_runtime import (  # noqa: E402
    evaluate_joint_weak_expanding_window,
    evaluate_joint_weak_walk_forward,
)
from joint_forecast_weak_likelihood_training_runtime import (  # noqa: E402
    build_joint_weak_training_dataset,
    train_joint_weak_source_set_artifact,
)
from scripts.build_official_forecast_source_accuracy_report import _load_candidate_events  # noqa: E402


SCHEMA_VERSION = "joint_weak_bias_research_pipeline_v1"
PIPELINE_CONTRACT_VERSION = "joint_weak_bias_research_pipeline_contract_v2"
RESEARCH_ROOT = ROOT / "data" / "research" / "joint_forecast_weak_likelihood"
DEFAULT_EVENT_ROOT = RESEARCH_ROOT / "station_day_candidates"
DEFAULT_OUTPUT_ROOT = RESEARCH_ROOT / "bias_pipeline"
DEFAULT_MINIMUM_EVENT_DAYS = 30
DEFAULT_MINIMUM_PAIR_SAMPLES = 20
DEFAULT_SOURCE_CATALOG = {
    "ecmwf": {"provider_family": "nwp"}, "icon": {"provider_family": "nwp"},
    "gfs": {"provider_family": "nwp"}, "jma": {"provider_family": "nwp"},
    "taf": {"provider_family": "taf"}, "wu": {"provider_family": "consumer_weather"},
    "twc": {"provider_family": "consumer_weather"},
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _safe_research_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    canonical = RESEARCH_ROOT.resolve()
    if resolved != canonical and canonical not in resolved.parents:
        raise ValueError("joint_weak_bias_pipeline_output_outside_research_root")
    return resolved


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("joint_weak_bias_pipeline_immutable_artifact_collision")
        return "existing_immutable"
    _atomic_write(path, payload)
    return "appended_immutable"


def _pipeline_contract_sha256(
    *, source_catalog: Mapping[str, Any], minimum_event_days_per_surface: int,
    minimum_pair_samples: int,
) -> str:
    """Bind deduplication to algorithm/config identity, not data alone."""

    return _sha(
        {
            "pipeline_contract_version": PIPELINE_CONTRACT_VERSION,
            "pipeline_schema": SCHEMA_VERSION,
            "source_catalog": dict(source_catalog),
            "minimum_event_days_per_surface": int(minimum_event_days_per_surface),
            "minimum_pair_samples": int(minimum_pair_samples),
            "bias_ewma_alpha_candidates": [0.03, 0.05],
            "frozen_evaluation": "strict_event_date_walk_forward_v1",
            "rolling_evaluation": "expanding_window_research_only_v1",
            "bias_state_asof_policy": "training_event_date_max_strictly_before_scored_event_date",
        }
    )


def _existing_report_for_dataset(root: Path, dataset_sha: str, pipeline_contract_sha256: str) -> str | None:
    try:
        latest = _mapping(json.loads((root / "latest.json").read_text(encoding="utf-8")))
        report_path = Path(_text(latest.get("report_path"))).expanduser().resolve()
        if root != report_path.parent.parent and root not in report_path.parents:
            return None
        report = _mapping(json.loads(report_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if (
        _text(report.get("dataset_sha256")) == dataset_sha
        and _text(report.get("pipeline_contract_sha256")) == pipeline_contract_sha256
    ):
        return _text(report.get("pipeline_sha256"))
    return None


def _evaluation_start(events: Sequence[Mapping[str, Any]], minimum_event_days: int) -> str | None:
    days = sorted({_text(event.get("event_date"))[:10] for event in events if _text(event.get("event_date"))})
    return days[int(minimum_event_days)] if len(days) > int(minimum_event_days) else None


def _readiness(dataset: Mapping[str, Any], frozen_oos: Mapping[str, Any], rolling: Mapping[str, Any] | None, *, minimum_event_days: int) -> dict[str, Any]:
    manifest = _mapping(dataset.get("manifest"))
    event_days = int(manifest.get("event_day_count") or 0)
    frozen: dict[str, Any] = {}
    selected_alpha = None
    promotion_reasons: list[str] = []
    for alpha, result in sorted(frozen_oos.items()):
        value = _mapping(result)
        selection = _mapping(value.get("bias_alpha_selection"))
        safety = _mapping(value.get("oos_safety"))
        frozen[alpha] = {"state": _text(value.get("state")) or "not_run", "sample_count": value.get("sample_count", 0), "safety_state": _text(safety.get("state")) or "not_run", "safety_blockers": list(safety.get("blockers") or []), "candidate_is_selected": selection.get("candidate_is_selected") is True, "selected_artifact_sha256": _text(selection.get("selected_artifact_sha256")), "selection_na_reason": _text(selection.get("na_reason"))}
        if selection.get("candidate_is_selected") is True:
            selected_alpha = alpha
        if safety.get("state") != "passed":
            promotion_reasons.extend(str(item) for item in safety.get("blockers") or ["oos_safety_not_passed"])
    if event_days < int(minimum_event_days):
        promotion_reasons.append("insufficient_independent_event_days")
    if not frozen_oos:
        promotion_reasons.append("frozen_oos_not_run")
    if selected_alpha is None and frozen_oos:
        promotion_reasons.append("no_alpha_candidate_selected")
    rolling_map = _mapping(rolling)
    return {
        "schema": "joint_weak_bias_research_readiness_v1",
        "state": "pending_formal_samples" if event_days < int(minimum_event_days) else "shadow_evidence_pending_promotion",
        "research_only": True, "production_weight_input": False,
        "training_event_day_count": event_days, "minimum_independent_event_days": int(minimum_event_days),
        "frozen_alpha_oos": frozen,
        "selected_alpha": selected_alpha,
        "rolling_origin": {"state": _text(rolling_map.get("state")) or "not_run", "fold_count": rolling_map.get("fold_count", 0), "promotion_eligible": rolling_map.get("promotion_eligible") is True, "promotion_na_reason": _text(rolling_map.get("promotion_na_reason"))},
        "promotion_state": "not_eligible_research_only",
        "promotion_blockers": sorted(set(promotion_reasons + ["separate_approved_review_and_active_pointer_required"])),
    }


def run_joint_weak_bias_research_pipeline(
    *, event_root: Path = DEFAULT_EVENT_ROOT, output_root: Path = DEFAULT_OUTPUT_ROOT,
    source_catalog: Mapping[str, Any] = DEFAULT_SOURCE_CATALOG,
    minimum_event_days_per_surface: int = DEFAULT_MINIMUM_EVENT_DAYS,
    minimum_pair_samples: int = DEFAULT_MINIMUM_PAIR_SAMPLES,
    allow_research_write: bool = False,
) -> dict[str, Any]:
    """Build reproducible artifacts/reports from candidate events, never live state."""

    root = _safe_research_root(output_root)
    events, load_excluded = _load_candidate_events(event_root.expanduser().resolve())
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=source_catalog)
    dataset_sha = _text(_mapping(dataset.get("manifest")).get("dataset_sha256"))
    pipeline_contract_sha = _pipeline_contract_sha256(
        source_catalog=source_catalog,
        minimum_event_days_per_surface=minimum_event_days_per_surface,
        minimum_pair_samples=minimum_pair_samples,
    )
    if allow_research_write and dataset_sha:
        existing_sha = _existing_report_for_dataset(root, dataset_sha, pipeline_contract_sha)
        if existing_sha:
            return {
                "schema": SCHEMA_VERSION, "state": "skipped_unchanged_dataset", "dataset_sha256": dataset_sha,
                "pipeline_contract_version": PIPELINE_CONTRACT_VERSION,
                "pipeline_contract_sha256": pipeline_contract_sha,
                "existing_pipeline_sha256": existing_sha, "candidate_load_excluded_counts": load_excluded,
                "write_authorized": True,
                "boundary": {"research_only": True, "writes_runtime_artifact_pointer": False, "writes_archive_db": False, "writes_posterior": False, "places_orders": False, "training_or_oos_is_not_live_ingest_gate": True},
            }
    start = _evaluation_start(dataset.get("samples") if isinstance(dataset.get("samples"), Sequence) else [], minimum_event_days_per_surface)
    artifacts: dict[str, Any] = {}
    frozen_oos: dict[str, Any] = {}
    rolling: dict[str, Any] | None = None
    if dataset.get("state") == "available" and start:
        for alpha in (0.03, 0.05):
            training_samples = [row for row in dataset.get("samples") or [] if _text(_mapping(row).get("event_date"))[:10] < start]
            prestart_dataset = {"samples": training_samples, "source_catalog": dict(source_catalog), "manifest": {"pipeline_evaluation_start_date": start}}
            trained = train_joint_weak_source_set_artifact(
                dataset=prestart_dataset, minimum_event_days_per_surface=minimum_event_days_per_surface,
                minimum_pair_samples=minimum_pair_samples, bias_ewma_alpha=alpha,
            )
            artifact = _mapping(trained.get("artifact"))
            artifacts[str(alpha)] = artifact
            frozen_oos[str(alpha)] = evaluate_joint_weak_walk_forward(
                dataset=dataset, evaluation_start_date=start,
                minimum_event_days_per_surface=minimum_event_days_per_surface,
                minimum_pair_samples=minimum_pair_samples, candidate_bias_ewma_alpha=alpha,
            )
        rolling = evaluate_joint_weak_expanding_window(
            dataset=dataset, evaluation_start_date=start,
            minimum_event_days_per_surface=minimum_event_days_per_surface,
            minimum_pair_samples=minimum_pair_samples,
        )
    report_core = {
        "schema": SCHEMA_VERSION, "dataset_sha256": dataset_sha, "event_root": str(event_root.expanduser().resolve()),
        "pipeline_contract_version": PIPELINE_CONTRACT_VERSION,
        "pipeline_contract_sha256": pipeline_contract_sha,
        "candidate_load_excluded_counts": load_excluded, "dataset": dataset,
        "evaluation_start_date": start, "frozen_alpha_oos": frozen_oos,
        "rolling_origin_research": rolling,
        "readiness": _readiness(dataset, frozen_oos, rolling, minimum_event_days=minimum_event_days_per_surface),
        "boundary": {"research_only": True, "artifact_approval_state": "shadow_only", "writes_runtime_artifact_pointer": False, "writes_archive_db": False, "writes_posterior": False, "places_orders": False, "training_or_oos_is_not_live_ingest_gate": True},
    }
    report = {**report_core, "pipeline_sha256": _sha(report_core)}
    write_outcomes: dict[str, str] = {}
    if allow_research_write:
        for alpha, artifact in artifacts.items():
            artifact_sha = _text(artifact.get("artifact_sha256"))
            if artifact_sha:
                write_outcomes[f"artifact_alpha_{alpha}"] = _write_immutable(root / "artifacts" / f"{artifact_sha}.json", artifact)
        write_outcomes["report"] = _write_immutable(root / "reports" / f"{report['pipeline_sha256']}.json", report)
        _atomic_write(root / "latest.json", {
            "schema": SCHEMA_VERSION,
            "pipeline_contract_version": PIPELINE_CONTRACT_VERSION,
            "pipeline_contract_sha256": pipeline_contract_sha,
            "dataset_sha256": dataset_sha,
            "pipeline_sha256": report["pipeline_sha256"],
            "report_path": str(root / "reports" / f"{report['pipeline_sha256']}.json"),
            "research_latest_pointer_only": True,
        })
    state = "materialized_shadow_research" if artifacts else "pending_formal_samples"
    return {**report, "state": state, "write_outcomes": write_outcomes, "write_authorized": allow_research_write}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-root", type=Path, default=DEFAULT_EVENT_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--minimum-event-days-per-surface", type=int, default=DEFAULT_MINIMUM_EVENT_DAYS)
    parser.add_argument("--minimum-pair-samples", type=int, default=DEFAULT_MINIMUM_PAIR_SAMPLES)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    result = run_joint_weak_bias_research_pipeline(
        event_root=args.event_root, output_root=args.output_root,
        minimum_event_days_per_surface=args.minimum_event_days_per_surface,
        minimum_pair_samples=args.minimum_pair_samples, allow_research_write=args.allow_research_write,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
