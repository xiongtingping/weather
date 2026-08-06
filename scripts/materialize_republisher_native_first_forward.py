#!/usr/bin/env python3
"""Probe native, capture fallback, and atomically stage unpublished P0.

The command is single-station by design so a transport failure cannot create a
partially advanced multi-station batch.  Planning is the default.  Network
execution and canonical commit are separate explicit gates.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract import (  # noqa: E402
    CanonicalRepublisherCrossSelectionFact,
    CanonicalRepublisherNoLiveActivationFact,
)
from bayes_basket_target.canonical_ingress import (  # noqa: E402
    stage_republisher_forward_and_p0,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.market_bucket_bridge import (  # noqa: E402
    CanonicalMarketBucketBridgeFact,
)
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    validate_writer_quiescence_receipt,
)
from bayes_basket_target.republisher_forward_evidence_store import (  # noqa: E402
    store_manifest,
    store_probe_evidence,
    verify_address,
)
from bayes_basket_target.republisher_native_forward_probe import (  # noqa: E402
    DEFAULT_PROXY,
    build_native_probe_plan,
    capture_open_meteo_component,
    execute_native_probe,
)
from bayes_basket_target.republisher_no_live_p0 import (  # noqa: E402
    build_forward_applicability_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    assert_target_raw_retention_budget,
    load_bounded_json,
    write_bounded_json,
)


CONFIRMATION = "commit-republisher-native-first-forward-production-no-live"
EVIDENCE_RESERVATION_BYTES = 32 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _latest_activation(database: Path, station_id: str) -> CanonicalRepublisherNoLiveActivationFact:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_no_live_activation' "
            "AND json_extract(fact_json,'$.station_id')=? "
            "ORDER BY CAST(json_extract(fact_json,'$.activation_generation') AS INTEGER) DESC",
            (station_id,),
        ).fetchall()
    if not rows:
        raise ContractError("republisher_forward_activation_missing")
    try:
        fact = CanonicalRepublisherNoLiveActivationFact.from_mapping(
            json.loads(str(rows[0][0]))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_forward_activation_json_invalid") from exc
    if fact.activation_generation < 2:
        raise ContractError("republisher_forward_distribution_activation_required")
    return fact


def _plan_receipt(
    *,
    database: Path,
    activation: CanonicalRepublisherNoLiveActivationFact,
    event_date: str,
    evidence_root: Path,
) -> dict[str, Any]:
    identity = {
        "schema": "republisher_native_first_forward_plan_v1",
        "state": "PLAN_READY_NO_NETWORK",
        "database": str(database),
        "station_id": activation.station_id,
        "event_date": event_date,
        "settlement_timezone": activation.settlement_timezone,
        "lead_bucket": activation.lead_bucket,
        "activation_generation": activation.activation_generation,
        "activation_content_sha256": activation.content_sha256,
        "evidence_root": str(evidence_root),
        "proxy": DEFAULT_PROXY,
        "sequence": [
            "native_exact_cohort_pre_probe",
            "open_meteo_exact_product_capture_only_if_all_native_terminal_fallback",
            "native_exact_cohort_post_probe",
            "content_addressed_evidence_manifest",
            "canonical_forward_applicability_and_p0_atomic_stage",
            "materialized_unpublished_no_consumer_authority",
        ],
        "writes_target_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _fallback_receipts(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for expected_model, raw in zip(("AIFS", "ICON"), result.get("product_results", [])):
        if not isinstance(raw, Mapping) or raw.get("upstream_model_id") != expected_model:
            raise ContractError("republisher_forward_native_result_set_invalid")
        receipt = raw.get("native_unavailability_receipt")
        if not isinstance(receipt, Mapping):
            raise ContractError("republisher_forward_native_terminal_receipt_missing")
        rows.append(dict(receipt))
    if len(rows) != 2:
        raise ContractError("republisher_forward_native_terminal_receipt_set_invalid")
    return rows


def _public_probe_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key != "_retained_raw_objects"
    }


def _blocked(
    state: str,
    blocker: str,
    plan: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity = {
        "schema": "republisher_native_first_forward_materialization_receipt_v1",
        "state": state,
        "blocker": blocker,
        "plan_content_sha256": plan["content_sha256"],
        "canonical_rows_inserted": 0,
        "forward_applicability_written": False,
        "p0_written": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    if evidence is not None:
        identity.update(
            {
                "evidence_manifest_content_sha256": evidence[
                    "manifest_content_sha256"
                ],
                "evidence_manifest_content_address": evidence[
                    "manifest_content_address"
                ],
            }
        )
    return {**identity, "content_sha256": content_sha256(identity)}


def _native_lineage_terminal_receipt(
    database: Path,
    *,
    activation: CanonicalRepublisherNoLiveActivationFact,
    event_date: str,
    plan: Mapping[str, Any],
    probe_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Close PR10 when native current data has no exact historical activation.

    A republisher activation cannot calibrate an official-native current
    product by name similarity.  This receipt binds both exact product
    vectors and terminates the scope without fabricating forward or P0 facts.
    """

    supplied = str(probe_result.get("content_sha256") or "").strip().lower()
    identity = {
        key: value for key, value in probe_result.items()
        if key not in {"content_sha256", "_retained_raw_objects"}
    }
    if supplied != content_sha256(identity):
        raise ContractError("republisher_forward_reused_probe_hash_mismatch")
    if (
        probe_result.get("schema") != "republisher_native_forward_probe_result_v1"
        or str(probe_result.get("station_id") or "").strip().upper()
        != activation.station_id
        or str(probe_result.get("event_date") or "") != event_date
        or str(probe_result.get("settlement_timezone") or "")
        != activation.settlement_timezone
    ):
        raise ContractError("republisher_forward_reused_probe_scope_mismatch")
    products = probe_result.get("product_results")
    if not isinstance(products, list) or len(products) != 2:
        raise ContractError("republisher_forward_reused_probe_product_set_invalid")
    native_vector: list[dict[str, str]] = []
    for expected, raw in zip(("AIFS", "ICON"), products):
        if not isinstance(raw, Mapping) or raw.get("upstream_model_id") != expected:
            raise ContractError("republisher_forward_reused_probe_product_set_invalid")
        native_vector.append(
            {
                "upstream_model_id": expected,
                "native_provider_id": str(raw.get("native_provider_id") or ""),
                "native_product_id": str(raw.get("native_product_id") or ""),
                "terminal_status": str(raw.get("terminal_status") or ""),
            }
        )
    if not any(
        row["terminal_status"] == "NATIVE_COMPLETE_NO_FALLBACK"
        for row in native_vector
    ):
        raise ContractError("republisher_forward_reused_probe_not_native_complete")

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='republisher_cross_selection' AND content_sha256=?",
            (activation.selection_content_sha256,),
        ).fetchall()
    if len(rows) != 1:
        raise ContractError("republisher_forward_selection_not_unique")
    try:
        selection = CanonicalRepublisherCrossSelectionFact.from_mapping(
            json.loads(str(rows[0][0]))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_forward_selection_json_invalid") from exc
    if (
        selection.station_id != activation.station_id
        or selection.content_sha256 != activation.selection_content_sha256
    ):
        raise ContractError("republisher_forward_selection_activation_mismatch")
    historical_vector = [
        {
            "upstream_model_id": item.upstream_model_id,
            "republisher_product_id": item.republisher_product_id,
            "independence_family_id": item.independence_family_id,
            "artifact_content_sha256": item.artifact_content_sha256,
        }
        for item in selection.component_artifact_refs
    ]
    receipt_identity = {
        "schema": "republisher_native_first_forward_materialization_receipt_v1",
        "state": "BLOCKED",
        "blocker": "LINEAGE_MISMATCH",
        "failure_code": "native_current_product_activation_lineage_unbound",
        "native_first_disposition": "NATIVE_COMPLETE_NO_FALLBACK",
        "plan_content_sha256": plan["content_sha256"],
        "probe_result_content_sha256": supplied,
        "station_id": activation.station_id,
        "event_date": event_date,
        "settlement_timezone": activation.settlement_timezone,
        "activation_content_sha256": activation.content_sha256,
        "selection_content_sha256": selection.content_sha256,
        "historical_activation_product_vector": historical_vector,
        "native_current_product_vector": native_vector,
        "canonical_rows_inserted": 0,
        "forward_applicability_written": False,
        "p0_written": False,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "release_state": "BLOCKED",
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }


def _exact_market_bridge(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    cutoff_utc: str,
) -> tuple[CanonicalMarketBucketBridgeFact, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT fact_json,content_sha256 FROM canonical_ssot_fact "
        "WHERE fact_type='market_bucket_bridge' "
        "AND json_extract(fact_json,'$.station_id')=? "
        "AND json_extract(fact_json,'$.event_date')=? "
        "AND ingested_at_utc<=? ORDER BY fact_id",
        (station_id, event_date, cutoff_utc),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("republisher_forward_p0_market_bridge_not_unique")
    try:
        bridge = CanonicalMarketBucketBridgeFact.from_mapping(
            json.loads(str(rows[0][0]))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("republisher_forward_p0_market_bridge_json_invalid") from exc
    if str(rows[0][1]) != bridge.content_sha256:
        raise ContractError("republisher_forward_p0_market_bridge_hash_mismatch")
    intervals = [
        {
            "bucket_id": row["outcome"],
            "ordinal": row["ordinal"],
            "lower_c": row["lower_c"],
            "upper_c": row["upper_c"],
        }
        for row in bridge.raw["bucket_contracts"]
    ]
    return bridge, intervals


def _fact_count(conn: sqlite3.Connection, fact_type: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type=?",
            (fact_type,),
        ).fetchone()[0]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--contract-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    parser.add_argument("--execute-network", action="store_true")
    parser.add_argument(
        "--reuse-probe-result",
        type=Path,
        help="reuse an immutable prior native probe result; performs no network or DB write",
    )
    parser.add_argument("--commit-production-no-live", action="store_true")
    parser.add_argument("--writer-quiescence-receipt", type=Path)
    parser.add_argument("--confirm")
    args = parser.parse_args()

    station = str(args.station).strip().upper()
    supplied = args.database.expanduser()
    if supplied.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    database = supplied.resolve(strict=True)
    evidence_root = args.evidence_root.expanduser().resolve()
    commit = bool(args.commit_production_no_live)
    if commit and not args.execute_network:
        parser.error("commit requires --execute-network")
    if args.reuse_probe_result is not None and (args.execute_network or commit):
        parser.error("--reuse-probe-result forbids network and commit")
    if commit and args.writer_quiescence_receipt is None:
        parser.error("commit requires --writer-quiescence-receipt")
    if commit and args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION!r}")
    activation = _latest_activation(database, station)
    plan = _plan_receipt(
        database=database,
        activation=activation,
        event_date=args.event_date,
        evidence_root=evidence_root,
    )
    if args.reuse_probe_result is not None:
        probe_result = load_bounded_json(
            args.reuse_probe_result.expanduser().resolve(strict=True)
        )
        receipt = _native_lineage_terminal_receipt(
            database,
            activation=activation,
            event_date=args.event_date,
            plan=plan,
            probe_result=probe_result,
        )
        write_bounded_json(args.contract_output, probe_result)
        write_bounded_json(args.receipt_output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2
    if not args.execute_network:
        write_bounded_json(args.contract_output, plan)
        write_bounded_json(args.receipt_output, plan)
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0

    assert_process_memory_budget(projected_bytes=EVIDENCE_RESERVATION_BYTES)
    assert_target_raw_retention_budget(
        evidence_root, bytes_needed=EVIDENCE_RESERVATION_BYTES
    )
    pre_plan = build_native_probe_plan(
        station_id=station,
        event_date=args.event_date,
        settlement_timezone=activation.settlement_timezone,
        checked_at_utc=_utc_now(),
    )
    pre_result = execute_native_probe(pre_plan)
    if not pre_result["fallback_terminal_for_all_products"]:
        blocker = (
            "NATIVE_COMPLETE_NO_FALLBACK"
            if pre_result["native_complete_for_any_product"]
            else "NATIVE_EVIDENCE_INDETERMINATE"
        )
        evidence = store_probe_evidence(
            evidence_root,
            phase="pre",
            plan=pre_plan,
            result=pre_result,
        )
        receipt = _blocked(
            "BLOCKED_NO_CAPTURE", blocker, plan, evidence=evidence
        )
        write_bounded_json(args.contract_output, _public_probe_result(pre_result))
        write_bounded_json(args.receipt_output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    captures = {
        model: capture_open_meteo_component(
            model=model,
            station_id=station,
            event_date=args.event_date,
            settlement_timezone=activation.settlement_timezone,
        )
        for model in ("AIFS", "ICON")
    }
    post_plan = build_native_probe_plan(
        station_id=station,
        event_date=args.event_date,
        settlement_timezone=activation.settlement_timezone,
        checked_at_utc=_utc_now(),
    )
    post_result = execute_native_probe(post_plan)
    if not post_result["fallback_terminal_for_all_products"]:
        blocker = (
            "NATIVE_COMPLETE_AFTER_CAPTURE"
            if post_result["native_complete_for_any_product"]
            else "POST_CAPTURE_NATIVE_EVIDENCE_INDETERMINATE"
        )
        evidence = store_probe_evidence(
            evidence_root,
            phase="post",
            plan=post_plan,
            result=post_result,
        )
        receipt = _blocked(
            "BLOCKED_POST_PROBE", blocker, plan, evidence=evidence
        )
        write_bounded_json(args.contract_output, _public_probe_result(post_result))
        write_bounded_json(args.receipt_output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    evidence = store_manifest(
        evidence_root,
        station_id=station,
        event_date=args.event_date,
        settlement_timezone=activation.settlement_timezone,
        pre_probe_plan=pre_plan,
        pre_probe_result=pre_result,
        captures=captures,
        post_probe_plan=post_plan,
        post_probe_result=post_result,
    )
    verify_address(
        evidence_root,
        content_address=evidence["manifest_content_address"],
        expected_sha256=evidence["manifest_content_sha256"],
    )
    component_receipts = [captures[model]["component_receipt"] for model in ("AIFS", "ICON")]
    native_receipts = _fallback_receipts(post_result)
    latest_component = max(
        _instant(row["response_completed_at_utc"]) for row in component_receipts
    )
    latest_native = max(_instant(row["checked_at_utc"]) for row in native_receipts)
    decision = max(latest_native, datetime.now(timezone.utc))
    decision_text = decision.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fresh_until = (latest_component + timedelta(hours=6)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    native_expires = (latest_native + timedelta(minutes=5)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    if decision >= min(_instant(fresh_until), _instant(native_expires)):
        receipt = _blocked("BLOCKED_STALE_CAPTURE", "FORWARD_EVIDENCE_EXPIRED", plan)
        write_bounded_json(args.contract_output, evidence["manifest"])
        write_bounded_json(args.receipt_output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2

    if commit:
        quiescence = load_bounded_json(
            args.writer_quiescence_receipt.expanduser().resolve(strict=True),
            limit=MAX_JSON_INPUT_BYTES,
        )
        validate_writer_quiescence_receipt(
            quiescence,
            db_path=database,
            require_current_database_identity=True,
        )

    target = sqlite3.connect(database, timeout=60.0, isolation_level=None)
    work = target
    source_for_preview: sqlite3.Connection | None = None
    if not commit:
        source_for_preview = target
        work = sqlite3.connect(":memory:", isolation_level=None)
        source_for_preview.backup(work)
    try:
        work.execute("PRAGMA busy_timeout=60000")
        before_forward = _fact_count(work, "forward_applicability")
        before_p0 = _fact_count(work, "p0_snapshot")
        before_envelope = int(
            work.execute(
                "SELECT COUNT(*) FROM basket_probability_envelope "
                "WHERE stage='P0' AND state='COMMITTED'"
            ).fetchone()[0]
        )
        forward_packet = build_forward_applicability_contract(
            work,
            activation_content_sha256=activation.content_sha256,
            station_id=station,
            event_date=args.event_date,
            decision_at_utc=decision_text,
            component_receipts=component_receipts,
            native_unavailability_receipts=native_receipts,
            evidence_manifest_content_sha256=evidence["manifest_content_sha256"],
            evidence_manifest_content_address=evidence["manifest_content_address"],
            fresh_until_utc=fresh_until,
            native_evidence_expires_at_utc=native_expires,
            as_of_utc=decision_text,
        )
        bridge, intervals = _exact_market_bridge(
            work,
            station_id=station,
            event_date=args.event_date,
            cutoff_utc=decision_text,
        )
        work.execute("BEGIN IMMEDIATE")
        try:
            staged = stage_republisher_forward_and_p0(
                work,
                forward_packet,
                bucket_intervals=intervals,
                market_bucket_bridge_sha256=bridge.content_sha256,
                external_evidence_root=evidence_root,
                created_at_utc=decision_text,
                as_of_utc=decision_text,
            )
            staged_forward = _fact_count(work, "forward_applicability")
            staged_p0 = _fact_count(work, "p0_snapshot")
            staged_envelope = int(
                work.execute(
                    "SELECT COUNT(*) FROM basket_probability_envelope "
                    "WHERE stage='P0' AND state='COMMITTED'"
                ).fetchone()[0]
            )
            if commit:
                work.commit()
            else:
                work.rollback()
        except Exception:
            if work.in_transaction:
                work.rollback()
            raise
        after_forward = _fact_count(work, "forward_applicability")
        after_p0 = _fact_count(work, "p0_snapshot")
        after_envelope = int(
            work.execute(
                "SELECT COUNT(*) FROM basket_probability_envelope "
                "WHERE stage='P0' AND state='COMMITTED'"
            ).fetchone()[0]
        )
    finally:
        work.close()
        if source_for_preview is not None:
            source_for_preview.close()

    expected_forward_delta = staged_forward - before_forward if commit else 0
    expected_p0_delta = staged_p0 - before_p0 if commit else 0
    expected_envelope_delta = staged_envelope - before_envelope if commit else 0
    if (
        after_forward - before_forward != expected_forward_delta
        or after_p0 - before_p0 != expected_p0_delta
        or after_envelope - before_envelope != expected_envelope_delta
    ):
        raise SystemExit("republisher_forward_p0_atomic_count_mismatch")
    materialization_contract_identity = {
        "schema": "republisher_native_first_forward_p0_materialization_contract_v1",
        "forward_contract": forward_packet,
        "p0_contract": staged["p0_contract"],
        "market_bucket_bridge_content_sha256": bridge.content_sha256,
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    materialization_contract = {
        **materialization_contract_identity,
        "content_sha256": content_sha256(materialization_contract_identity),
    }
    write_bounded_json(args.contract_output, materialization_contract)
    receipt_identity = {
        "schema": "republisher_native_first_forward_materialization_receipt_v1",
        "state": "COMMITTED_MATERIALIZED_UNPUBLISHED" if commit else "VALIDATED_NO_WRITE",
        "station_id": station,
        "event_date": args.event_date,
        "activation_content_sha256": activation.content_sha256,
        "evidence_manifest_content_sha256": evidence["manifest_content_sha256"],
        "evidence_manifest_content_address": evidence["manifest_content_address"],
        "contract_content_sha256": materialization_contract["content_sha256"],
        "forward_applicability_content_sha256": staged[
            "forward_applicability_content_sha256"
        ],
        "p0_snapshot_content_sha256": staged["p0_snapshot_content_sha256"],
        "probability_root_sha256": staged["probability_root_sha256"],
        "canonical_rows_inserted": (
            expected_forward_delta + expected_p0_delta if commit else 0
        ),
        "forward_applicability_count_before": before_forward,
        "forward_applicability_count_after": after_forward,
        "p0_snapshot_count_before": before_p0,
        "p0_snapshot_count_after": after_p0,
        "p0_envelope_count_before": before_envelope,
        "p0_envelope_count_after": after_envelope,
        "forward_applicability_written": bool(commit and expected_forward_delta),
        "p0_written": bool(commit and expected_p0_delta and expected_envelope_delta),
        "release_state": "MATERIALIZED_UNPUBLISHED",
        "production_no_live_only": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    write_bounded_json(args.receipt_output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        raise SystemExit(str(exc)) from exc
