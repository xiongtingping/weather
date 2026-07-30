#!/usr/bin/env python3
"""Materialize sequential RKPK P1 evidence and attempt one no-live basket replay.

This command is intentionally retrospective and target-isolated.  It consumes
an already materialized P0-M replay plus formal canonical AWC observations,
then compiles only observations that advance the official reported daily high.
It never mutates the source database and never calls an order gateway.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope, content_sha256  # noqa: E402
from bayes_basket_target.cash_phase import (  # noqa: E402
    BasketCashPhaseInput,
    CashPhase,
    resolve_cash_phase_policy,
)
from bayes_basket_target.database import resolve_target_database  # noqa: E402
from bayes_basket_target.materialization import insert_probability_envelope  # noqa: E402
from bayes_basket_target.p0_preselection import (  # noqa: E402
    AUTHORITY_SCOPE,
    _market_snapshot,
    _registry_rows,
    load_market_registry_snapshot,
    load_official_ws_quotes,
)
from bayes_basket_target.production import compile_production_plan  # noqa: E402
from bayes_basket_target.production_io import load_production_artifacts  # noqa: E402
from bayes_basket_target.resource_bounds import load_bounded_json, write_bounded_json  # noqa: E402
from bayes_basket_target.settlement_time import (  # noqa: E402
    assert_probability_timezone_matches_market,
    registry_settlement_timezone,
)
from scripts.build_bayes_basket_target_p1_packets import compile_station_p1_packets  # noqa: E402


SCHEMA = "bayes_basket_p1_retrospective_replay_v1"
CANONICAL_SCHEMA = "bayes_basket_target_canonical_observation_evidence_v1"
P0M_SCHEMA = "bayes_basket_p0m_retrospective_replay_v1"


def _mapping(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve())
    if not isinstance(value, Mapping):
        raise ContractError(f"{path.name}_mapping_required")
    return value


def _validate_p0m_canonical_boundary(
    p0m_replay: Mapping[str, Any],
    canonical: Mapping[str, Any],
) -> None:
    if (
        p0m_replay.get("schema") != P0M_SCHEMA
        or p0m_replay.get("state") != "retrospective_no_live_materialized"
        or (p0m_replay.get("boundary") or {}).get("places_orders") is not False
    ):
        raise ContractError("p1_replay_p0m_boundary_invalid")
    if (
        canonical.get("schema") != CANONICAL_SCHEMA
        or canonical.get("state") != "EXPORTED"
        or canonical.get("target_timezone")
        != (p0m_replay.get("p0m_envelope") or {}).get("settlement_timezone")
        or canonical.get("blockers")
    ):
        raise ContractError("p1_replay_canonical_evidence_invalid")


def _formal_awc_station_rows(
    canonical: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
) -> list[dict[str, Any]]:
    rows = [
        dict(item)
        for item in canonical.get("observations", [])
        if isinstance(item, Mapping)
        and item.get("station_id") == station_id
        and item.get("event_date_local") == event_date
    ]
    if not rows:
        raise ContractError("p1_replay_formal_observations_missing")
    if any(
        str(item.get("source_lane") or "").strip().lower() != "awc"
        or str(item.get("report_type") or "").strip().upper() not in {"METAR", "SPECI"}
        or str(item.get("evidence_role") or "").strip().upper() != "FORMAL"
        or str(item.get("qc_status") or "").strip().upper() != "ACCEPTED"
        for item in rows
    ):
        raise ContractError("p1_replay_formal_awc_scope_invalid")
    return rows


def replay(
    *,
    p0m_replay_path: Path,
    canonical_evidence_path: Path,
    source_db_path: Path,
    registry_path: Path,
    ws_manifest_path: Path,
    evaluation_at_utc: str,
    freshness_seconds: int,
    maturity_bin: str,
) -> dict[str, Any]:
    p0m_replay = _mapping(p0m_replay_path)
    canonical = _mapping(canonical_evidence_path)
    registry = load_market_registry_snapshot(registry_path)
    ws_manifest = _mapping(ws_manifest_path)
    _ws_root, ws_quotes = load_official_ws_quotes(ws_manifest_path)
    _validate_p0m_canonical_boundary(p0m_replay, canonical)
    p0 = ProbabilityEnvelope.from_mapping(p0m_replay["p0_root"])
    p0m = ProbabilityEnvelope.from_mapping(p0m_replay["p0m_envelope"])
    station_rows = _formal_awc_station_rows(
        canonical,
        station_id=p0m.station_id,
        event_date=p0m.event_date,
    )
    registry_rows = [
        dict(item)
        for item in registry.get("rows", [])
        if isinstance(item, Mapping)
        and item.get("station_id") == p0m.station_id
        and item.get("event_date") == p0m.event_date
    ]
    registry_timezone = registry_settlement_timezone(
        registry_rows,
        station_id=p0m.station_id,
        event_date=p0m.event_date,
    )
    assert_probability_timezone_matches_market(
        probability_timezone=p0m.settlement_timezone,
        market_timezone=registry_timezone,
    )
    packets = compile_station_p1_packets(
        predecessor=p0m,
        observations=station_rows,
        registry_rows=registry_rows,
        compilation_at_utc=evaluation_at_utc,
        freshness_seconds=freshness_seconds,
    )
    if not packets:
        raise ContractError("p1_replay_no_new_reported_high")
    envelopes = [
        ProbabilityEnvelope.from_mapping(packet["expected_envelope"])
        for packet in packets
    ]
    latest = envelopes[-1]
    production_plan: Mapping[str, Any] | None = None
    production_market_snapshot: Mapping[str, Any] | None = None
    production_plan_blocker = ""
    source_path = resolve_target_database(source_db_path.expanduser().resolve())
    source_uri = f"file:{source_path}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30.0) as source:
        with sqlite3.connect(":memory:", timeout=30.0) as scratch:
            source.backup(scratch)
            scratch.execute(
                "DROP TRIGGER IF EXISTS basket_probability_envelope_no_delete"
            )
            ids = [p0.checkpoint_id, p0m.checkpoint_id]
            ids.extend(envelope.checkpoint_id for envelope in envelopes)
            scratch.executemany(
                "DELETE FROM basket_probability_envelope WHERE envelope_id=?",
                ((item,) for item in ids),
            )
            insert_probability_envelope(
                scratch, p0m, created_at_utc=p0m.decision_at_utc
            )
            insert_probability_envelope(
                scratch, p0m, created_at_utc=p0m.decision_at_utc
            )
            for envelope in envelopes:
                insert_probability_envelope(
                    scratch,
                    envelope,
                    created_at_utc=envelope.decision_at_utc,
                )
            try:
                artifacts = load_production_artifacts(
                    scratch,
                    station_id=p0m.station_id,
                    evaluation_at_utc=evaluation_at_utc,
                    require_calibration=True,
                )
                cash_candidates = [
                    item for item in artifacts.cash_phase_policies
                    if item.station_id == p0m.station_id
                ]
                if len(cash_candidates) != 1:
                    raise ContractError("p1_replay_cash_policy_not_unique")
                cash_policy = resolve_cash_phase_policy(
                    cash_candidates,
                    policy_version=cash_candidates[0].policy_version,
                    station_id=p0m.station_id,
                    observed_at_utc=evaluation_at_utc,
                    required_authority_scope=AUTHORITY_SCOPE,
                )
                phase_input = BasketCashPhaseInput.from_mapping({
                    "policy_version": cash_policy.policy_version,
                    "station_id": p0m.station_id,
                    "current_phase": CashPhase.HEAVY.value,
                    "event_date_local": p0m.event_date,
                    "observed_at_utc": evaluation_at_utc,
                    "station_timezone": cash_policy.station_timezone,
                    "checkpoint_id": "p1_cash_" + latest.checkpoint_id,
                })
                market = _market_snapshot(
                    envelope=latest,
                    registry_rows=_registry_rows(
                        registry,
                        station_id=p0m.station_id,
                        event_date=p0m.event_date,
                    ),
                    quotes=ws_quotes,
                    manifest=ws_manifest,
                    evaluation_at_utc=evaluation_at_utc,
                )
                production_market_snapshot = market
                production_plan = compile_production_plan(
                    probability_envelopes=[p0, p0m, *envelopes],
                    market_snapshot=market,
                    policy_activations=artifacts.policy_activations,
                    global_priors=artifacts.global_priors,
                    policy_artifacts=artifacts.policy_artifacts,
                    evaluation_at_utc=evaluation_at_utc,
                    season_bin=str(p0m.lineage["season_bin"]),
                    maturity_bin=maturity_bin,
                    liquidity_bin="liquid",
                    strategy_version="target-v1",
                    cash_phase_policy=cash_policy,
                    cash_phase_input=phase_input,
                    policy_authority_scope=AUTHORITY_SCOPE,
                )
            except ContractError as exc:
                production_plan_blocker = str(exc)
    identity = {
        "schema": SCHEMA,
        "state": (
            "retrospective_no_live_materialized"
            if production_plan is not None
            else "P1_MATERIALIZED_BASKET_FAIL_CLOSED"
        ),
        "p0m_checkpoint_id": p0m.checkpoint_id,
        "canonical_evidence_sha256": canonical["evidence_sha256"],
        "reported_observation_count": len(station_rows),
        "p1_update_count": len(envelopes),
        "consumed_observation_ids": [
            str(packet["observation_id"]) for packet in packets
        ],
        "p1_envelopes": [item.to_dict() for item in envelopes],
        "latest_p1_envelope": latest.to_dict(),
        "p1_production_plan": production_plan,
        "production_market_snapshot": production_market_snapshot,
        "production_plan_blocker": production_plan_blocker,
        "boundary": {
            "retrospective_replay_only": True,
            "native_local_observation_receipt": False,
            "settlement_truth_final_frozen": False,
            "writes_production_db": False,
            "writes_source_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p0m-replay", type=Path, required=True)
    parser.add_argument("--canonical-evidence", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--ws-manifest", type=Path, required=True)
    parser.add_argument("--evaluation-at-utc", required=True)
    parser.add_argument("--freshness-seconds", type=int, default=3600)
    parser.add_argument("--maturity-bin", default="d1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(
        p0m_replay_path=args.p0m_replay,
        canonical_evidence_path=args.canonical_evidence,
        source_db_path=args.source_db,
        registry_path=args.registry,
        ws_manifest_path=args.ws_manifest,
        evaluation_at_utc=args.evaluation_at_utc,
        freshness_seconds=args.freshness_seconds,
        maturity_bin=args.maturity_bin,
    )
    write_bounded_json(args.output.expanduser().resolve(), result)
    latest = result["latest_p1_envelope"]
    print(json.dumps({
        "state": result["state"],
        "p1_checkpoint_id": latest["checkpoint_id"],
        "reported_observation_count": result["reported_observation_count"],
        "p1_update_count": result["p1_update_count"],
        "production_plan_blocker": result["production_plan_blocker"],
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "output": str(args.output.expanduser().resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["state"] == "retrospective_no_live_materialized" else 2


if __name__ == "__main__":
    raise SystemExit(main())
