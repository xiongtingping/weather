from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.contracts import canonical_json, content_sha256
from bayes_basket_target.database import TARGET_ACTIVATION_DDL
from bayes_basket_target.execution_version_vector import (
    build_no_execution_version_policy,
)
from bayes_basket_target.ssot import apply_production_ssot_schema, apply_target_ssot_schema
from scripts.bayes_basket_target_m0_closure_audit import build_audit


def _acceptance(path: Path) -> Path:
    path.write_text(json.dumps({
        "schema": "bayes_basket_target_production_no_live_acceptance_v1",
        "status": "PASS",
        "orders_submitted": 0,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "live_order_authorized": False,
    }), encoding="utf-8")
    return path


def _db(
    path: Path,
    activation_path: Path,
    *,
    blocked: bool = True,
    include_vector: bool = True,
) -> Path:
    activation = load_target_activation(activation_path)
    with sqlite3.connect(path) as conn:
        apply_target_ssot_schema(conn)
        apply_production_ssot_schema(conn)
        conn.executescript(TARGET_ACTIVATION_DDL)
        runtime = {**activation, "generation": 4, "activated_at": "2026-07-26T00:00:00Z"}
        conn.execute(
            "INSERT INTO basket_strategy_activation_state("
            "singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at"
            ") VALUES(1,4,'enabled',?,?,?,?)",
            (
                activation["strategy_id"],
                activation["activation_contract_sha256"],
                json.dumps(runtime, sort_keys=True),
                "2026-07-26T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_capital_pool("
            "pool_id,balance_usdc,reserved_usdc,capital_epoch_id,version,updated_at"
            ") VALUES(1,0,0,'epoch-zero',1,'2026-07-26T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO basket_target_execution_cycles("
            "cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,plan_sha256,"
            "requested_reserve_usdc,state,created_at,updated_at"
            ") VALUES('cycle-1','plan-1','EDDM','2026-07-26','Europe/Berlin','basket',?,0,'prepared',?,?)",
            ("a" * 64, "2026-07-26T00:00:00Z", "2026-07-26T00:00:00Z"),
        )
        if blocked:
            reasons = ["no_positive_increment"]
            decision_identity = {
                "cycle_id": "cycle-1",
                "plan_id": "plan-1",
                "station_id": "EDDM",
                "event_date": "2026-07-26",
                "settlement_timezone": "Europe/Berlin",
                "strategy_lane": "basket",
                "decided_state": "blocked",
                "will_order": 0,
                "reason_codes": reasons,
                "details": {},
            }
            decision_sha = content_sha256(
                {
                    "schema": "basket_target_decision_fact_v1",
                    **decision_identity,
                }
            )
            conn.execute(
                "INSERT INTO auto_trade_decisions("
                "decision_id,cycle_id,plan_id,station_id,event_date,settlement_timezone,strategy_lane,"
                "decided_state,will_order,reason_codes_json,details_json,"
                "decision_sha256,created_at"
                ") VALUES('decision-1','cycle-1','plan-1','EDDM','2026-07-26','Europe/Berlin',"
                "'basket','blocked',0,?,'{}',?,'2026-07-26T00:00:00Z')",
                (canonical_json(reasons), decision_sha),
            )
            if include_vector:
                policy = build_no_execution_version_policy(
                    {"policy": "NO_EXECUTION", "reason_codes": reasons}
                )
                conn.execute(
                    "INSERT INTO basket_target_execution_version_vectors("
                    "order_intent_id,cycle_id,decision_id,training_fact_id,"
                    "vector_policy,target_price_id,quote_observed_at,"
                    "quote_expires_at,vector_json,vector_sha256,created_at"
                    ") VALUES(NULL,'cycle-1','decision-1',NULL,'NO_EXECUTION',"
                    "NULL,NULL,NULL,?,?, '2026-07-26T00:00:00Z')",
                    (canonical_json(policy.to_dict()), policy.policy_sha256),
                )
        conn.commit()
    return path


def test_blocked_prepared_cycle_is_retained_audit_fact_not_unresolved(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    activation = root / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    result = build_audit(
        db_path=_db(tmp_path / "target.db", activation),
        activation_path=activation,
        acceptance_paths=[
            _acceptance(tmp_path / "acceptance-a.json"),
            _acceptance(tmp_path / "acceptance-b.json"),
        ],
    )

    assert result["status"] == "PASS"
    assert result["cycle_reconciliation"]["actionable_unresolved_count"] == 0
    assert result["cycle_reconciliation"]["retained_blocked_cycle_count"] == 1
    assert result["boundary"]["deletes_retained_cycles"] is False
    assert result["boundary"]["reads_shared_live_database"] is False


def test_prepared_cycle_without_blocking_decision_remains_actionable(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    activation = root / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    result = build_audit(
        db_path=_db(tmp_path / "target.db", activation, blocked=False),
        activation_path=activation,
        acceptance_paths=[
            _acceptance(tmp_path / "acceptance-a.json"),
            _acceptance(tmp_path / "acceptance-b.json"),
        ],
    )

    assert result["status"] == "BLOCKED"
    assert result["cycle_reconciliation"]["actionable_unresolved_count"] == 1
    assert result["checks"]["cycle_reconciliation_closed"] is False


def test_two_distinct_repeatable_acceptance_receipts_are_required(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    activation = root / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    one = _acceptance(tmp_path / "acceptance.json")
    result = build_audit(
        db_path=_db(tmp_path / "target.db", activation),
        activation_path=activation,
        acceptance_paths=[one],
    )

    assert result["status"] == "BLOCKED"
    assert result["acceptance"]["required_count"] == 2
    assert result["checks"]["two_consecutive_acceptances_pass"] is False
