#!/usr/bin/env python3
"""Complete the RKPK P0 -> P0-M bridge after the no-live v4 cutover.

The command never places orders or calls an order gateway.  It waits for the
resident service to publish the exact 00Z/d2 P0, evaluates the preregistered
TAF transport OOS against that live fingerprint, atomically publishes the
bound TAF artifact, and then observes the resident SSOT for P0-M/P1/decision
progress.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.calibration import (  # noqa: E402
    bind_taf_artifact_to_live_nwp_compatibility,
)
from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.contracts import (  # noqa: E402
    FORMAL_ENSEMBLE_METHOD_VERSION,
    ModelEnsembleArtifact,
    content_sha256,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_process_memory_budget,
    load_bounded_json,
    write_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.no_live_capital_epoch import (  # noqa: E402
    target_epoch_identity,
)
from bayes_basket_target.runtime_config import (  # noqa: E402
    DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG,
    load_target_no_live_runtime_config,
)
from bayes_basket_target.resident_taf_producer import (  # noqa: E402
    refresh_resident_taf_inputs,
)
from bayes_basket_target.ssot import (  # noqa: E402
    inspect_calibration_model_pool_schema,
)
from bayes_basket_target.taf_nwp_transport_oos import (  # noqa: E402
    build_transport_oos_report,
)


DB = ROOT / "data" / "bayes_basket_target_ssot.db"
P0_INBOX = ROOT / "data" / "bayes_basket_target_p0_inbox.json"
TAF_INBOX = ROOT / "data" / "bayes_basket_target_taf_revision_inbox.json"
ACTIVE_TAF = (
    ROOT / "config" / "artifacts"
    / "bayes_basket_target_taf_revision_active.json"
)
ACTIVATION = (
    ROOT / "config" / "trading"
    / "bayes_basket_target_production_no_live_activation_v1.json"
)
LIVE_PAUSE = ROOT / "data" / "live_paused.flag"
EXACT_JOIN = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_00z_d2_taf_exact_join_30d_v4.json"
)
CROSSFIT = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_00z_d2_taf_nwp_crossfit_30d_v4.json"
)
TAF_ARTIFACT = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_00z_d2_taf_revision_artifact_v4.json"
)
PREFIX_TAF = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_kst_recalibration_20260727"
    / "awc_taf_calibration_RKPK_20260628_0630.json"
)
PREFIX_ECMWF = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_ecmwf_d1_20260628_0713.json"
)
PREFIX_GFS = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_gfs_d1_20260628_0713.json"
)
TRANSPORT_REPORT = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_00z_d2_taf_transport_oos_v4_current.json"
)
BOUND_TAF = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_00z_d2_taf_bound_current.json"
)
PUBLISH_RECEIPT = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_native_00z_d2_taf_publish_current.json"
)
FINAL_RECEIPT = (
    ROOT / "output" / "audits"
    / "bayes_basket_target_rkpk_vertical_completion_current.json"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _validate_delta_v2_policy_rows(
    rows: list[tuple[str, str, str, int, int, str | None]],
) -> None:
    active = sorted(
        (
            maturity_bin,
            strategy_version,
            artifact_id,
            int(generation),
            int(oos_gate_passed),
        )
        for (
            strategy_version,
            maturity_bin,
            artifact_id,
            generation,
            oos_gate_passed,
            decision,
        ) in rows
        if decision == "NO_LIVE_ACTIVATED"
    )
    if active != [
        (
            "d1",
            "target-v2",
            "delta-v2-policy-rkpk-summer-liquid-v3-d1",
            2,
            0,
        ),
        (
            "d2",
            "target-v2",
            "delta-v2-policy-rkpk-summer-liquid-v3-d2",
            2,
            0,
        ),
        (
            "d2_fallback",
            "target-v2",
            "delta-v2-policy-rkpk-summer-liquid-v3-d2_fallback",
            2,
            0,
        ),
    ]:
        raise RuntimeError("rkpk_completion_delta_v2_activation_missing")
    inactive = [
        (strategy_version, maturity_bin, artifact_id, int(generation), decision)
        for (
            strategy_version,
            maturity_bin,
            artifact_id,
            generation,
            _oos_gate_passed,
            decision,
        ) in rows
        if decision != "NO_LIVE_ACTIVATED"
    ]
    if sorted(inactive) != [
        (
            "target-v1",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v1-d1",
            1,
            "SUPERSEDED",
        ),
        (
            "target-v1",
            "d2_fallback",
            "delta-v2-policy-rkpk-summer-liquid-v1-d2_fallback",
            1,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d1",
            "delta-v2-policy-rkpk-summer-liquid-v2-d1",
            1,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d2",
            "delta-v2-policy-rkpk-summer-liquid-v2-d2",
            1,
            "SUPERSEDED",
        ),
        (
            "target-v2",
            "d2_fallback",
            "delta-v2-policy-rkpk-summer-liquid-v2-d2_fallback",
            1,
            "SUPERSEDED",
        ),
    ]:
        raise RuntimeError(
            "rkpk_completion_delta_v2_competing_activation_invalid"
        )


def _preconditions(database: Path) -> dict[str, Any]:
    if not LIVE_PAUSE.is_file():
        raise RuntimeError("rkpk_completion_live_pause_missing")
    with sqlite3.connect(
        f"file:{database.resolve()}?mode=ro",
        uri=True,
        timeout=30,
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("rkpk_completion_quick_check_failed")
        if inspect_calibration_model_pool_schema(conn).get("ok") is not True:
            raise RuntimeError("rkpk_completion_five_model_schema_missing")
        activation = load_target_activation(ACTIVATION)
        activation_row = conn.execute(
            "SELECT generation,activation_sha256 FROM "
            "basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
        if (
            activation_row is None
            or str(activation_row[1])
            != str(activation["activation_contract_sha256"])
        ):
            raise RuntimeError("rkpk_completion_activation_state_drift")
        runtime = load_target_no_live_runtime_config(
            DEFAULT_TARGET_NO_LIVE_RUNTIME_CONFIG
        )
        epoch = target_epoch_identity(runtime)
        pool = conn.execute(
            "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version "
            "FROM basket_target_capital_pool WHERE pool_id=1"
        ).fetchone()
        if (
            pool is None
            or abs(float(pool[0]) - float(
                runtime["target_remaining_budget_usdc"]
            )) > 1e-9
            or float(pool[1]) < 0.0
            or float(pool[1]) > float(pool[0])
            or str(pool[2]) != str(epoch["capital_epoch_id"])
        ):
            raise RuntimeError("rkpk_completion_capital_epoch_drift")
        migration = conn.execute(
            "SELECT migration_id,migration_json,content_sha256 "
            "FROM basket_target_capital_epoch_migrations "
            "WHERE next_epoch_id=?",
            (epoch["capital_epoch_id"],),
        ).fetchone()
        if migration is None:
            raise RuntimeError("rkpk_completion_capital_migration_missing")
        migration_identity = json.loads(str(migration[1]))
        if (
            content_sha256(migration_identity) != str(migration[2])
            or migration_identity.get("runtime_contract_sha256")
            != epoch["runtime_contract_sha256"]
        ):
            raise RuntimeError(
                "rkpk_completion_capital_migration_content_drift"
            )
        activations = conn.execute(
            "SELECT artifact_kind,generation,count(*) "
            "FROM calibration_activation "
            "WHERE station_id='RKPK' "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' AND ("
            "(artifact_kind='MODEL_CALIBRATION' AND generation=4) OR "
            "(artifact_kind='MODEL_ENSEMBLE' AND generation=5)) "
            "GROUP BY artifact_kind,generation ORDER BY artifact_kind"
        ).fetchall()
        if activations != [
            ("MODEL_CALIBRATION", 4, 2),
            ("MODEL_ENSEMBLE", 5, 1),
        ]:
            raise RuntimeError("rkpk_completion_v4_activation_missing")
        policies = conn.execute(
            "SELECT p.strategy_version,a.maturity_bin,a.artifact_id,"
            "a.generation,p.oos_gate_passed,("
            "SELECT promotion.decision "
            "FROM basket_delta_v2_policy_promotion promotion "
            "WHERE promotion.artifact_id=a.artifact_id "
            "ORDER BY promotion.decision_seq DESC LIMIT 1) "
            "FROM basket_delta_v2_policy_activation a "
            "JOIN basket_delta_v2_policy_artifact p "
            "ON p.artifact_id=a.artifact_id "
            "WHERE a.station_id='RKPK' "
            "AND a.authority_scope='PRODUCTION_NO_LIVE' "
            "ORDER BY p.strategy_version,a.maturity_bin"
        ).fetchall()
        _validate_delta_v2_policy_rows(policies)
        target_v2_prior_ids = conn.execute(
            "SELECT global_prior_id FROM basket_global_prior_artifact "
            "WHERE strategy_version='target-v2' ORDER BY global_prior_id"
        ).fetchall()
        target_v2_policy_scopes = conn.execute(
            "SELECT stage,maturity_bin,count(*),max(generation),"
            "max(CASE WHEN generation=2 THEN global_prior_id ELSE '' END) "
            "FROM basket_policy_activation "
            "WHERE station_id='RKPK' AND strategy_version='target-v2' "
            "AND season_bin='summer' AND liquidity_bin='liquid' "
            "GROUP BY stage,maturity_bin ORDER BY stage,maturity_bin"
        ).fetchall()
        if target_v2_prior_ids != [
            ("target-v2-rkpk-no-live-global-prior-v1",),
            ("target-v2-rkpk-no-live-global-prior-v2",),
        ] or target_v2_policy_scopes != [
            ("P0", "d1", 2, 2, "target-v2-rkpk-no-live-global-prior-v2"),
            ("P0", "d2", 2, 2, "target-v2-rkpk-no-live-global-prior-v2"),
            (
                "P0",
                "d2_fallback",
                2,
                2,
                "target-v2-rkpk-no-live-global-prior-v2",
            ),
            ("P1", "d1", 2, 2, "target-v2-rkpk-no-live-global-prior-v2"),
            ("P1", "d2", 2, 2, "target-v2-rkpk-no-live-global-prior-v2"),
            (
                "P1",
                "d2_fallback",
                2,
                2,
                "target-v2-rkpk-no-live-global-prior-v2",
            ),
        ]:
            raise RuntimeError(
                "rkpk_completion_target_v2_station_policy_missing"
            )
    return {
        "schema_current": True,
        "p0_v4_activation_current": True,
        "delta_v2_no_live_current": True,
        "target_v2_station_policy_current": True,
        "activation_generation": int(activation_row[0]),
        "activation_sha256": str(activation_row[1]),
        "capital_pool": {
            "balance_usdc": float(pool[0]),
            "reserved_usdc": float(pool[1]),
            "capital_epoch_id": str(pool[2]),
            "version": int(pool[3]),
        },
        "capital_migration_id": str(migration[0]),
        "capital_migration_sha256": str(migration[2]),
        "target_owned_only": True,
        "reads_shared_live_capital": False,
    }


def _select_p0(
    inbox_path: Path,
    *,
    station_id: str,
    event_date: str,
) -> dict[str, Any] | None:
    if not inbox_path.is_file():
        return None
    raw = load_bounded_json(inbox_path)
    if not isinstance(raw, Mapping):
        return None
    identity = {str(key): value for key, value in raw.items() if key != "content_sha256"}
    if (
        raw.get("schema") != "bayes_basket_target_p0_inbox_v1"
        or str(raw.get("content_sha256") or "") != content_sha256(identity)
    ):
        raise RuntimeError("rkpk_completion_p0_inbox_invalid")
    matches: list[dict[str, Any]] = []
    for wrapper in raw.get("packets") or []:
        if not isinstance(wrapper, Mapping) or not isinstance(
            wrapper.get("packet"), Mapping
        ):
            continue
        packet = dict(wrapper["packet"])
        ensemble_raw = packet.get("ensemble_artifact")
        if not isinstance(ensemble_raw, Mapping):
            continue
        ensemble = ModelEnsembleArtifact.from_mapping(ensemble_raw)
        if (
            str(packet.get("station_id") or "").upper(),
            str(packet.get("event_date") or ""),
            ensemble.lead_bucket,
            ensemble.run_cycle,
            ensemble.weight_method,
        ) != (
            station_id.upper(),
            event_date,
            "d2",
            "00Z",
            FORMAL_ENSEMBLE_METHOD_VERSION,
        ):
            continue
        models = sorted(
            str(row.get("model_id") or "").upper()
            for row in packet.get("current_model_runs") or []
            if isinstance(row, Mapping)
        )
        if models != ["ECMWF", "GFS"]:
            raise RuntimeError("rkpk_completion_p0_model_set_invalid")
        weights = ensemble.resolve_weights(models)
        if any(weight <= 0.0 for weight in weights.values()):
            raise RuntimeError("rkpk_completion_p0_weight_invalid")
        matches.append({
            "packet": packet,
            "weights": weights,
            "inbox": dict(raw),
        })
    if len(matches) > 1:
        raise RuntimeError("rkpk_completion_p0_exact_scope_not_unique")
    return matches[0] if matches else None


def _wait_for_p0(
    inbox_path: Path,
    *,
    station_id: str,
    event_date: str,
    deadline: float,
) -> dict[str, Any]:
    while True:
        assert_process_memory_budget()
        found = _select_p0(
            inbox_path,
            station_id=station_id,
            event_date=event_date,
        )
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            raise RuntimeError("rkpk_completion_p0_v4_wait_timeout")
        time.sleep(5.0)


def _latest_envelope(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    stage: str,
    root_checkpoint_id: str | None = None,
) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT checkpoint_id,decision_at_utc,posterior_version,"
        "probabilities_json,lineage_json,content_sha256 "
        "FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND stage=? "
        "AND state='COMMITTED' ORDER BY decision_at_utc DESC",
        (station_id, event_date, stage),
    ).fetchall()
    for row in rows:
        lineage = json.loads(str(row[4]))
        if (
            root_checkpoint_id is not None
            and str(lineage.get("p0_root_checkpoint_id") or "")
            != root_checkpoint_id
        ):
            continue
        return {
            "checkpoint_id": str(row[0]),
            "decision_at_utc": str(row[1]),
            "posterior_version": str(row[2]),
            "probabilities": json.loads(str(row[3])),
            "lineage": lineage,
            "content_sha256": str(row[5]),
        }
    return None


def _inspect_scope_authority(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
) -> dict[str, Any]:
    """Prove that one materialized scope has one economic authority.

    Execution-cycle rows are append-only audit facts.  A cycle is currently
    actionable only when its authoritative ``auto_trade_decisions`` row is
    also ``prepared``; historical cycles paired with blocked decisions must
    not be counted as competing candidates.
    """

    latest = conn.execute(
        "SELECT c.cycle_id,d.decision_id,d.decided_state,"
        "c.requested_reserve_usdc "
        "FROM basket_target_execution_cycles c "
        "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
        "WHERE c.station_id=? AND c.event_date=? "
        "AND c.settlement_timezone=d.settlement_timezone "
        "ORDER BY d.created_at DESC,d.decision_id DESC LIMIT 1",
        (station_id, event_date),
    ).fetchone()
    prepared = conn.execute(
        "SELECT c.cycle_id,d.decision_id,c.requested_reserve_usdc "
        "FROM basket_target_execution_cycles c "
        "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
        "WHERE c.station_id=? AND c.event_date=? "
        "AND c.settlement_timezone=d.settlement_timezone "
        "AND c.state='prepared' AND d.decided_state='prepared' "
        "ORDER BY d.created_at",
        (station_id, event_date),
    ).fetchall()
    if len(prepared) > 1:
        raise RuntimeError(
            "rkpk_completion_competing_prepared_authority"
        )
    blocked_audit_count = int(conn.execute(
        "SELECT count(*) FROM basket_target_execution_cycles c "
        "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
        "WHERE c.station_id=? AND c.event_date=? "
        "AND c.state='prepared' AND d.decided_state='blocked'",
        (station_id, event_date),
    ).fetchone()[0])
    cycle_id = str(latest[0]) if latest else None
    decision_id = str(latest[1]) if latest else None
    decided_state = str(latest[2]) if latest else None
    requested_reserve = float(latest[3]) if latest else 0.0
    active = conn.execute(
        "SELECT r.reservation_id,r.cycle_id,r.amount_usdc "
        "FROM basket_target_capital_reservations r "
        "JOIN basket_target_execution_cycles c ON c.cycle_id=r.cycle_id "
        "WHERE c.station_id=? AND c.event_date=? "
        "AND r.state='active' AND r.amount_usdc>0 "
        "ORDER BY r.created_at",
        (station_id, event_date),
    ).fetchall()
    if len(active) > 1:
        raise RuntimeError(
            "rkpk_completion_competing_active_reservation"
        )
    intents = conn.execute(
        "SELECT i.order_intent_id,i.cycle_id,i.requested_notional_usdc,"
        "i.live_submit_authorized "
        "FROM basket_target_order_intents i "
        "JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id "
        "WHERE c.station_id=? AND c.event_date=? AND c.cycle_id=? "
        "ORDER BY i.created_at",
        (station_id, event_date, cycle_id),
    ).fetchall()
    scope_fact_count = (
        int(conn.execute(
            "SELECT count(*) FROM basket_policy_training_fact "
            "WHERE station_id=? AND event_date=? AND decision_id=?",
            (station_id, event_date, decision_id),
        ).fetchone()[0])
        if decision_id is not None
        else 0
    )
    duplicate_scope_keys = int(conn.execute(
        "SELECT count(*) FROM ("
        "SELECT decision_scope_key FROM basket_policy_training_fact "
        "WHERE station_id=? AND event_date=? "
        "GROUP BY decision_scope_key HAVING count(*)>1)",
        (station_id, event_date),
    ).fetchone()[0])
    order_event_count = int(conn.execute(
        "SELECT count(*) FROM basket_target_order_events e "
        "JOIN basket_target_order_intents i "
        "ON i.order_intent_id=e.order_intent_id "
        "JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id "
        "WHERE c.station_id=? AND c.event_date=?",
        (station_id, event_date),
    ).fetchone()[0])
    execution_receipt_count = int(conn.execute(
        "SELECT count(*) FROM basket_target_execution_receipts r "
        "JOIN basket_target_execution_cycles c ON c.cycle_id=r.cycle_id "
        "WHERE c.station_id=? AND c.event_date=?",
        (station_id, event_date),
    ).fetchone()[0])
    intent_notional = sum(float(row[2]) for row in intents)
    unauthorized_intents = sum(int(row[3]) != 0 for row in intents)
    active_amount = float(active[0][2]) if active else 0.0
    active_cycle_id = str(active[0][1]) if active else None
    intent_cycle_ids = sorted({str(row[1]) for row in intents})
    prepared_authority_ok = bool(
        decided_state == "prepared"
        and len(prepared) == 1
        and str(prepared[0][0]) == cycle_id
        and len(active) == 1
        and len(intents) >= 1
        and cycle_id == active_cycle_id
        and intent_cycle_ids == [cycle_id]
        and abs(requested_reserve - active_amount) <= 1e-9
        and abs(intent_notional - active_amount) <= 1e-9
        and unauthorized_intents == 0
        and scope_fact_count == 1
        and duplicate_scope_keys == 0
        and order_event_count == 0
        and execution_receipt_count == 0
    )
    hold_authority_ok = bool(
        decided_state == "blocked"
        and cycle_id is not None
        and abs(requested_reserve) <= 1e-9
        and len(active) == 0
        and len(intents) == 0
        and scope_fact_count == 1
        and duplicate_scope_keys == 0
        and order_event_count == 0
        and execution_receipt_count == 0
    )
    materialized_authority_ok = prepared_authority_ok or hold_authority_ok
    return {
        "materialized_authority_ok": materialized_authority_ok,
        "authority_mode": (
            "PREPARED_CANDIDATE"
            if prepared_authority_ok
            else "HOLD"
            if hold_authority_ok
            else "INVALID"
        ),
        "latest_decided_state": decided_state,
        "nonblocked_prepared_count": len(prepared),
        "blocked_cycle_audit_count": blocked_audit_count,
        "cycle_id": cycle_id,
        "decision_id": decision_id,
        "active_positive_reservation_count": len(active),
        "active_reservation_id": (
            str(active[0][0]) if active else None
        ),
        "active_reservation_amount_usdc": active_amount,
        "intent_count": len(intents),
        "intent_notional_usdc": intent_notional,
        "unauthorized_intent_count": unauthorized_intents,
        "current_decision_training_fact_count": scope_fact_count,
        "duplicate_decision_scope_key_count": duplicate_scope_keys,
        "order_event_count": order_event_count,
        "execution_receipt_count": execution_receipt_count,
    }


def _chain_materialized(chain: Mapping[str, Any]) -> bool:
    """Accept either a coherent candidate or a coherent economic HOLD."""

    decision = chain.get("decision")
    authority = chain.get("authority_uniqueness")
    if (
        chain.get("p0") is None
        or chain.get("p0m") is None
        or chain.get("p1") is None
        or chain.get("training_fact") is None
        or not isinstance(decision, Mapping)
        or not isinstance(authority, Mapping)
        or authority.get("materialized_authority_ok") is not True
        or decision.get("will_order") is not False
    ):
        return False
    state = str(decision.get("decided_state") or "")
    intents = decision.get("intents")
    reservation = decision.get("reservation")
    if not isinstance(intents, list):
        return False
    if state == "prepared":
        return (
            decision.get("reason_codes") == []
            and len(intents) >= 1
            and isinstance(reservation, Mapping)
        )
    if state == "blocked":
        return len(intents) == 0 and reservation is None
    return False


def _observe_chain(
    database: Path,
    *,
    station_id: str,
    event_date: str,
    p0_decision_at_utc: str,
    deadline: float,
) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    while True:
        assert_process_memory_budget()
        with sqlite3.connect(
            f"file:{database.resolve()}?mode=ro",
            uri=True,
            timeout=30,
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            p0_rows = conn.execute(
                "SELECT checkpoint_id,decision_at_utc,posterior_version,"
                "probabilities_json,lineage_json,content_sha256 "
                "FROM basket_probability_envelope "
                "WHERE station_id=? AND event_date=? AND stage='P0' "
                "AND state='COMMITTED' AND decision_at_utc=? "
                "ORDER BY created_at DESC LIMIT 1",
                (station_id, event_date, p0_decision_at_utc),
            ).fetchone()
            p0 = None
            if p0_rows is not None:
                p0 = {
                    "checkpoint_id": str(p0_rows[0]),
                    "decision_at_utc": str(p0_rows[1]),
                    "posterior_version": str(p0_rows[2]),
                    "probabilities": json.loads(str(p0_rows[3])),
                    "lineage": json.loads(str(p0_rows[4])),
                    "content_sha256": str(p0_rows[5]),
                }
            p0m = (
                _latest_envelope(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                    stage="P0-M",
                    root_checkpoint_id=p0["checkpoint_id"],
                )
                if p0 is not None
                else None
            )
            p1 = (
                _latest_envelope(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                    stage="P1",
                    root_checkpoint_id=p0["checkpoint_id"],
                )
                if p0 is not None
                else None
            )
            selected = p1 or p0m
            fact = None
            decision = None
            if selected is not None:
                fact_row = conn.execute(
                    "SELECT fact_id,stage,probability_envelope_id,"
                    "ws_snapshot_id,quote_version,decision_id,"
                    "cycle_id,intent_count,policy_version_vector_json,"
                    "feature_json,content_sha256,inventory_snapshot_id,"
                    "order_state_snapshot_id "
                    "FROM basket_policy_training_fact "
                    "WHERE station_id=? AND event_date=? "
                    "AND probability_envelope_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (
                        station_id,
                        event_date,
                        selected["checkpoint_id"],
                    ),
                ).fetchone()
                if fact_row is not None:
                    vector = json.loads(str(fact_row[8]))
                    features = json.loads(str(fact_row[9]))
                    fact = {
                        "fact_id": str(fact_row[0]),
                        "stage": str(fact_row[1]),
                        "probability_envelope_id": str(fact_row[2]),
                        "ws_snapshot_id": str(fact_row[3]),
                        "quote_version": str(fact_row[4]),
                        "decision_id": str(fact_row[5]),
                        "cycle_id": str(fact_row[6]),
                        "intent_count": int(fact_row[7]),
                        "policy_version_vector": vector,
                        "features": features,
                        "content_sha256": str(fact_row[10]),
                        "inventory_snapshot_id": str(fact_row[11]),
                        "order_state_snapshot_id": str(fact_row[12]),
                    }
                    decision_row = conn.execute(
                        "SELECT decision_id,decided_state,will_order,"
                        "reason_codes_json,decision_sha256,details_json "
                        "FROM auto_trade_decisions WHERE decision_id=?",
                        (fact["decision_id"],),
                    ).fetchone()
                    if decision_row is not None:
                        intents = [
                            {
                                "order_intent_id": str(row[0]),
                                "bucket": str(row[1]),
                                "side": str(row[2]),
                                "order_action": str(row[3]),
                                "limit_price": float(row[4]),
                                "requested_size": float(row[5]),
                                "requested_notional_usdc": float(row[6]),
                                "state": str(row[7]),
                                "live_submit_authorized": bool(row[8]),
                            }
                            for row in conn.execute(
                                "SELECT order_intent_id,bucket,side,"
                                "order_action,limit_price,requested_size,"
                                "requested_notional_usdc,state,"
                                "live_submit_authorized "
                                "FROM basket_target_order_intents "
                                "WHERE cycle_id=? ORDER BY rowid",
                                (fact["cycle_id"],),
                            ).fetchall()
                        ]
                        reservation_row = conn.execute(
                            "SELECT reservation_id,amount_usdc,state,"
                            "release_reason FROM "
                            "basket_target_capital_reservations "
                            "WHERE cycle_id=?",
                            (fact["cycle_id"],),
                        ).fetchone()
                        label_row = conn.execute(
                            "SELECT label_id,truth_id,truth_revision_no,"
                            "official_tmax_c,winning_bucket,"
                            "settlement_closed_at_utc,authority_scope,"
                            "content_sha256 "
                            "FROM basket_policy_training_label "
                            "WHERE fact_id=? ORDER BY labeled_at_utc DESC "
                            "LIMIT 1",
                            (fact["fact_id"],),
                        ).fetchone()
                        plan = (
                            features.get("plan")
                            if isinstance(features, Mapping)
                            and isinstance(features.get("plan"), Mapping)
                            else {}
                        )
                        selected_actions = (
                            (
                                plan.get("delta_v2_action_selection") or {}
                            ).get("selected_actions", [])
                            if isinstance(
                                plan.get("delta_v2_action_selection"),
                                Mapping,
                            )
                            else []
                        )
                        decision = {
                            "decision_id": str(decision_row[0]),
                            "decided_state": str(decision_row[1]),
                            "will_order": bool(decision_row[2]),
                            "reason_codes": json.loads(str(decision_row[3])),
                            "decision_sha256": str(decision_row[4]),
                            "details": json.loads(str(decision_row[5])),
                            "intents": intents,
                            "reservation": (
                                {
                                    "reservation_id": str(
                                        reservation_row[0]
                                    ),
                                    "amount_usdc": float(
                                        reservation_row[1]
                                    ),
                                    "state": str(reservation_row[2]),
                                    "release_reason": reservation_row[3],
                                }
                                if reservation_row is not None
                                else None
                            ),
                            "basket": {
                                "center_bucket": plan.get("center_bucket"),
                                "direction": plan.get("direction"),
                                "candidate_support": plan.get(
                                    "candidate_support"
                                ),
                                "final_support": plan.get("final_support"),
                                "execution_gap_support": plan.get(
                                    "execution_gap_support"
                                ),
                                "selected_buckets": plan.get(
                                    "selected_buckets"
                                ),
                                "selected_actions": selected_actions,
                                "targets": plan.get("targets"),
                            },
                            "label": (
                                {
                                    "label_id": str(label_row[0]),
                                    "truth_id": str(label_row[1]),
                                    "truth_revision_no": int(label_row[2]),
                                    "official_tmax_c": float(label_row[3]),
                                    "winning_bucket": str(label_row[4]),
                                    "settlement_closed_at_utc": str(
                                        label_row[5]
                                    ),
                                    "authority_scope": str(label_row[6]),
                                    "content_sha256": str(label_row[7]),
                                }
                                if label_row is not None
                                else None
                            ),
                        }
            latest = {
                "p0": p0,
                "p0m": p0m,
                "p1": p1,
                "training_fact": fact,
                "decision": decision,
                "authority_uniqueness": _inspect_scope_authority(
                    conn,
                    station_id=station_id,
                    event_date=event_date,
                ),
            }
        if _chain_materialized(latest):
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(5.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--station-id", default="RKPK")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args()
    if args.wait_seconds < 0 or args.wait_seconds > 1800:
        raise SystemExit("wait-seconds must be in [0,1800]")
    station = args.station_id.strip().upper()
    deadline = time.monotonic() + args.wait_seconds
    preconditions = _preconditions(args.db)
    p0 = _wait_for_p0(
        P0_INBOX,
        station_id=station,
        event_date=args.event_date,
        deadline=deadline,
    )
    report = build_transport_oos_report(
        exact_join_path=EXACT_JOIN,
        crossfit_path=CROSSFIT,
        live_p0_inbox_path=P0_INBOX,
        taf_artifact_path=TAF_ARTIFACT,
        allowed_evidence_root=ROOT / "output" / "audits",
        station_id=station,
        season_bin="summer",
        lead_bucket="d2",
        run_cycle="00Z",
        history_prefix_taf_path=PREFIX_TAF,
        history_prefix_evidence_paths=(PREFIX_ECMWF, PREFIX_GFS),
        minimum_sample_n=24,
    )
    write_bounded_json_if_changed(TRANSPORT_REPORT, report)
    if (
        report.get("state") != "PASS"
        or report.get("validated") is not True
        or not isinstance(report.get("evidence"), Mapping)
    ):
        raise RuntimeError(
            "rkpk_completion_transport_oos_not_passed:"
            f"{report.get('gaps')!r}"
        )
    artifact_raw = load_bounded_json(TAF_ARTIFACT)
    if not isinstance(artifact_raw, Mapping):
        raise RuntimeError("rkpk_completion_taf_artifact_invalid")
    bound = bind_taf_artifact_to_live_nwp_compatibility(
        taf_artifact=artifact_raw,
        live_fingerprint=report["live_fingerprint"],
        transport_oos_evidence=report["evidence"],
    )
    write_bounded_json_if_changed(BOUND_TAF, bound.to_dict())
    published = refresh_resident_taf_inputs(
        source_inbox_paths=(TAF_INBOX,),
        artifact_path=BOUND_TAF,
        output_inbox_path=TAF_INBOX,
        output_artifact_path=ACTIVE_TAF,
        as_of_utc=_now(),
    )
    write_bounded_json_if_changed(PUBLISH_RECEIPT, published)
    chain = _observe_chain(
        args.db,
        station_id=station,
        event_date=args.event_date,
        p0_decision_at_utc=str(p0["packet"]["decision_at_utc"]),
        deadline=deadline,
    )
    materialized = _chain_materialized(chain)
    labeled = bool(
        materialized and chain["decision"]["label"] is not None
    )
    identity = {
        "schema": "rkpk_vertical_slice_no_live_completion_receipt_v2",
        "state": (
            "COMPLETE"
            if labeled
            else (
                "MATERIALIZED_RUNNING_WAITING_LABEL"
                if materialized
                else "WAITING_RESIDENT_OUTPUT"
            )
        ),
        "observed_at_utc": _now(),
        "station_id": station,
        "event_date": args.event_date,
        "preconditions": preconditions,
        "p0_weights": p0["weights"],
        "p0_decision_at_utc": p0["packet"]["decision_at_utc"],
        "transport_report_sha256": report["content_sha256"],
        "transport_sample_n": report["sample_n"],
        "taf_artifact_sha256": bound.content_sha256,
        "publish": published,
        "chain": chain,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    receipt = {**identity, "content_sha256": content_sha256(identity)}
    write_bounded_json(FINAL_RECEIPT, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if materialized else 3


if __name__ == "__main__":
    raise SystemExit(main())
