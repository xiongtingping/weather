#!/usr/bin/env python3
"""Materialize the fail-closed static source-weight champion as an artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"

from forecast_source_contextual_weight_model import constrain_source_weights
from forecast_source_weight_artifact_registry import canonical_artifact_sha256
from scripts.train_forecast_source_contextual_weights import build_training_samples


def build_static_champion(score_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    samples, manifest = build_training_samples(score_rows, immutable_only=True)
    entities = sorted({source for sample in samples for source in sample.get("distributions", {})})
    provider_families: dict[str, str] = {}
    for sample in samples:
        provider_families.update({str(key): str(value) for key, value in sample.get("provider_families", {}).items()})
    constraints = {"source_floor": 0.05, "source_cap": 0.45, "family_floor": 0.05, "family_cap": 0.70}
    global_weights = constrain_source_weights(
        {source: 1.0 for source in entities},
        provider_families=provider_families,
        **constraints,
    )
    artifact = {
        "schema": "forecast_source_contextual_weight_artifact_v1",
        "version": "static_champion_research_v1",
        "research_only": True,
        "production_ready": False,
        "runtime_integrated": False,
        "runtime_champion_mode": "legacy_no_active_artifact",
        "source_data_policy": "external_only",
        "entities": entities,
        "provider_families": provider_families,
        "context_fields": [],
        "context_support": {},
        "sparse_context_fallback_enabled": True,
        "seasonal_order": 0,
        "include_lead_time": False,
        "feature_names": ["intercept"],
        "feature_means": [0.0],
        "feature_scales": [1.0],
        "coefficients": [[0.0] for _source in entities],
        "weight_constraints": constraints,
        "global_weights": global_weights,
        "training_manifest_sha256": manifest["training_manifest_sha256"],
        "paired_event_count_by_source": manifest["paired_event_count_by_source"],
        "trained_through_settlement_at": max((str(row.get("settlement_at") or "") for row in samples), default=""),
        "market_event_count": len({str(row.get("event_id") or "") for row in samples}),
        "training": {"sample_count_input": len(samples), "sample_count_usable": len(samples), "optimizer": "none_static_champion"},
        "gates": {"strict_oos_evaluated": True, "nll_improved": False, "rps_improved": False, "promotion_eligible": False},
    }
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.score_dataset.read_text(encoding="utf-8"))
    rows = payload.get("score_rows") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("score_rows_missing")
    artifact = build_static_champion(rows)
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        root = DEFAULT_RESEARCH_ROOT.resolve()
        if output != root and root not in output.parents:
            raise ValueError("static_champion_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
