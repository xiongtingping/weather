#!/usr/bin/env python3
"""Read-only readiness for X posterior -> approved bridge -> market B."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
import hashlib


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_bridge_activation_runtime import (  # noqa: E402
    load_active_resolution_bridge_artifact,
)
from resolution_bridge_pair_coverage_runtime import (  # noqa: E402
    build_resolution_bridge_pair_coverage_audit,
)
from scripts.production_reported_x_runtime_readiness import audit as audit_physical_x  # noqa: E402


SCHEMA_VERSION = "production_reported_x_market_readiness_v1"
REQUIRED_TABLES = (
    "strong_observation_updates",
    "posterior_checkpoints",
    "realization_states",
    "posterior_commit_outbox",
    "posterior_outbox_execution_contracts",
    "event_fact_history",
    "event_fact_latest",
    "production_initial_posterior_lineage",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(
    *,
    db: Path,
    probability_activation: Path,
    bridge_activation: Path,
    pair_registry: Path,
    no_order_receipt: Path | None,
    event_date: str,
) -> dict[str, Any]:
    safety_blockers: list[str] = []
    promotion_blockers: list[str] = []
    table_state: dict[str, bool] = {}
    try:
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            actual = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        table_state = {table: table in actual for table in REQUIRED_TABLES}
    except sqlite3.Error as exc:
        safety_blockers.append(f"archive_db_unreadable:{type(exc).__name__}")
        table_state = {table: False for table in REQUIRED_TABLES}
    if not all(table_state.values()):
        safety_blockers.append("successor_market_transaction_schema_incomplete")
    writer_text = (ROOT / "production_reported_x_market_transaction_writer.py").read_text(encoding="utf-8")
    writer_checks = {
        "atomic_begin_immediate": "BEGIN IMMEDIATE" in writer_text,
        "rollback_on_exception": "conn.rollback()" in writer_text,
        "predecessor_posterior_sha_cas": "previous_posterior_sha256_cas_failed" in writer_text,
        "frozen_epoch_context_guard": "frozen_epoch_context_drift" in writer_text,
        "realization_content_hash_guard": "build_realization_state_id" in writer_text and "_sha(state_payload)" in writer_text,
        "direct_report_bucket_retired": "current_report_bucket" in writer_text,
        "no_network_or_clob_import": all(
            token not in writer_text
            for token in ("import requests", "import httpx", "clob_order_service", "polymarket_order")
        ),
    }
    if not all(writer_checks.values()):
        safety_blockers.append("successor_market_transaction_boundary_incomplete")
    try:
        activation, artifact = load_active_resolution_bridge_artifact(
            activation_path=bridge_activation,
            root=ROOT,
        )
    except ValueError as exc:
        activation, artifact = {}, None
        safety_blockers.append(f"resolution_bridge_activation_invalid:{exc}")
    activation_state = _text(activation.get("state"))
    bridge_enabled = activation_state == "production_enabled" and artifact is not None
    receipt: dict[str, Any] = {}
    receipt_path = no_order_receipt.expanduser().resolve() if no_order_receipt is not None else None
    if receipt_path is not None and receipt_path.is_file():
        try:
            loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt = dict(loaded) if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            receipt = {}
    unsigned_receipt = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    computed_receipt_sha = hashlib.sha256(
        json.dumps(unsigned_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    code_drill_accepted = bool(
        receipt.get("schema") == "production_reported_x_market_no_order_drill_v1"
        and receipt.get("ok") is True
        and receipt.get("state") == "accepted"
        and receipt.get("backup_unchanged") is True
        and not receipt.get("forbidden_execution_tokens")
        and _text(receipt.get("writer_sha256"))
        == _sha(ROOT / "production_reported_x_market_transaction_writer.py")
        and _text(receipt.get("generator_sha256"))
        == _sha(ROOT / "scripts/production_reported_x_market_no_order_drill.py")
        and _text(receipt.get("receipt_sha256")) == computed_receipt_sha
    )
    if not code_drill_accepted:
        safety_blockers.append("successor_market_no_order_code_drill_missing_or_stale")
    activation_drill_bound = bool(
        code_drill_accepted
        and receipt.get("activation_bound") is True
        and _text(receipt.get("bridge_activation_sha256")) == _text(activation.get("activation_sha256"))
        and _text(receipt.get("bridge_artifact_sha256")) == _text((artifact or {}).get("artifact_sha256"))
    )
    if bridge_enabled and not activation_drill_bound:
        promotion_blockers.append("successor_market_no_order_drill_not_bound_to_active_bridge")
    coverage = build_resolution_bridge_pair_coverage_audit(db_path=pair_registry)
    if coverage.get("ok") is not True:
        promotion_blockers.extend(coverage.get("block_reasons") or [])
    physical = audit_physical_x(
        db=db,
        activation_path=probability_activation,
        event_date=_text(event_date)[:10],
    )
    if physical.get("ok") is not True:
        promotion_blockers.append("physical_x_runtime_not_ready")
    if not bridge_enabled:
        promotion_blockers.append("resolution_bridge_production_activation_disabled")
    production_ready = not safety_blockers and not promotion_blockers and bridge_enabled
    if safety_blockers:
        state = "blocked"
    elif production_ready:
        state = "production_ready"
    else:
        state = "safe_fail_closed_accumulating"
    return {
        "schema": SCHEMA_VERSION,
        "ok": not safety_blockers,
        "state": state,
        "production_market_projection_ready": production_ready,
        "safety_blockers": sorted(set(safety_blockers)),
        "promotion_blockers": sorted(set(promotion_blockers)),
        "bridge_activation_state": activation_state,
        "bridge_artifact_sha256": _text((artifact or {}).get("artifact_sha256")),
        "pair_coverage": coverage,
        "physical_x_runtime": physical,
        "required_table_state": table_state,
        "writer_checks": writer_checks,
        "no_order_drill": {
            "receipt_path": str(receipt_path) if receipt_path is not None else "",
            "code_drill_accepted": code_drill_accepted,
            "active_bridge_bound": activation_drill_bound,
            "receipt_sha256": _text(receipt.get("receipt_sha256")),
        },
        "boundary": {
            "bridge_sample_or_oos_shortage_blocks_physical_x_live": False,
            "amsc_ingress_blocks_on_bridge": False,
            "physical_x_ssot_continues_when_market_b_na": True,
            "market_projection_and_outbox_fail_closed_until_bridge_approved": True,
            "strict_asof_or_oos_is_live_trade_gate": False,
            "places_orders": False,
        },
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument(
        "--probability-activation",
        type=Path,
        default=ROOT / "config/production_probability_runtime_activation.json",
    )
    parser.add_argument(
        "--bridge-activation",
        type=Path,
        default=ROOT / "config/resolution_bridge_production_activation.json",
    )
    parser.add_argument(
        "--pair-registry",
        type=Path,
        default=ROOT / "data/shadow/resolution_final_labels.db",
    )
    parser.add_argument("--event-date", required=True)
    parser.add_argument(
        "--no-order-receipt",
        type=Path,
        default=ROOT / "data/logs/production_reported_x_market_no_order_drill.json",
    )
    args = parser.parse_args()
    result = audit(
        db=args.db.expanduser(),
        probability_activation=args.probability_activation.expanduser(),
        bridge_activation=args.bridge_activation.expanduser(),
        pair_registry=args.pair_registry.expanduser(),
        no_order_receipt=args.no_order_receipt.expanduser(),
        event_date=args.event_date,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
