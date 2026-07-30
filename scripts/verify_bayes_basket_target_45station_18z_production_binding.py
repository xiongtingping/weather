#!/usr/bin/env python3
"""Verify committed 18Z/d2 no-live bindings without writing target SSOT."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration import (  # noqa: E402
    resolve_active_calibration_artifact,
    resolve_active_ensemble_artifact,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.production_io import load_production_artifacts  # noqa: E402
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.station_registry import station_coordinates  # noqa: E402
from bayes_basket_target.strict_asof_replay import (  # noqa: E402
    season_bin_for_event_date,
)


SCHEMA = "bayes_basket_target_45station_18z_production_binding_v1"
MODELS = ("GFS", "ICON")
SETTLEMENT_DEFINITION = "official_daily_tmax_c"


def verify(
    *,
    db_path: Path,
    manifest: dict[str, Any],
    event_date: str,
    evaluation_at_utc: str,
    required_generation: int,
) -> dict[str, Any]:
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    database = resolve_target_database(db_path)
    with sqlite3.connect(
        f"file:{database}?mode=ro", uri=True, timeout=60.0
    ) as conn:
        conn.row_factory = sqlite3.Row
        if str(conn.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
            raise ContractError("production_binding_database_quick_check_failed")
        activation_row = conn.execute(
            "SELECT generation,state,strategy_id,activation_json "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
        if activation_row is None:
            raise ContractError("production_binding_strategy_activation_missing")
        activation = json.loads(str(activation_row["activation_json"]))
        if (
            activation.get("execution_mode") != "production_no_live"
            or activation.get("authorizes_order") is not False
            or activation.get("live_submit_authorized") is not False
            or activation.get("calls_order_gateway") is not False
        ):
            raise ContractError("production_binding_live_boundary_invalid")

        for row in manifest["station_states"]:
            station = str(row["station_id"])
            season = season_bin_for_event_date(
                event_date,
                latitude=float(station_coordinates(station)["latitude"]),
            )
            try:
                bundle = load_production_artifacts(
                    conn,
                    station_id=station,
                    evaluation_at_utc=evaluation_at_utc,
                    require_calibration=True,
                    require_policy=False,
                )
                model_bindings = {}
                for model in MODELS:
                    artifact, model_activation = (
                        resolve_active_calibration_artifact(
                            activations=bundle.calibration_activations,
                            artifacts=bundle.calibration_artifacts,
                            station_id=station,
                            model_id=model,
                            season_bin=season,
                            lead_bucket="d2",
                            run_cycle="18Z",
                            settlement_definition=SETTLEMENT_DEFINITION,
                            decision_at_utc=evaluation_at_utc,
                            required_authority_scope=
                                "PRODUCTION_NO_LIVE_ONLY",
                        )
                    )
                    if model_activation.generation != required_generation:
                        raise ContractError(
                            "production_binding_model_generation_mismatch"
                        )
                    model_bindings[model] = {
                        "artifact_id": artifact.calibration_artifact_id,
                        "activation_id": model_activation.activation_id,
                    }
                ensemble, ensemble_activation = (
                    resolve_active_ensemble_artifact(
                        activations=bundle.calibration_activations,
                        artifacts=bundle.ensemble_artifacts,
                        station_id=station,
                        season_bin=season,
                        lead_bucket="d2",
                        run_cycle="18Z",
                        settlement_definition=SETTLEMENT_DEFINITION,
                        available_models=MODELS,
                        decision_at_utc=evaluation_at_utc,
                        required_authority_scope=
                            "PRODUCTION_NO_LIVE_ONLY",
                    )
                )
                if ensemble_activation.generation != required_generation:
                    raise ContractError(
                        "production_binding_ensemble_generation_mismatch"
                    )
                if row["state"] != "ACTIVATION_PACKET_READY":
                    raise ContractError(
                        "production_binding_blocked_station_resolved"
                    )
                ready.append({
                    "station_id": station,
                    "season_bin": season,
                    "model_bindings": model_bindings,
                    "ensemble_artifact_id": ensemble.ensemble_artifact_id,
                    "ensemble_activation_id":
                        ensemble_activation.activation_id,
                    "generation": ensemble_activation.generation,
                    "weights": dict(ensemble.resolve_weights(MODELS)),
                })
            except ContractError as exc:
                if row["state"] == "ACTIVATION_PACKET_READY":
                    raise
                blocked.append({
                    "station_id": station,
                    "expected_state": row["state"],
                    "fail_closed_reason": str(exc),
                })

        probability_counts = {
            str(row["stage"]): int(row["n"])
            for row in conn.execute(
                "SELECT stage,COUNT(*) n FROM basket_probability_envelope "
                "WHERE event_date=? GROUP BY stage",
                (event_date,),
            ).fetchall()
        }
        execution_counts = {
            table: int(conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0])
            for table in (
                "basket_target_order_events",
                "basket_target_execution_receipts",
            )
        }
        authorizing_decisions = int(conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions "
            "WHERE will_order<>0 OR decided_state IN "
            "('submitting','submitted','reconcile_required')"
        ).fetchone()[0])

    result = {
        "schema": SCHEMA,
        "database": str(database),
        "event_date": event_date,
        "evaluation_at_utc": evaluation_at_utc,
        "strategy_activation": {
            "generation": int(activation_row["generation"]),
            "state": str(activation_row["state"]),
            "strategy_id": str(activation_row["strategy_id"]),
            "execution_mode": activation["execution_mode"],
            "authorizes_order": False,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
        },
        "ready_station_count": len(ready),
        "blocked_station_count": len(blocked),
        "ready_bindings": ready,
        "blocked_bindings": blocked,
        "probability_envelope_counts": probability_counts,
        "execution_counts": execution_counts,
        "authorizing_decision_count": authorizing_decisions,
        "checks": {
            "database_quick_check": True,
            "exact_11_generation_bound": len(ready) == 11,
            "other_34_fail_closed": len(blocked) == 34,
            "p1_final_authority_present":
                int(probability_counts.get("P1", 0)) > 0,
            "no_authorizing_decisions": authorizing_decisions == 0,
            "no_order_events": execution_counts[
                "basket_target_order_events"
            ] == 0,
            "live_submit_authorized": False,
            "calls_order_gateway": False,
        },
        "boundary": {
            "read_only_database": True,
            "writes_production_database": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }
    result["content_sha256"] = content_sha256(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--evaluation-at-utc", required=True)
    parser.add_argument("--required-generation", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(
        db_path=args.db,
        manifest=load_bounded_json(args.manifest.expanduser().resolve()),
        event_date=args.event_date,
        evaluation_at_utc=args.evaluation_at_utc,
        required_generation=args.required_generation,
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    print(json.dumps({
        "schema": result["schema"],
        "ready_station_count": result["ready_station_count"],
        "blocked_station_count": result["blocked_station_count"],
        "checks": result["checks"],
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
