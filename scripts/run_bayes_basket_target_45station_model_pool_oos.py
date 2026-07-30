#!/usr/bin/env python3
"""Run a registry-bound 45-station explicit-cycle multi-model OOS pool.

The station runner owns exact joins and scoring.  This orchestrator only
fans the same content-verified evidence pool across the canonical registry
and emits a bounded production-no-live review manifest.  It never promotes
an artifact or writes SQLite.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from scripts.run_bayes_basket_target_station_explicit_18z_oos import (  # noqa: E402
    _evidence_args,
    run as run_station,
)


SCHEMA = "bayes_basket_target_45station_model_pool_oos_v2"
DEFAULT_REGIONAL_POOL = (
    ROOT / "config/bayes_basket_target/station_regional_model_pool_v1.json"
)


def _stations(registry_path: Path) -> list[str]:
    payload = load_bounded_json(registry_path.expanduser().resolve())
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ContractError("model_pool_oos_registry_rows_missing")
    stations = sorted({
        str(row.get("station_id") or "").strip().upper()
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "").strip()
    })
    if len(stations) != 45:
        raise ContractError("model_pool_oos_registry_station_count_invalid")
    return stations


def run(args: argparse.Namespace) -> dict[str, Any]:
    registry = args.registry.expanduser().resolve()
    truth_manifest = args.truth_manifest.expanduser().resolve()
    evidence = _evidence_args(args.model_artifacts)
    models = tuple(sorted(evidence))
    if len(models) < 2:
        raise ContractError("model_pool_oos_minimum_two_models_required")
    output_root = args.output_root.expanduser().resolve()
    regional_pool_path = args.regional_pool.expanduser().resolve()
    regional_pool = load_bounded_json(regional_pool_path)
    if (
        regional_pool.get("schema")
        != "bayes_basket_target_station_regional_model_pool_v1"
    ):
        raise ContractError("model_pool_oos_regional_pool_invalid")
    global_models = {
        str(model).strip().upper()
        for model in regional_pool.get("global_model_pool") or []
    }
    station_regional = regional_pool.get("station_regional_pool")
    if not isinstance(station_regional, Mapping):
        raise ContractError("model_pool_oos_station_regional_pool_missing")
    station_results = []
    for station in _stations(registry):
        output = output_root / station / f"explicit_{args.lead_bucket}_oos.json"
        station_models = global_models | {
            str(model).strip().upper()
            for model in station_regional.get(station, [])
        }
        station_evidence = {
            model: paths
            for model, paths in evidence.items()
            if model in station_models
        }
        try:
            payload = run_station(
                station_id=station,
                registry_path=registry,
                truth_manifest_path=truth_manifest,
                evidence_paths=station_evidence,
                as_of_utc=args.as_of_utc,
                output_path=output,
                run_cycle=args.run_cycle,
                lead_bucket=args.lead_bucket,
            )
            winner = payload.get("oos", {}).get("research_winner") or {}
            formal_gate = payload.get("formal_quality_gate") or {}
            station_results.append({
                "station_id": station,
                "state": (
                    "ACTIVATION_PACKET_READY"
                    if formal_gate.get("passed") is True
                    else "BLOCKED_FORMAL_QUALITY_GATE"
                ),
                "exact_join_row_count": payload["exact_join_row_count"],
                "truth_label_authority": payload["truth_label_authority"],
                "global_candidate_models": sorted(
                    set(payload["model_ids"]) & global_models
                ),
                "regional_candidate_models": sorted(
                    set(payload["model_ids"]) - global_models
                ),
                "selected_models": list(winner.get("selected_models") or []),
                "selected_weights": dict(
                    winner.get("selected_weights") or {}
                ),
                "probability_non_degradation_vs_uniform": (
                    winner.get(
                        "probability_non_degradation_vs_uniform"
                    ) is True
                ),
                "point_non_degradation_vs_uniform": (
                    payload.get("point_oos", {}).get("passed_vs_uniform")
                    is True
                ),
                "formal_quality_gate": dict(formal_gate),
                "output": str(output),
            })
        except (ContractError, OSError, ValueError) as exc:
            station_results.append({
                "station_id": station,
                "state": "BLOCKED_INPUT_OR_JOIN",
                "exact_join_row_count": 0,
                "truth_label_authority": "",
                "selected_models": [],
                "selected_weights": {},
                "probability_non_degradation_vs_uniform": False,
                "point_non_degradation_vs_uniform": False,
                "formal_quality_gate": {
                    "passed": False,
                    "blockers": [str(exc)[:256]],
                    "activation_written": False,
                },
                "output": str(output),
            })
    ready = [
        row for row in station_results
        if row["state"] == "ACTIVATION_PACKET_READY"
    ]
    identity = {
        "schema": SCHEMA,
        "as_of_utc": args.as_of_utc,
        "station_count": len(station_results),
        "run_cycle": args.run_cycle,
        "lead_bucket": args.lead_bucket,
        "model_ids": list(models),
        "inputs": {
            "registry": str(registry),
            "truth_manifest": str(truth_manifest),
            "model_evidence": {
                model: [str(path.expanduser().resolve()) for path in paths]
                for model, paths in evidence.items()
            },
            "regional_pool": str(regional_pool_path),
        },
        "station_results": station_results,
        "checks": {
            "exact_45_station_scope": len(station_results) == 45,
            "all_selected_winners_at_least_two_models": all(
                len(row["selected_models"]) >= 2
                for row in station_results
                if row["selected_models"]
            ),
            "single_model_winner_count": sum(
                len(row["selected_models"]) == 1
                for row in station_results
            ),
            "activation_packet_ready_station_count": len(ready),
            "all_input_or_join_complete": all(
                row["state"] != "BLOCKED_INPUT_OR_JOIN"
                for row in station_results
            ),
            "activation_written": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
        },
        "boundary": {
            "production_no_live_only": True,
            "formal_promotion_written": False,
            "activation_written": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
        },
    }
    result = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(output_root / "manifest.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--truth-manifest", type=Path, required=True)
    parser.add_argument(
        "--model-evidence",
        dest="model_artifacts",
        action="append",
        required=True,
    )
    parser.add_argument(
        "--run-cycle",
        choices=("00Z", "06Z", "12Z", "18Z"),
        required=True,
    )
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument(
        "--lead-bucket",
        choices=("d1", "d2"),
        default="d2",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--regional-pool",
        type=Path,
        default=DEFAULT_REGIONAL_POOL,
    )
    args = parser.parse_args(argv)
    result = run(args)
    print(json.dumps({
        "schema": result["schema"],
        "station_count": result["station_count"],
        "model_ids": result["model_ids"],
        "run_cycle": result["run_cycle"],
        "activation_packet_ready_station_count": result["checks"][
            "activation_packet_ready_station_count"
        ],
        "single_model_winner_count": result["checks"][
            "single_model_winner_count"
        ],
        "output": str(
            (args.output_root / "manifest.json").expanduser().resolve()
        ),
        "writes_production_database": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
