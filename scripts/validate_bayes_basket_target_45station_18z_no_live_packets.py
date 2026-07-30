#!/usr/bin/env python3
"""Dry-run a 45-station no-live packet through ingress and exact-mask lookup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.artifact_ingress import ingest_artifact_bundle  # noqa: E402
from bayes_basket_target.calibration import (  # noqa: E402
    resolve_active_calibration_artifact,
    resolve_active_ensemble_artifact,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.production_io import load_production_artifacts  # noqa: E402
from bayes_basket_target.p0_producer import compatible_run_candidates  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402


SCHEMA = "bayes_basket_target_45station_18z_no_live_packet_dry_run_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MODELS = ("GFS", "ICON")


def validate(
    *,
    packet: dict[str, Any],
    build_manifest: dict[str, Any],
    decision_at_utc: str,
) -> dict[str, Any]:
    ready = {
        str(row["station_id"]): row
        for row in build_manifest["station_states"]
        if row["state"] == "ACTIVATION_PACKET_READY"
    }
    all_states = {
        str(row["station_id"]): str(row["state"])
        for row in build_manifest["station_states"]
    }
    if len(all_states) != 45:
        raise ContractError("no_live_dry_run_station_scope_invalid")

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        first = ingest_artifact_bundle(conn, packet, commit=True)
        replay = ingest_artifact_bundle(conn, packet, commit=True)
        ready_results = []
        blocked_results = []
        for station, expected_state in sorted(all_states.items()):
            try:
                bundle = load_production_artifacts(
                    conn,
                    station_id=station,
                    evaluation_at_utc=decision_at_utc,
                    require_calibration=True,
                    require_policy=False,
                )
                if station not in ready:
                    raise ContractError(
                        f"blocked_station_loaded_calibration_artifacts:{station}"
                    )
                ensemble = next(
                    item for item in bundle.ensemble_artifacts.values()
                    if item.station_id == station
                )
                resolved_models = {}
                for model in MODELS:
                    artifact, activation = resolve_active_calibration_artifact(
                        activations=bundle.calibration_activations,
                        artifacts=bundle.calibration_artifacts,
                        station_id=station,
                        model_id=model,
                        season_bin=ensemble.season_bin,
                        lead_bucket="d2",
                        run_cycle="18Z",
                        settlement_definition=SETTLEMENT_DEFINITION,
                        decision_at_utc=decision_at_utc,
                        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                    )
                    resolved_models[model] = {
                        "artifact_id": artifact.calibration_artifact_id,
                        "activation_id": activation.activation_id,
                    }
                resolved_ensemble, ensemble_activation = (
                    resolve_active_ensemble_artifact(
                        activations=bundle.calibration_activations,
                        artifacts=bundle.ensemble_artifacts,
                        station_id=station,
                        season_bin=ensemble.season_bin,
                        lead_bucket="d2",
                        run_cycle="18Z",
                        settlement_definition=SETTLEMENT_DEFINITION,
                        available_models=MODELS,
                        decision_at_utc=decision_at_utc,
                        required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                    )
                )
                p0_candidates = compatible_run_candidates(
                    artifacts=bundle,
                    station=station,
                    event_date="2026-07-29",
                    settlement_timezone=str(
                        ready[station]["settlement_timezone"]
                    ),
                    decision_at_utc=decision_at_utc,
                    run_initializations=["2026-07-27T18:00:00Z"],
                )
                if len(p0_candidates) != 1:
                    raise ContractError(
                        f"p0_exact_18z_d2_preflight_failed:{station}"
                    )
                wrong_mask_failures = []
                for wrong_mask in (("GFS",), ("ECMWF", "GFS", "ICON")):
                    try:
                        resolve_active_ensemble_artifact(
                            activations=bundle.calibration_activations,
                            artifacts=bundle.ensemble_artifacts,
                            station_id=station,
                            season_bin=ensemble.season_bin,
                            lead_bucket="d2",
                            run_cycle="18Z",
                            settlement_definition=SETTLEMENT_DEFINITION,
                            available_models=wrong_mask,
                            decision_at_utc=decision_at_utc,
                            required_authority_scope="PRODUCTION_NO_LIVE_ONLY",
                        )
                    except ContractError as exc:
                        wrong_mask_failures.append(str(exc))
                    else:
                        raise ContractError(
                            f"no_live_wrong_mask_resolved:{station}:{wrong_mask}"
                        )
                ready_results.append({
                    "station_id": station,
                    "resolved_models": resolved_models,
                    "ensemble_artifact_id": resolved_ensemble.ensemble_artifact_id,
                    "ensemble_activation_id": ensemble_activation.activation_id,
                    "resolved_weights": dict(
                        resolved_ensemble.resolve_weights(MODELS)
                    ),
                    "p0_preflight": {
                        "run_initialization_utc":
                            p0_candidates[0]["run_initialization_utc"],
                        "lead_bucket": p0_candidates[0]["lead_bucket"],
                        "run_cycle": p0_candidates[0]["run_cycle"],
                        "selected_models": p0_candidates[0][
                            "activation_binding"
                        ]["selected_models"],
                    },
                    "wrong_mask_failures": wrong_mask_failures,
                })
            except ContractError as exc:
                if station in ready:
                    raise
                blocked_results.append({
                    "station_id": station,
                    "expected_state": expected_state,
                    "fail_closed_reason": str(exc),
                })

        manifest_counts = build_manifest.get("packet_counts")
        if not isinstance(manifest_counts, Mapping):
            raise ContractError("packet_build_manifest_counts_missing")
        expected_ingress_counts = {
            "calibration_artifacts": int(
                manifest_counts.get("calibration_artifacts") or 0
            ),
            "ensemble_selection_evidence": len(ready),
            "ensemble_artifacts": int(
                manifest_counts.get("ensemble_artifacts") or 0
            ),
            "calibration_activations": int(
                manifest_counts.get("calibration_activations") or 0
            ),
            "global_priors": 0,
            "policy_artifacts": 0,
            "policy_activations": 0,
            "cash_phase_policies": 0,
            "cash_phase_authority_manifests": 0,
            "calibration_promotions": 0,
            "policy_promotions": 0,
        }
        receipt = {
            "schema": SCHEMA,
            "decision_at_utc": decision_at_utc,
            "ingress_counts": dict(first["counts"]),
            "idempotent_replay_counts": dict(replay["counts"]),
            "ready_station_count": len(ready_results),
            "blocked_station_count": len(blocked_results),
            "ready_results": ready_results,
            "blocked_results": blocked_results,
            "checks": {
                "ingress_exact_counts": (
                    first["counts"] == expected_ingress_counts
                ),
                "ingress_replay_zero_write": all(
                    int(value) == 0 for value in replay["counts"].values()
                ),
                "all_ready_exact_mask_resolved": len(ready_results) == len(ready),
                "all_ready_p0_preflight_resolved": all(
                    row["p0_preflight"]["lead_bucket"] == "d2"
                    and row["p0_preflight"]["run_cycle"] == "18Z"
                    and tuple(row["p0_preflight"]["selected_models"]) == MODELS
                    for row in ready_results
                ),
                "all_other_stations_fail_closed":
                    len(blocked_results) == 45 - len(ready),
                "wrong_masks_fail_closed": all(
                    len(row["wrong_mask_failures"]) == 2
                    for row in ready_results
                ),
                "formal_promotion_created": False,
                "live_order_authorized": False,
                "calls_order_gateway": False,
            },
            "boundary": {
                "temporary_in_memory_database_only": True,
                "writes_production_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
            },
        }
    receipt["content_sha256"] = content_sha256(receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--decision-at-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = validate(
        packet=load_bounded_json(args.packet.expanduser().resolve()),
        build_manifest=load_bounded_json(
            args.build_manifest.expanduser().resolve()
        ),
        decision_at_utc=args.decision_at_utc,
    )
    write_bounded_json(args.output.expanduser().resolve(), receipt)
    print(json.dumps({
        "schema": receipt["schema"],
        "ready_station_count": receipt["ready_station_count"],
        "blocked_station_count": receipt["blocked_station_count"],
        "checks": receipt["checks"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
