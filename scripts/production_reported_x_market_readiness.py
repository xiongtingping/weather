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
from resolution_bridge_artifact_runtime import (  # noqa: E402
    OPERATIONAL_BASELINE_KERNEL_KIND,
)
from production_probability_v6_market_context_runtime import (  # noqa: E402
    resolve_active_v6_market_context,
)
from resolution_bridge_pair_coverage_runtime import (  # noqa: E402
    build_resolution_bridge_pair_coverage_audit,
)
from scripts.production_probability_v6_readiness import audit as audit_physical_x  # noqa: E402
from posterior_market_projection_runtime import build_posterior_market_projection  # noqa: E402
from production_probability_v6_p0_input_runtime import (  # noqa: E402
    DEFAULT_RESOLUTION_AUTHORITY,
    build_physical_only_resolution_contract,
)


SCHEMA_VERSION = "production_reported_x_market_readiness_v1"
V6_RUNTIME_FILES = {
    "writer": ROOT / "production_probability_v6_market_transaction_writer.py",
    "bundle": ROOT / "production_probability_v6_market_bundle_runtime.py",
    "branch": ROOT / "production_probability_v6_transaction_branch_runtime.py",
    "commit": ROOT / "production_probability_v6_transaction_commit_runtime.py",
}
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


def _latest_reported_x_distribution(db: Path) -> dict[str, float]:
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
        row = conn.execute(
            """SELECT h.fact_json FROM event_fact_latest l
               JOIN event_fact_history h ON h.id=l.fact_id
              WHERE h.composer_version='physical_state_bayes_v6_production'
              ORDER BY h.id DESC LIMIT 1"""
        ).fetchone()
    try:
        fact = json.loads(_text(row[0]) if row else "{}")
    except ValueError:
        fact = {}
    public = fact.get("posterior_ssot_fact") if isinstance(fact, dict) else {}
    raw = public.get("reported_physical_x_distribution") if isinstance(public, dict) else {}
    return {str(key): float(value) for key, value in raw.items()} if isinstance(raw, dict) else {}


