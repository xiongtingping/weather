#!/usr/bin/env python3
"""Apply one pre-registered strict-Δ no-live candidate authority.

This breaks the forward-OOS bootstrap cycle without claiming historical OOS.
It writes only the isolated target SSOT authority tables and an audit receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json
from bayes_basket_target.strict_delta_artifact import (
    ACTIVATION_SCHEMA,
    ARTIFACT_SCHEMA,
    PROMOTION_SCHEMA,
    StrictDeltaPolicyArtifact,
    commit_strict_delta_authority,
)


SCHEMA = "strict_delta_preregistered_candidate_apply_v1"
DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_PROTOCOL = (
    ROOT
    / "config/trading/bayes_basket_target_strict_delta_RKPK_forward_candidate_v1.json"
)
DEFAULT_RECEIPT = (
    ROOT
    / "output/audits/bayes_basket_target_strict_delta_RKPK_forward_candidate_v1.json"
)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_protocol(raw: Mapping[str, Any]) -> None:
    if raw.get("schema") != "strict_delta_preregistered_candidate_protocol_v1":
        raise ContractError("strict_delta_candidate_protocol_schema_invalid")
    required = {
        "station_id",
        "stage",
        "season_bin",
        "maturity_bin",
        "liquidity_bin",
        "strategy_version",
        "authority_scope",
        "policy",
        "negative_evidence_path",
        "negative_evidence_file_sha256",
        "planned_train_start_date",
        "planned_train_end_date",
        "planned_holdout_start_date",
        "planned_holdout_end_date",
        "oos_gate_version",
        "trained_through_utc",
        "valid_from_utc",
        "valid_until_utc",
        "activated_at_utc",
        "generation",
    }
    missing = sorted(key for key in required if raw.get(key) in (None, ""))
    if missing:
        raise ContractError(
            "strict_delta_candidate_protocol_missing:" + ",".join(missing)
        )
    if (
        raw.get("authority_scope") != "PRODUCTION_NO_LIVE_CANDIDATE"
        or raw.get("execution_mode") != "production_no_live"
        or raw.get("authorizes_order") is not False
        or raw.get("live_submit_authorized") is not False
        or raw.get("calls_order_gateway") is not False
    ):
        raise ContractError("strict_delta_candidate_no_live_boundary_invalid")


def build_authority(
    protocol: Mapping[str, Any],
    *,
    protocol_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _assert_protocol(protocol)
    evidence_path = (
        ROOT / str(protocol["negative_evidence_path"])
    ).resolve()
    if (
        not evidence_path.is_relative_to(ROOT)
        or _file_sha256(evidence_path)
        != str(protocol["negative_evidence_file_sha256"])
    ):
        raise ContractError("strict_delta_candidate_negative_evidence_drift")
    artifact_id = "strict-delta-rkpk-forward-candidate-" + protocol_sha256[:16]
    policy = dict(protocol["policy"])
    policy["schema"] = "strict_delta_policy_v1"
    policy["policy_version"] = artifact_id
    artifact_identity = {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": artifact_id,
        "station_id": str(protocol["station_id"]).upper(),
        "stage": str(protocol["stage"]).upper(),
        "season_bin": str(protocol["season_bin"]),
        "maturity_bin": str(protocol["maturity_bin"]),
        "liquidity_bin": str(protocol["liquidity_bin"]),
        "strategy_version": str(protocol["strategy_version"]),
        "policy": policy,
        "training_evidence_sha256": str(
            protocol["negative_evidence_file_sha256"]
        ),
        "train_start_date": str(protocol["planned_train_start_date"]),
        "train_end_date": str(protocol["planned_train_end_date"]),
        "holdout_start_date": str(protocol["planned_holdout_start_date"]),
        "holdout_end_date": str(protocol["planned_holdout_end_date"]),
        "sample_n": 0,
        "holdout_n": 0,
        "oos_metrics": {},
        "oos_gate_version": str(protocol["oos_gate_version"]),
        "oos_gate_passed": False,
        "trained_through_utc": str(protocol["trained_through_utc"]),
        "valid_from_utc": str(protocol["valid_from_utc"]),
        "valid_until_utc": str(protocol["valid_until_utc"]),
        "authority_scope": str(protocol["authority_scope"]),
    }
    artifact = {
        **artifact_identity,
        "content_sha256": content_sha256(artifact_identity),
    }
    governed = StrictDeltaPolicyArtifact.from_mapping(artifact)
    promotion_identity = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": "strict-delta-candidate-receipt-" + protocol_sha256[:16],
        "artifact_id": governed.artifact_id,
        "decision_seq": 1,
        "decision": "CANDIDATE",
        "gate_version": governed.oos_gate_version,
        "decided_at_utc": str(protocol["activated_at_utc"]),
        "receipt_sha256": protocol_sha256,
    }
    promotion = {
        **promotion_identity,
        "content_sha256": content_sha256(promotion_identity),
    }
    activation_identity = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": "strict-delta-candidate-activation-" + protocol_sha256[:16],
        "artifact_id": governed.artifact_id,
        "station_id": governed.station_id,
        "stage": governed.stage,
        "season_bin": governed.season_bin,
        "maturity_bin": governed.maturity_bin,
        "liquidity_bin": governed.liquidity_bin,
        "strategy_version": governed.strategy_version,
        "generation": int(protocol["generation"]),
        "valid_from_utc": governed.valid_from_utc,
        "valid_until_utc": governed.valid_until_utc,
        "activated_at_utc": str(protocol["activated_at_utc"]),
        "authority_scope": governed.authority_scope,
    }
    activation = {
        **activation_identity,
        "content_sha256": content_sha256(activation_identity),
    }
    return artifact, promotion, activation


def run(*, db: Path, protocol_path: Path, receipt: Path) -> dict[str, Any]:
    protocol = load_bounded_json(protocol_path)
    protocol_sha256 = hashlib.sha256(_canonical(protocol)).hexdigest()
    artifact, promotion, activation = build_authority(
        protocol,
        protocol_sha256=protocol_sha256,
    )
    with sqlite3.connect(db, timeout=30.0, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        table_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND "
                "name='basket_strict_delta_policy_promotion'"
            ).fetchone()[0]
        )
        if "'CANDIDATE'" not in table_sql:
            raise ContractError(
                "strict_delta_candidate_schema_migration_required"
            )
        authority_receipt = commit_strict_delta_authority(
            conn,
            artifact=artifact,
            promotion=promotion,
            activation=activation,
        )
    result = {
        "schema": SCHEMA,
        "state": authority_receipt["state"],
        "protocol_path": str(protocol_path.resolve()),
        "protocol_sha256": protocol_sha256,
        "artifact": artifact,
        "promotion": promotion,
        "activation": activation,
        "authority_receipt": authority_receipt,
        "execution_mode": "production_no_live",
        "authorizes_order": False,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    result["receipt_sha256"] = content_sha256(result)
    write_bounded_json(receipt, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    try:
        result = run(
            db=args.db.expanduser().resolve(),
            protocol_path=args.protocol.expanduser().resolve(),
            receipt=args.receipt.expanduser().resolve(),
        )
    except Exception as exc:
        print(json.dumps({
            "schema": SCHEMA,
            "state": "BLOCKED",
            "reason": f"{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }, sort_keys=True))
        return 2
    print(json.dumps({
        "schema": result["schema"],
        "state": result["state"],
        "artifact_id": result["artifact"]["artifact_id"],
        "activation_id": result["activation"]["activation_id"],
        "receipt_sha256": result["receipt_sha256"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
