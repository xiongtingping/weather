#!/usr/bin/env python3
"""Sequential strict-OOS ablation: global -> seasonal/lead -> weather context."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_forecast_source_weight_shadow import evaluate_contextual_weight_oos


SCHEMA_VERSION = "forecast_source_contextual_ablation_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def evaluate_contextual_ablation(
    score_rows: Sequence[Mapping[str, Any]],
    *,
    context_fields: Sequence[str] = (),
    minimum_train_event_days: int = 30,
    fold_event_days: int = 7,
    bootstrap_replicates: int = 5000,
    model_iterations: int = 1500,
) -> dict[str, Any]:
    common = {
        "minimum_train_event_days": minimum_train_event_days,
        "fold_event_days": fold_event_days,
        "bootstrap_replicates": bootstrap_replicates,
        "segment_minimum_ess": 20,
    }
    global_options = {"seasonal_order": 0, "include_lead_time": False, "max_iterations": model_iterations}
    seasonal_options = {"seasonal_order": 2, "include_lead_time": True, "max_iterations": model_iterations}
    weather_options = dict(seasonal_options)
    global_report = evaluate_contextual_weight_oos(
        score_rows,
        context_fields=(),
        model_options=global_options,
        **common,
    )
    seasonal_report = evaluate_contextual_weight_oos(
        score_rows,
        context_fields=(),
        model_options=seasonal_options,
        champion_context_fields=(),
        champion_model_options=global_options,
        **common,
    )
    weather_report = evaluate_contextual_weight_oos(
        score_rows,
        context_fields=context_fields,
        model_options=weather_options,
        champion_context_fields=(),
        champion_model_options=seasonal_options,
        **common,
    )
    global_kept = bool(global_report["release_gate"]["statistical_gate_passed"])
    seasonal_kept = bool(global_kept and seasonal_report["release_gate"]["statistical_gate_passed"])
    weather_kept = bool(seasonal_kept and context_fields and weather_report["release_gate"]["statistical_gate_passed"])
    selected = "weather_context" if weather_kept else "seasonal_lead" if seasonal_kept else "global" if global_kept else "static_champion"
    return {
        "schema": SCHEMA_VERSION,
        "source_data_policy": "external_only",
        "layer_order": ["global", "seasonal_lead", "weather_context"],
        "layers": {
            "global": {"retained": global_kept, "incremental_vs": "static_champion", "report": global_report},
            "seasonal_lead": {"retained": seasonal_kept, "incremental_vs": "global", "report": seasonal_report},
            "weather_context": {"retained": weather_kept, "incremental_vs": "seasonal_lead", "report": weather_report},
        },
        "selected_specification": selected,
        "weather_context_fallback": "seasonal_lead" if seasonal_kept else "global" if global_kept else "static_champion",
        "production_apply_allowed": False,
        "guards": {"writes_archive_db": False, "changes_parameters": False, "places_orders": False, "restarts_launchd": False},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-dataset", required=True, type=Path)
    parser.add_argument("--context-field", action="append", default=[])
    parser.add_argument("--minimum-train-event-days", type=int, default=30)
    parser.add_argument("--fold-event-days", type=int, default=7)
    parser.add_argument("--bootstrap-replicates", type=int, default=5000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.score_dataset.read_text(encoding="utf-8"))
    rows = payload.get("score_rows") if isinstance(payload, Mapping) else payload
    if not isinstance(rows, list):
        raise ValueError("score_rows_missing")
    report = evaluate_contextual_ablation(
        rows,
        context_fields=args.context_field,
        minimum_train_event_days=args.minimum_train_event_days,
        fold_event_days=args.fold_event_days,
        bootstrap_replicates=args.bootstrap_replicates,
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = args.output.expanduser().resolve()
        research_root = DEFAULT_RESEARCH_ROOT.resolve()
        if output != research_root and research_root not in output.parents:
            raise ValueError("contextual_ablation_output_outside_research_root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
