"""PR14 45-station unresolved policy, utility, and pure-YES basket basis."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .canonical_expected_scope_manifest import build_scope_basis
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .station_policy_forward_outcome_binding import (
    build_station_policy_utility_contract_pointer,
)
from .station_registry import coordinate_registry_manifest


SCHEMA = "bayes_basket_target_pr14_strategy_terminal_bundle_v1"
TERMINAL_BASIS_SCHEMA = "bayes_basket_target_pr14_strategy_station_terminal_basis_v1"
TERMINAL_ROW_SCHEMA = "bayes_basket_target_pr14_strategy_station_terminal_v1"
BUILDER_ID = "pr14.canonical_strategy_terminal_basis.v1"
EXPECTED_STATION_COUNT = 45


class PR14StrategyBasisError(ContractError):
    """PR14 source evidence cannot be represented without guessing."""


def _utc_z(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PR14StrategyBasisError(f"pr14_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PR14StrategyBasisError(f"pr14_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _latest_decisions(
    conn: sqlite3.Connection, *, cutoff: str
) -> dict[str, dict[str, Any]]:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_trade_decisions'"
    ).fetchone()
    if table is None:
        raise PR14StrategyBasisError("pr14_auto_trade_decisions_missing")
    rows = conn.execute(
        "WITH ranked AS ("
        "SELECT decision_id,station_id,decided_state,will_order,reason_codes_json,"
        "decision_sha256,created_at,ROW_NUMBER() OVER (PARTITION BY station_id "
        "ORDER BY created_at DESC,decision_id DESC) AS rn "
        "FROM auto_trade_decisions WHERE created_at<=?) "
        "SELECT decision_id,station_id,decided_state,will_order,reason_codes_json,"
        "decision_sha256,created_at FROM ranked WHERE rn=1 ORDER BY station_id",
        (cutoff,),
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for decision_id, station_id, state, will_order, reasons_json, sha, created in rows:
        station = str(station_id or "").strip().upper()
        try:
            reasons = json.loads(str(reasons_json or "[]"))
        except json.JSONDecodeError as exc:
            raise PR14StrategyBasisError("pr14_decision_reasons_invalid") from exc
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise PR14StrategyBasisError("pr14_decision_reasons_invalid")
        if station in result:
            raise PR14StrategyBasisError("pr14_latest_decision_duplicate")
        if str(state).lower() != "blocked" or int(will_order) != 0:
            raise PR14StrategyBasisError("pr14_latest_decision_not_fail_closed")
        result[station] = {
            "decision_id": str(decision_id),
            "decision_sha256": str(sha),
            "created_at_utc": _utc_z(created, "decision_created_at"),
            "decided_state": "blocked",
            "will_order": False,
            "reason_codes": sorted(set(reasons)),
        }
    return result


def build_pr14_strategy_artifacts(
    db_path: str | Path, *, as_of_utc: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    cutoff = _utc_z(as_of_utc, "as_of_utc")
    source = Path(db_path).expanduser().resolve(strict=True)
    registry = coordinate_registry_manifest()
    station_ids = list(registry["station_ids"])
    if len(station_ids) != EXPECTED_STATION_COUNT or station_ids != sorted(set(station_ids)):
        raise PR14StrategyBasisError("pr14_registry_not_exact_45")

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        inspection = inspect_canonical_ssot_schema(conn)
        schema_state = str(inspection.get("state") or "")
        if schema_state not in {"V12", "V13", "V14"} or inspection.get("ok") is not True:
            raise PR14StrategyBasisError("pr14_canonical_v12_required")
        fact_counts = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact "
                "WHERE ingested_at_utc<=? GROUP BY fact_type ORDER BY fact_type",
                (cutoff,),
            )
        }
        decisions = _latest_decisions(conn, cutoff=cutoff)
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise PR14StrategyBasisError("pr14_read_failed") from exc
    finally:
        conn.close()
    if set(decisions) != set(station_ids):
        raise PR14StrategyBasisError("pr14_latest_decision_set_not_exact_45")
    forbidden_canonical = {
        "strategy_policy", "strategy_utility", "decision", "intent",
        "capital_reservation", "ws_snapshot",
    }
    if forbidden_canonical & set(fact_counts):
        raise PR14StrategyBasisError("pr14_unrecognized_canonical_fact_type_present")

    utility_pointer = build_station_policy_utility_contract_pointer()
    inventory_sha = content_sha256(
        {
            "canonical_fact_type_counts": fact_counts,
            "latest_decisions": decisions,
            "utility_contract_pointer_sha256": utility_pointer["pointer_sha256"],
        }
    )
    rows: list[dict[str, Any]] = []
    for station_id in station_ids:
        identity = {
            "schema": TERMINAL_ROW_SCHEMA,
            "station_id": station_id,
            "as_of_utc": cutoff,
            "builder_id": BUILDER_ID,
            "canonical_registry_sha256": registry["registry_sha256"],
            "source_inventory_content_sha256": inventory_sha,
            "latest_auto_trade_decision": decisions[station_id],
            "terminal_by_scope": {
                "STRATEGY_POLICY": {
                    "state": "BLOCKED",
                    "blockers": [
                        "UPSTREAM_PROBABILITY_ROOT_MISSING",
                        "POLICY_ACTIVATION_MISSING",
                    ],
                    "typed_scope_emitted": False,
                },
                "STRATEGY_UTILITY": {
                    "state": "BLOCKED",
                    "blockers": [
                        "POLICY_ACTIVATION_MISSING",
                        "UTILITY_FILL_SETTLEMENT_MISSING",
                        "OFFICIAL_WS_CANONICAL_FACT_MISSING",
                    ],
                    "typed_scope_emitted": False,
                },
                "PURE_YES_BASKET": {
                    "state": "BLOCKED",
                    "blockers": decisions[station_id]["reason_codes"],
                    "will_order": False,
                    "buy_no_intent_emitted": False,
                },
            },
            "utility_contract_pointer_sha256": utility_pointer["pointer_sha256"],
            "utility_contract_state": utility_pointer["state"],
            "legacy_policy_or_ws_used_as_canonical_input": False,
            "canonical_schema_state": schema_state,
            "v14_runtime_consumer_ready": False,
        }
        rows.append({**identity, "content_sha256": content_sha256(identity)})
    basis_identity = {
        "schema": TERMINAL_BASIS_SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": BUILDER_ID,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "canonical_registry_sha256": registry["registry_sha256"],
        "station_ids": station_ids,
        "terminal_scope_count": len(rows),
        "terminal_scopes": rows,
        "source_inventory_content_sha256": inventory_sha,
        "production_no_live": True,
        "canonical_schema_state": schema_state,
        "v14_runtime_consumer_ready": False,
        "writes_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    terminal_basis = {**basis_identity, "content_sha256": content_sha256(basis_identity)}
    row_hashes = [row["content_sha256"] for row in rows]
    shared_sources = (
        registry["registry_sha256"], inventory_sha, terminal_basis["content_sha256"]
    )
    policy_basis = build_scope_basis(
        scope_type="STRATEGY_POLICY",
        as_of_utc=cutoff,
        expected_keys=(),
        source_content_sha256s=shared_sources,
        unresolved_input_receipts=row_hashes,
    )
    utility_basis = build_scope_basis(
        scope_type="STRATEGY_UTILITY",
        as_of_utc=cutoff,
        expected_keys=(),
        source_content_sha256s=shared_sources,
        unresolved_input_receipts=row_hashes,
    )
    bundle_identity = {
        "schema": SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": BUILDER_ID,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "terminal_scope_count": len(rows),
        "strategy_policy_expected_key_count": 0,
        "strategy_utility_expected_key_count": 0,
        "unresolved_input_receipt_count": len(row_hashes),
        "terminal_basis_content_sha256": terminal_basis["content_sha256"],
        "strategy_policy_basis_content_sha256": policy_basis["content_sha256"],
        "strategy_utility_basis_content_sha256": utility_basis["content_sha256"],
        "source_inventory_content_sha256": inventory_sha,
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "hard_no_strategy_removed": True,
        "buy_no_intent_count": 0,
        "will_order_scope_count": 0,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
    }
    bundle = {**bundle_identity, "content_sha256": content_sha256(bundle_identity)}
    return policy_basis, utility_basis, terminal_basis, bundle


__all__ = ["PR14StrategyBasisError", "build_pr14_strategy_artifacts"]
