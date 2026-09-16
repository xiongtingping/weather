#!/usr/bin/env python3
"""M3-03 isolated P0 historical-meta rehearsal.

This is a disposable rehearsal only.  It clones the target SSOT with SQLite's
online backup, builds P0 from the retained native decode and registry, and
atomically commits the v4 theta=0 proof together with the probability envelope
and X->S->B bridge into the clone.  It never opens the production database for
writing and never calls an order gateway.

The native decode in the current workspace was produced against an older
registry snapshot.  When that snapshot is still present in the cloned SSOT,
the rehearsal proves the exact station-day market rows are unchanged and
re-seals a temporary decode receipt against the current registry snapshot.
This is a lineage-preserving temporary input normalization, not a data or
freshness rewrite.  Any row mismatch fails closed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.p0_producer import build_packets  # noqa: E402
from bayes_basket_target.probability_bridge import (  # noqa: E402
    install_probability_bridge_schema,
    materialize_probability_bridge,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    apply_probability_evidence_schema,
    insert_probability_update_proof,
)
from bayes_basket_target.probability_packet import (  # noqa: E402
    build_probability_envelope,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MIN_TARGET_FREE_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


SCHEMA = "bayes_basket_target_m3_03_p0_historical_meta_rehearsal_v1"
DEFAULT_SSOT = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_DECODE = (
    ROOT
    / "data/runtime/bayes_basket_target_nwp/compiled/2026-07-30/native_decode.json"
)
DEFAULT_STATE = (
    ROOT
    / "data/runtime/bayes_basket_target_nwp/compiled/2026-07-30/state.json"
)
DEFAULT_REGISTRY = (
    ROOT / "data/runtime/bayes_basket_target_nwp/market_registry_forward.json"
)
MAX_REHEARSAL_DB_BYTES = max(int(MAX_TARGET_DB_BYTES), 512 * 1024 * 1024)
PROOF_KIND = "P0_HISTORICAL_TILT"


def _timestamp(value: Any, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"m303_{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"m303_{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _quick_check(conn: sqlite3.Connection, *, name: str) -> None:
    rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    if rows != ["ok"]:
        raise ContractError(f"m303_{name}_quick_check_failed")


def _free_bytes(path: Path) -> int:
    stat = os.statvfs(path)
    return int(stat.f_bavail) * int(stat.f_frsize)


def _online_backup(source: Path, target: Path) -> None:
    source = source.expanduser().resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise ContractError("m303_source_database_invalid")
    source_size = int(source.stat().st_size)
    if source_size <= 0 or source_size > MAX_REHEARSAL_DB_BYTES:
        raise ContractError("m303_source_database_size_invalid")
    if _free_bytes(target.parent) < MIN_TARGET_FREE_BYTES + 3 * source_size:
        raise ContractError("m303_rehearsal_free_space_insufficient")
    with sqlite3.connect(
        f"file:{source}?mode=ro", uri=True, timeout=60.0
    ) as source_conn, sqlite3.connect(target, timeout=60.0) as target_conn:
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.execute("PRAGMA busy_timeout=60000")
        _quick_check(source_conn, name="source")
        source_conn.backup(target_conn, pages=256, sleep=0.05)
        target_conn.commit()
        _quick_check(target_conn, name="clone")
    if int(target.stat().st_size) > MAX_REHEARSAL_DB_BYTES:
        raise ContractError("m303_clone_database_size_invalid")


def _curve_scope(decode: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    container = decode.get("curves")
    rows = container.get("curves") if isinstance(container, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise ContractError("m303_native_curves_missing")
    dates = {
        str(row.get("event_date") or "")
        for row in rows
        if isinstance(row, Mapping)
    }
    stations = {
        str(row.get("station_id") or "").strip().upper()
        for row in rows
        if isinstance(row, Mapping)
    }
    if len(dates) != 1 or "" in dates or len(stations) != 1 or "" in stations:
        raise ContractError("m303_native_curve_scope_invalid")
    return next(iter(dates)), (next(iter(stations)),)


def _registry_row_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    # refreshed_at and the snapshot foreign key are catalog metadata.  All
    # market/token/rule/settlement fields remain part of the equality proof.
    return {
        str(key): value
        for key, value in row.items()
        if str(key) not in {"snapshot_sha256", "refreshed_at"}
    }


def _rows_from_current_registry(
    registry: Mapping[str, Any], *, station_id: str, event_date: str
) -> list[dict[str, Any]]:
    rows = registry.get("rows")
    if not isinstance(rows, list):
        raise ContractError("m303_registry_rows_missing")
    selected = [
        _registry_row_identity(row)
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("station_id") or "").strip().upper() == station_id
        and str(row.get("event_date") or "") == event_date
    ]
    if not selected:
        raise ContractError("m303_registry_scope_missing")
    return sorted(selected, key=lambda row: (str(row.get("outcome")), str(row.get("slug"))))


def _rows_from_historical_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_sha256: str,
    station_id: str,
    event_date: str,
) -> list[dict[str, Any]]:
    snapshot = conn.execute(
        "SELECT snapshot_sha256 FROM basket_target_market_registry_snapshot "
        "WHERE snapshot_sha256=?",
        (snapshot_sha256,),
    ).fetchone()
    if snapshot is None:
        raise ContractError("m303_native_registry_snapshot_not_retained")
    rows = conn.execute(
        "SELECT * FROM basket_target_market_registry WHERE snapshot_sha256=? "
        "AND station_id=? AND event_date=?",
        (snapshot_sha256, station_id, event_date),
    ).fetchall()
    if not rows:
        raise ContractError("m303_native_registry_scope_not_retained")
    names = [str(item[1]) for item in conn.execute(
        "PRAGMA table_info(basket_target_market_registry)"
    )]
    return sorted(
        [_registry_row_identity(dict(zip(names, row))) for row in rows],
        key=lambda row: (str(row.get("outcome")), str(row.get("slug"))),
    )


def _rebind_decode_if_needed(
    conn: sqlite3.Connection,
    *,
    decode: Mapping[str, Any],
    registry: Mapping[str, Any],
    event_date: str,
    station_id: str,
    destination: Path,
) -> tuple[Path, dict[str, Any]]:
    declared = str(decode.get("market_registry_snapshot_sha256") or "")
    current = str(registry.get("snapshot_sha256") or "")
    if len(current) != 64:
        raise ContractError("m303_registry_snapshot_sha_invalid")
    if declared == current:
        write_bounded_json_if_changed(destination, dict(decode))
        return destination, {
            "state": "EXACT_INPUT_SNAPSHOT",
            "from_snapshot_sha256": declared,
            "to_snapshot_sha256": current,
            "row_equality_checked": True,
        }
    if len(declared) != 64:
        raise ContractError("m303_native_snapshot_sha_invalid")
    historical = _rows_from_historical_snapshot(
        conn,
        snapshot_sha256=declared,
        station_id=station_id,
        event_date=event_date,
    )
    current_rows = _rows_from_current_registry(
        registry, station_id=station_id, event_date=event_date
    )
    if historical != current_rows:
        raise ContractError("m303_registry_station_day_contract_changed")
    rebound = dict(decode)
    rebound["market_registry_snapshot_sha256"] = current
    evidence_identity = {
        key: value
        for key, value in rebound.items()
        if key not in {"evidence_sha256", "receipt_sha256"}
    }
    rebound["evidence_sha256"] = content_sha256(evidence_identity)
    rebound["receipt_sha256"] = content_sha256(
        {key: value for key, value in rebound.items() if key != "receipt_sha256"}
    )
    write_bounded_json_if_changed(destination, rebound)
    return destination, {
        "state": "TEMPORARY_LINEAGE_REBIND",
        "from_snapshot_sha256": declared,
        "to_snapshot_sha256": current,
        "row_equality_checked": True,
        "historical_row_count": len(historical),
        "current_row_count": len(current_rows),
        "source_receipt_sha256": str(decode.get("receipt_sha256") or ""),
        "rebound_receipt_sha256": rebound["receipt_sha256"],
    }


def _validate_source_clock(
    decode: Mapping[str, Any], state: Mapping[str, Any]
) -> tuple[str, str]:
    decision = _timestamp(decode.get("as_of_utc"), name="source_as_of_utc")
    if str(state.get("native_decode_receipt_sha256") or "") != str(
        decode.get("receipt_sha256") or ""
    ):
        raise ContractError("m303_state_decode_receipt_mismatch")
    if str(state.get("materialized_at_utc") or "") != str(
        decode.get("as_of_utc") or ""
    ):
        raise ContractError("m303_state_decision_clock_mismatch")
    fresh_until = _timestamp(state.get("fresh_until_utc"), name="fresh_until_utc")
    if fresh_until <= decision:
        raise ContractError("m303_source_freshness_expired")
    return _iso(decision), _iso(fresh_until)


def _proof_row(
    envelope: ProbabilityEnvelope, proof: Mapping[str, Any]
) -> dict[str, Any]:
    proof_id = str(proof.get("proof_id") or "")
    proof_sha = str(proof.get("content_sha256") or "")
    if not proof_id or len(proof_sha) != 64:
        raise ContractError("m303_historical_proof_invalid")
    return {
        "proof_id": proof_id,
        "proof_kind": PROOF_KIND,
        "station_id": envelope.station_id,
        "event_date": envelope.event_date,
        "settlement_timezone": envelope.settlement_timezone,
        "prediction_root_checkpoint_id": envelope.checkpoint_id,
        "artifact_id_or_none": str(proof.get("meta_artifact_id") or "NONE"),
        "activation_id_or_none": str(proof.get("meta_activation_id") or "NONE"),
        "canonical_sequence_sha256_or_none": "NONE",
        "decision_at_utc": str(proof.get("as_of_utc") or envelope.decision_at_utc),
        "proof": dict(proof),
        "content_sha256": proof_sha,
    }


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "basket_probability_envelope",
        "basket_probability_bridge_history",
        "basket_probability_bridge_latest",
        "basket_probability_bridge_bucket_probability",
        "basket_probability_update_proof",
    )
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def _reset_temporary_probability_scope(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> int:
    """Isolate one exact scope in the disposable clone only.

    The source SSOT already contains an older P0 at the same source clock.
    Production correctly rejects a second authority at that instant.  A
    rehearsal must therefore start this exact station-day probability scope
    empty; no source or production file is ever opened for this DELETE.
    """

    before = int(conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ).fetchone()[0])
    # The production clone inherits the append-only trigger.  Temporarily
    # remove only the DELETE trigger, clear this rehearsal scope, and restore
    # the identical trigger before any M3-03 write is exercised.
    conn.execute("DROP TRIGGER IF EXISTS basket_probability_envelope_no_delete")
    conn.execute(
        "DELETE FROM basket_probability_envelope WHERE station_id=? "
        "AND event_date=?",
        (station_id, event_date),
    )
    conn.execute(
        "CREATE TRIGGER basket_probability_envelope_no_delete "
        "BEFORE DELETE ON basket_probability_envelope BEGIN "
        "SELECT RAISE(ABORT,'append_only'); END"
    )
    after = int(conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=?",
        (station_id, event_date),
    ).fetchone()[0])
    if after != 0:
        raise ContractError("m303_temporary_probability_scope_not_empty")
    return before


def _commit_envelope_and_proof(
    conn: sqlite3.Connection,
    envelope: ProbabilityEnvelope,
    proof: Mapping[str, Any],
    *,
    fail_after_proof: bool = False,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    row = _proof_row(envelope, proof)
    conn.execute("BEGIN IMMEDIATE")
    try:
        bridge = materialize_probability_bridge(
            conn,
            envelope,
            created_at_utc=envelope.decision_at_utc,
            failure_injector=failure_injector,
        )
        proof_receipt = insert_probability_update_proof(conn, row)
        if fail_after_proof:
            raise RuntimeError("m303_injected_failure_after_proof")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {
        "bridge": bridge,
        "proof": proof_receipt,
        "wrote": bool(
            bridge.get("wrote") or proof_receipt.get("wrote")
        ),
    }


def _older_envelope(envelope: ProbabilityEnvelope) -> ProbabilityEnvelope:
    decision = _timestamp(envelope.decision_at_utc, name="decision_at_utc")
    fresh = _timestamp(envelope.fresh_until_utc, name="fresh_until_utc")
    raw = envelope.to_dict()
    raw["checkpoint_id"] = envelope.checkpoint_id + "-older"
    raw["decision_at_utc"] = _iso(decision - timedelta(minutes=1))
    raw["fresh_until_utc"] = _iso(fresh - timedelta(minutes=1))
    return ProbabilityEnvelope.from_mapping(raw)


def run_rehearsal(
    *,
    ssot_path: Path = DEFAULT_SSOT,
    decode_path: Path = DEFAULT_DECODE,
    state_path: Path = DEFAULT_STATE,
    registry_path: Path = DEFAULT_REGISTRY,
    event_date: str | None = None,
) -> dict[str, Any]:
    source_db = ssot_path.expanduser().resolve(strict=True)
    decode = load_bounded_json(decode_path.expanduser().resolve(strict=True))
    state = load_bounded_json(state_path.expanduser().resolve(strict=True))
    registry = load_bounded_json(registry_path.expanduser().resolve(strict=True))
    if not all(isinstance(item, Mapping) for item in (decode, state, registry)):
        raise ContractError("m303_input_mapping_required")
    decision_at_utc, fresh_until_utc = _validate_source_clock(decode, state)
    inferred_date, (station_id,) = _curve_scope(decode)
    target_date = str(event_date or inferred_date)
    if target_date != inferred_date:
        raise ContractError("m303_event_date_source_mismatch")

    temp_parent = Path("/private/tmp")
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_root_string = ""
    cleanup_confirmed = False
    with tempfile.TemporaryDirectory(
        prefix="bayes-target-m303-", dir=str(temp_parent)
    ) as raw_temp:
        temp_root = Path(raw_temp)
        temp_root_string = str(temp_root)
        clone = temp_root / "ssot-clone.db"
        rebound_decode = temp_root / "native_decode.rebound.json"
        output_dir = temp_root / "p0-output"
        inbox_path = temp_root / "p0-inbox.json"
        # A scope the current registry no longer carries cannot be rebound
        # whatever the clone holds, so say so before cloning the store: the
        # backup needs three times the SSOT free, and the answer was
        # "free space insufficient" instead of the true refusal.
        if str(decode.get("market_registry_snapshot_sha256") or "") != str(
            registry.get("snapshot_sha256") or ""
        ):
            _rows_from_current_registry(
                registry, station_id=station_id, event_date=target_date
            )
        _online_backup(source_db, clone)
        with sqlite3.connect(clone, timeout=60.0) as conn:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA foreign_keys=ON")
            probability_schema = apply_probability_evidence_schema(
                conn, commit=True
            )
            bridge_schema = install_probability_bridge_schema(conn, commit=True)
            isolated_rows = _reset_temporary_probability_scope(
                conn, station_id=station_id, event_date=target_date
            )
            conn.commit()
            rebound_path, lineage = _rebind_decode_if_needed(
                conn,
                decode=decode,
                registry=registry,
                event_date=target_date,
                station_id=station_id,
                destination=rebound_decode,
            )
            build = build_packets(
                db_path=clone,
                decode_evidence_path=rebound_path,
                registry_snapshot_path=registry_path,
                event_date=target_date,
                decision_at_utc=decision_at_utc,
                fresh_until_utc=fresh_until_utc,
                output_dir=output_dir,
                inbox_path=inbox_path,
                emit_packet_files=True,
            )
            if int(build.get("packet_count") or 0) != 1:
                raise ContractError("m303_expected_one_p0_packet")
            if int(build.get("blocked_count") or 0) != 0:
                raise ContractError("m303_p0_packet_blocked")
            packet_path = output_dir / f"{station_id}_{target_date}_p0.json"
            packet = load_bounded_json(packet_path)
            if not isinstance(packet, Mapping):
                raise ContractError("m303_p0_packet_invalid")
            proof = packet.get("p0_historical_tilt_proof")
            if not isinstance(proof, Mapping):
                raise ContractError("m303_p0_historical_proof_missing")
            before = proof.get("probabilities_before")
            after = proof.get("probabilities_after")
            if before != after:
                raise ContractError("m303_theta_zero_probability_changed")
            if proof.get("theta_zero_fallback") is not True:
                raise ContractError("m303_theta_zero_fallback_missing")
            envelope = build_probability_envelope(packet)
            if not isinstance(envelope, ProbabilityEnvelope):
                raise ContractError("m303_p0_envelope_invalid")
            ensemble = packet.get("ensemble_artifact")
            if not isinstance(ensemble, Mapping):
                raise ContractError("m303_ensemble_missing")
            models = ensemble.get("candidate_models")
            masks = ensemble.get("supported_masks")
            if (
                not isinstance(models, list)
                or len(models) < 2
                or len(set(map(str, models))) != len(models)
                or not isinstance(masks, Mapping)
                or len(masks) != 1
            ):
                raise ContractError("m303_formal_ensemble_model_count_invalid")
            weights = next(iter(masks.values()))
            if (
                not isinstance(weights, Mapping)
                or set(map(str, weights)) != set(map(str, models))
                or any(float(value) <= 0.0 for value in weights.values())
                or abs(sum(float(value) for value in weights.values()) - 1.0)
                > 1e-9
            ):
                raise ContractError("m303_formal_ensemble_weights_invalid")
            counts_before = _counts(conn)
            first = _commit_envelope_and_proof(conn, envelope, proof)
            counts_after_first = _counts(conn)
            second = _commit_envelope_and_proof(conn, envelope, proof)
            counts_after_second = _counts(conn)
            if counts_after_second != counts_after_first:
                raise ContractError("m303_duplicate_replay_wrote_rows")

            rollback_before = _counts(conn)
            try:
                _commit_envelope_and_proof(
                    conn,
                    envelope,
                    proof,
                    fail_after_proof=True,
                )
            except RuntimeError as exc:
                if str(exc) != "m303_injected_failure_after_proof":
                    raise
            if _counts(conn) != rollback_before:
                raise ContractError("m303_failure_after_proof_not_rolled_back")

            def fail_at_history(point: str) -> None:
                if point == "history":
                    raise RuntimeError("m303_injected_failure_at_history")

            try:
                _commit_envelope_and_proof(
                    conn,
                    envelope,
                    proof,
                    failure_injector=fail_at_history,
                )
            except RuntimeError as exc:
                if str(exc) != "m303_injected_failure_at_history":
                    raise
            if _counts(conn) != rollback_before:
                raise ContractError("m303_failure_at_history_not_rolled_back")

            older = _older_envelope(envelope)
            conn.execute("BEGIN IMMEDIATE")
            try:
                older_receipt = materialize_probability_bridge(
                    conn,
                    older,
                    created_at_utc=older.decision_at_utc,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            latest = conn.execute(
                "SELECT stage,checkpoint_id,decision_at_utc "
                "FROM basket_probability_bridge_latest WHERE station_id=? "
                "AND event_date=?",
                (envelope.station_id, envelope.event_date),
            ).fetchone()
            if latest is None or str(latest[1]) != envelope.checkpoint_id:
                raise ContractError("m303_out_of_order_lowered_authority")
            _quick_check(conn, name="clone_final")
            counts_final = _counts(conn)
    cleanup_confirmed = not Path(temp_root_string).exists()
    if not cleanup_confirmed:
        raise ContractError("m303_temporary_cleanup_failed")
    return {
        "schema": SCHEMA,
        "state": "REHEARSED",
        "event_date": target_date,
        "station_id": station_id,
        "decision_at_utc": decision_at_utc,
        "fresh_until_utc": fresh_until_utc,
        "input_lineage": lineage,
        "packet_count": int(build["packet_count"]),
        "temporary_scope_reset_rows": isolated_rows,
        "posterior_version": envelope.posterior_version,
        "model_count": len(models),
        "model_ids": list(models),
        "weights": {str(key): float(value) for key, value in weights.items()},
        "theta_zero_fallback": proof["theta_zero_fallback"],
        "probabilities_before_after_equal": before == after,
        "proof_id": proof["proof_id"],
        "proof_sha256": proof["content_sha256"],
        "envelope_checkpoint_id": envelope.checkpoint_id,
        "envelope_content_sha256": envelope.content_sha256,
        "transaction": {
            "counts_before": counts_before,
            "first": first,
            "counts_after_first": counts_after_first,
            "second_replay": second,
            "counts_after_second": counts_after_second,
            "failure_after_proof_rolled_back": True,
            "failure_at_history_rolled_back": True,
            "out_of_order_lower_stage_preserved_latest": True,
            "older_replay": older_receipt,
            "counts_final": counts_final,
        },
        "boundary": {
            "writes_real_database": False,
            "writes_target_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "forward_oos_blocks_no_live": False,
        },
        "temporary_cleanup_confirmed": cleanup_confirmed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssot", type=Path, default=DEFAULT_SSOT)
    parser.add_argument("--decode", type=Path, default=DEFAULT_DECODE)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--event-date", default=None)
    args = parser.parse_args()
    try:
        result = run_rehearsal(
            ssot_path=args.ssot,
            decode_path=args.decode,
            state_path=args.state,
            registry_path=args.registry,
            event_date=args.event_date,
        )
    except Exception as exc:
        result = {
            "schema": SCHEMA,
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "boundary": {
                "writes_real_database": False,
                "writes_target_database": False,
                "calls_order_gateway": False,
                "places_orders": False,
                "live_submit_authorized": False,
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
