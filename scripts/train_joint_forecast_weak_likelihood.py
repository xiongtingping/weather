#!/usr/bin/env python3
"""Build a shadow-only exact-source-set weak-likelihood artifact.

Input is an immutable JSON event envelope assembled outside this command.  The
default mode is read-only and prints a report; writing requires an explicit
research-only flag and can never update a runtime artifact pointer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from joint_forecast_weak_likelihood_training_runtime import (  # noqa: E402
    build_joint_weak_training_dataset,
    train_joint_weak_source_set_artifact,
)


DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "joint_forecast_weak_likelihood"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_research_path(value: Path) -> Path:
    path = value.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if path != root and root not in path.parents:
        raise ValueError("joint_weak_artifact_output_outside_research_root")
    return path


def build_training_report(
    *, events_payload: Mapping[str, Any], source_catalog: Mapping[str, Any], minimum_event_days_per_surface: int,
    minimum_pair_samples: int, bias_ewma_alpha: float = 0.05,
) -> dict[str, Any]:
    events = events_payload.get("events") if isinstance(events_payload.get("events"), Sequence) else []
    dataset = build_joint_weak_training_dataset(events=events, source_catalog=source_catalog)
    report = train_joint_weak_source_set_artifact(
        dataset=dataset,
        minimum_event_days_per_surface=minimum_event_days_per_surface,
        minimum_pair_samples=minimum_pair_samples,
        bias_ewma_alpha=bias_ewma_alpha,
    )
    report["input"] = {
        "event_count": len(events),
        "source_catalog_ids": sorted(_mapping(source_catalog)),
        "runtime_activation_attempted": False,
    }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", required=True, type=Path, help="immutable strict-as-of + physical-X event envelope")
    parser.add_argument("--source-catalog", required=True, type=Path)
    parser.add_argument("--minimum-event-days-per-surface", type=int, default=30)
    parser.add_argument("--minimum-pair-samples", type=int, default=20)
    parser.add_argument("--bias-ewma-alpha", type=float, choices=(0.03, 0.05), default=0.05)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    events_payload = _mapping(json.loads(args.events.read_text(encoding="utf-8")))
    catalog_payload = _mapping(json.loads(args.source_catalog.read_text(encoding="utf-8")))
    catalog = _mapping(catalog_payload.get("source_catalog") or catalog_payload)
    report = build_training_report(
        events_payload=events_payload,
        source_catalog=catalog,
        minimum_event_days_per_surface=args.minimum_event_days_per_surface,
        minimum_pair_samples=args.minimum_pair_samples,
        bias_ewma_alpha=args.bias_ewma_alpha,
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("joint_weak_research_write_requires_explicit_allow_flag")
        output = _safe_research_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("state") == "trained_shadow_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