def audit(
    *,
    db: Path,
    probability_activation: Path,
    bridge_activation: Path,
    market_context_activation: Path,
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
    runtime_text = {name: path.read_text(encoding="utf-8") for name, path in V6_RUNTIME_FILES.items()}
    writer_text = runtime_text["writer"]
    writer_checks = {
        "atomic_begin_immediate": "BEGIN IMMEDIATE" in writer_text,
        "rollback_on_exception": "conn.rollback()" in writer_text,
        "predecessor_checkpoint_cas": "expected_previous_checkpoint_id" in writer_text and "_assert_bundle" in writer_text,
        "realization_content_hash_guard": "build_realization_state_id" in writer_text and "canonical_state" in writer_text and "state_sha" in writer_text,
        "v6_bundle_bound": "build_v6_market_transaction_bundle" in runtime_text["branch"],
        "v6_branch_physical_fallback": "def _physical_bundle" in runtime_text["branch"] and "physical_only_bundle_ready" in runtime_text["branch"],
        "v6_commit_writer_bound": "commit_v6_market_transaction_b" in runtime_text["commit"],
        "no_network_or_clob_import": all(
            token not in "\n".join(runtime_text.values())
            for token in ("import requests", "import httpx", "clob_order_service", "polymarket_order", "post_order")
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
    context_result = resolve_active_v6_market_context(
        activation_path=market_context_activation,
        root=ROOT,
        event_date=event_date,
    )
    market_context = context_result.get("market_context") if isinstance(context_result.get("market_context"), dict) else {}
    context_activation = context_result.get("activation") if isinstance(context_result.get("activation"), dict) else {}
    context_enabled = context_result.get("market_projection_available") is True and bool(market_context)
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
        receipt.get("schema") == "production_probability_v6_market_no_order_drill_v2"
        and receipt.get("ok") is True
        and receipt.get("state") == "accepted"
        and receipt.get("archive_unchanged") is True
        and receipt.get("writes_archive_db") is False
        and receipt.get("external_disk_required") is False
        and not receipt.get("forbidden_execution_tokens")
        and all(
            _text((receipt.get("runtime_sha256") or {}).get(name)) == _sha(path)
            for name, path in V6_RUNTIME_FILES.items()
        )
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
        and _text(receipt.get("market_context_activation_sha256")) == _text(context_activation.get("activation_sha256"))
        and _text(receipt.get("market_context_sha256")) == _text(market_context.get("market_context_sha256"))
    )
    if bridge_enabled and context_enabled and not activation_drill_bound:
        promotion_blockers.append("successor_market_no_order_drill_not_bound_to_active_bridge")
    coverage = build_resolution_bridge_pair_coverage_audit(db_path=pair_registry)
    operational_baseline = bool(
        bridge_enabled
        and _text((artifact or {}).get("kernel_kind")) == OPERATIONAL_BASELINE_KERNEL_KIND
    )
    if coverage.get("ok") is not True and not operational_baseline:
        promotion_blockers.extend(coverage.get("block_reasons") or [])
    projection_numerics: dict[str, Any] = {
        "ok": False,
        "state": "not_evaluated",
        "market_bucket_probabilities": {},
    }
    if bridge_enabled and context_enabled:
        try:
            reported_x = _latest_reported_x_distribution(db)
            projection = build_posterior_market_projection(
                reported_physical_x_distribution=reported_x,
                physical_resolution_contract=build_physical_only_resolution_contract(
                    DEFAULT_RESOLUTION_AUTHORITY,
                    event_date=event_date,
                ),
                market_target_contract=market_context.get("market_target_contract"),
                bridge_artifact=artifact,
            )
            probabilities = projection.get("market_bucket_probabilities") if isinstance(projection.get("market_bucket_probabilities"), dict) else {}
            total = sum(float(value) for value in probabilities.values())
            minimum = min((float(value) for value in probabilities.values()), default=None)
            projection_numerics = {
                "ok": bool(projection.get("ok") is True and probabilities and abs(total - 1.0) <= 1e-8),
                "state": _text(projection.get("state")),
                "market_bucket_probabilities": probabilities,
                "probability_sum": total,
                "minimum_bucket_probability": minimum,
                "all_buckets_strictly_positive": bool(probabilities and minimum is not None and minimum > 0.0),
                "reported_x_unchanged": projection.get("reported_x_unchanged") is True,
            }
            if projection_numerics["ok"] is not True:
                safety_blockers.append("successor_market_soft_projection_numeric_invalid")
            if operational_baseline and projection_numerics["all_buckets_strictly_positive"] is not True:
                safety_blockers.append("operational_soft_projection_contains_false_exact_zero")
        except (OSError, TypeError, ValueError) as exc:
            projection_numerics = {
                "ok": False,
                "state": "error",
                "error": f"{type(exc).__name__}:{exc}",
                "market_bucket_probabilities": {},
            }
            safety_blockers.append("successor_market_soft_projection_numeric_invalid")
    physical = audit_physical_x(
        ROOT,
        db,
    )
    if physical.get("ok") is not True:
        promotion_blockers.append("physical_x_runtime_not_ready")
    if not bridge_enabled:
        promotion_blockers.append("resolution_bridge_production_activation_disabled")
    if not context_enabled:
        promotion_blockers.append(_text(context_result.get("na_reason")) or "v6_market_context_unavailable")
    production_ready = not safety_blockers and not promotion_blockers and bridge_enabled and context_enabled
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
        "bridge_kernel_kind": _text((artifact or {}).get("kernel_kind")),
        "operational_baseline_active": operational_baseline,
        "market_context_state": _text(context_result.get("state")),
        "market_context_sha256": _text(market_context.get("market_context_sha256")),
        "pair_coverage": coverage,
        "projection_numerics": projection_numerics,
        "pair_coverage_role": (
            "calibrated_successor_readiness_only"
            if operational_baseline
            else "active_artifact_promotion_requirement"
        ),
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
            "operational_baseline_pair_coverage_is_live_gate": False,
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
    parser.add_argument(
        "--market-context-activation",
        type=Path,
        default=ROOT / "config/trading/production_probability_v6_market_context_activation.json",
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
        market_context_activation=args.market_context_activation.expanduser(),
        pair_registry=args.pair_registry.expanduser(),
        no_order_receipt=args.no_order_receipt.expanduser(),
        event_date=args.event_date,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
