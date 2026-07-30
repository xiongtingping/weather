#!/usr/bin/env python3
"""Materialize the complete target-v2 station-policy authority for no-live."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.artifact_ingress import (  # noqa: E402
    SCHEMA as INGRESS_SCHEMA,
    ingest_artifact_bundle,
)
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.policy import (  # noqa: E402
    GlobalBasketPriorArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)
from bayes_basket_target.production_io import (  # noqa: E402
    load_production_artifacts,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)


MANIFEST_SCHEMA = (
    "bayes_basket_target_target_v2_station_policy_no_live_manifest_v1"
)
RECEIPT_SCHEMA = (
    "bayes_basket_target_target_v2_station_policy_no_live_receipt_v1"
)
SERVICE_LABEL = "system/com.weather.reported_trajectory_no"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_MANIFEST = (
    ROOT
    / "config"
    / "trading"
    / "bayes_basket_target_target_v2_station_policy_no_live_v3.json"
)


def _service_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", SERVICE_LABEL],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "state = running" in result.stdout


def load_manifest(path: Path) -> dict[str, Any]:
    raw = load_bounded_json(path.expanduser().resolve(), limit=64 * 1024)
    if not isinstance(raw, Mapping) or raw.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("target_v2_station_policy_manifest_invalid")
    value = dict(raw)
    generation = value.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ValueError("target_v2_station_policy_generation_invalid")
    expected_stages = ["P0", "P1"]
    expected_budget = 20.0 if generation >= 3 else 12.0
    if (
        value.get("station_id") != "RKPK"
        or value.get("strategy_version") != "target-v2"
        or value.get("stages") != expected_stages
        or value.get("maturity_bins") != ["d1", "d2", "d2_fallback"]
        or value.get("policy_authority_scope")
        != "PRODUCTION_NO_LIVE_CANDIDATE"
        or value.get("execution_mode") != "production_no_live"
        or value.get("production_no_live") is not True
        or value.get("live_submit_authorized") is not False
        or value.get("calls_order_gateway") is not False
        or value.get("places_orders") is not False
        or int(value.get("orders_submitted", -1)) != 0
    ):
        raise ValueError("target_v2_station_policy_boundary_invalid")
    families = value.get("family_parameters")
    if (
        not isinstance(families, Mapping)
        or set(families) != {"target", "reserve", "cadence"}
        or families.get("target") != {}
        or families.get("reserve") != {}
        or families.get("cadence") != {"gate_seconds_intercept": 1.0}
    ):
        raise ValueError("target_v2_station_policy_family_invalid")
    default = value.get("default_policy")
    if (
        not isinstance(default, Mapping)
        or float(default.get("total_budget_usdc", 0.0)) != expected_budget
        or not (
            0.0
            < float(default.get("clip_min_notional_usdc", 0.0))
            <= float(default.get("clip_max_notional_usdc", 0.0))
        )
        or float(default.get("clip_max_notional_usdc", 0.0)) != 2.0
    ):
        raise ValueError("target_v2_station_policy_default_invalid")
    return value


def build_packet(manifest: Mapping[str, Any]) -> dict[str, Any]:
    prior = GlobalBasketPriorArtifact.create(
        global_prior_id=manifest["global_prior_id"],
        strategy_version=manifest["strategy_version"],
        family_parameters=manifest["family_parameters"],
        default_policy=manifest["default_policy"],
        valid_from_utc=manifest["valid_from_utc"],
        valid_until_utc=manifest["valid_until_utc"],
        promoted=False,
    )
    activations = [
        StationBasketPolicyActivation.create(
            activation_id=(
                "target-v2-rkpk-no-live-"
                f"{stage.lower()}-{maturity}-"
                f"g{int(manifest['generation'])}"
            ),
            station_id=manifest["station_id"],
            stage=stage,
            season_bin=manifest["season_bin"],
            maturity_bin=maturity,
            liquidity_bin=manifest["liquidity_bin"],
            strategy_version=manifest["strategy_version"],
            global_prior_id=prior.global_prior_id,
            target_artifact_id=None,
            reserve_artifact_id=None,
            cadence_artifact_id=None,
            fallback_families=["target", "reserve", "cadence"],
            generation=int(manifest["generation"]),
            valid_from_utc=manifest["valid_from_utc"],
            valid_until_utc=manifest["valid_until_utc"],
            activated_at_utc=manifest["activated_at_utc"],
            promoted=False,
        )
        for stage in manifest["stages"]
        for maturity in manifest["maturity_bins"]
    ]
    return {
        "schema": INGRESS_SCHEMA,
        "policy_authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
        "execution_mode": "production_no_live",
        "calibration_artifacts": [],
        "ensemble_selection_evidence": [],
        "ensemble_artifacts": [],
        "calibration_activations": [],
        "global_priors": [prior.to_dict()],
        "policy_artifacts": [],
        "policy_activations": [item.to_dict() for item in activations],
        "cash_phase_policies": [],
        "cash_phase_authority_manifests": [],
        "calibration_promotions": [],
        "policy_promotions": [],
        "live_order_authorized": False,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def _resolve_all_scopes(
    *,
    manifest: Mapping[str, Any],
    activations: Any,
    global_priors: Any,
    artifacts: Any,
) -> list[dict[str, str]]:
    valid_from = datetime.fromisoformat(
        str(manifest["valid_from_utc"]).replace("Z", "+00:00")
    )
    activated_at = datetime.fromisoformat(
        str(manifest["activated_at_utc"]).replace("Z", "+00:00")
    )
    decision_at = (
        max(valid_from, activated_at) + timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")
    resolved: list[dict[str, str]] = []
    for stage in manifest["stages"]:
        for maturity in manifest["maturity_bins"]:
            policy = resolve_station_policy(
                activations=activations,
                global_priors=global_priors,
                artifacts=artifacts,
                station_id="RKPK",
                stage=stage,
                season_bin="summer",
                maturity_bin=maturity,
                liquidity_bin="liquid",
                strategy_version="target-v2",
                decision_at_utc=decision_at,
                required_authority_scope=(
                    "PRODUCTION_NO_LIVE_CANDIDATE"
                ),
            )
            resolved.append({
                "stage": stage,
                "maturity_bin": maturity,
                "activation_id": policy.activation_id,
                "global_prior_id": policy.global_prior_id,
                "policy_sha256": policy.policy_sha256,
            })
    return resolved


def apply(
    *,
    database: Path,
    manifest_path: Path,
    commit: bool,
) -> dict[str, Any]:
    db = resolve_target_database(database.expanduser().resolve())
    if commit and db == DEFAULT_DB.resolve() and _service_running():
        raise RuntimeError("target_v2_station_policy_writer_must_be_stopped")
    manifest = load_manifest(manifest_path)
    packet = build_packet(manifest)
    if not commit:
        resolved = _resolve_all_scopes(
            manifest=manifest,
            activations=packet["policy_activations"],
            global_priors={
                row["global_prior_id"]: row
                for row in packet["global_priors"]
            },
            artifacts={},
        )
        return {
            "schema": RECEIPT_SCHEMA,
            "state": "VALIDATED_NO_WRITE",
            "database": str(db),
            "counts": {
                "global_priors": len(packet["global_priors"]),
                "policy_activations": len(
                    packet["policy_activations"]
                ),
            },
            "resolved_scopes": resolved,
            "resolved_scope_count": len(resolved),
            "strategy_version": "target-v2",
            "execution_mode": "production_no_live",
            "live_submit_authorized": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        }
    with sqlite3.connect(db, timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("target_v2_station_policy_quick_check_failed")
        counts = ingest_artifact_bundle(
            conn,
            packet,
            commit=commit,
        )
        bundle = load_production_artifacts(
            conn,
            station_id="RKPK",
            evaluation_at_utc="2026-07-28T23:32:23Z",
            require_calibration=False,
            require_policy=False,
        )
        resolved = _resolve_all_scopes(
            manifest=manifest,
            activations=bundle.policy_activations,
            global_priors=bundle.global_priors,
            artifacts=bundle.policy_artifacts,
        )
    write_counts = counts.get("counts") if isinstance(counts, Mapping) else None
    rows_written = (
        sum(int(value) for value in write_counts.values())
        if isinstance(write_counts, Mapping)
        else 0
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "state": "COMMITTED" if rows_written else "UNCHANGED_ZERO_WRITE",
        "database": str(db),
        "counts": counts,
        "rows_written": rows_written,
        "resolved_scopes": resolved,
        "resolved_scope_count": len(resolved),
        "strategy_version": "target-v2",
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-reported-trajectory-no-stopped")
    args = parser.parse_args()
    if args.apply and (
        args.confirm_reported_trajectory_no_stopped
        != "TARGET_V2_STATION_POLICY_NO_LIVE"
    ):
        raise SystemExit(
            "apply requires --confirm-reported-trajectory-no-stopped "
            "TARGET_V2_STATION_POLICY_NO_LIVE"
        )
    result = apply(
        database=args.db,
        manifest_path=args.manifest,
        commit=args.apply,
    )
    if args.receipt is not None:
        write_bounded_json(args.receipt.expanduser().resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
