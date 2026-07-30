#!/usr/bin/env python3
"""Rehearse 45-station no-live expansion on disposable SQLite clones."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.artifact_ingress import (  # noqa: E402
    ingest_artifact_bundle,
)
from bayes_basket_target.batch_expansion_gate import (  # noqa: E402
    evaluate_45station_no_live_expansion,
)
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.probability_bridge import (  # noqa: E402
    backfill_probability_bridges,
    install_probability_bridge_schema,
)
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    apply_probability_evidence_schema,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_TARGET_DB_BYTES,
    MIN_TARGET_FREE_BYTES,
    load_bounded_json,
)


SCHEMA = "bayes_basket_target_45station_expansion_rehearsal_v1"
DEFAULT_SSOT = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_FORWARD = (
    ROOT
    / "data/runtime/bayes_basket_target_cash_phase"
    / "forward_observations.sqlite3"
)
DEFAULT_CANDIDATE_PACKET = (
    ROOT
    / "output/audits"
    / "bayes_basket_target_no_live_candidate_artifacts_v2.json"
)
DEFAULT_SELECTION_MANIFEST = (
    ROOT
    / "output/audits"
    / "bayes_basket_target_45station_explicit_18z_d2_oos_min20_holdout8_v4"
    / "manifest.json"
)
MAX_FORWARD_STORE_BYTES = 256 * 1024 * 1024


def _quick_check(conn: sqlite3.Connection, *, name: str) -> None:
    if [str(row[0]) for row in conn.execute("PRAGMA quick_check")] != ["ok"]:
        raise ContractError(f"{name}_quick_check_failed")


def _clone(source: Path, target: Path, *, max_bytes: int) -> None:
    raw_source = source.expanduser()
    if raw_source.is_symlink():
        raise ContractError("expansion_rehearsal_source_database_invalid")
    resolved = raw_source.resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.stat().st_size > max_bytes
    ):
        raise ContractError("expansion_rehearsal_source_database_invalid")
    with sqlite3.connect(
        f"file:{resolved}?mode=ro",
        uri=True,
        timeout=60.0,
    ) as source_conn, sqlite3.connect(target, timeout=60.0) as target_conn:
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.execute("PRAGMA busy_timeout=60000")
        _quick_check(source_conn, name="source")
        source_conn.backup(target_conn)
        target_conn.commit()
        _quick_check(target_conn, name="clone")


def _eligible_stations(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    rows = manifest.get("station_results")
    if not isinstance(rows, list) or len(rows) != 45:
        raise ContractError("expansion_rehearsal_selection_manifest_invalid")
    eligible: set[str] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ContractError(
                "expansion_rehearsal_selection_manifest_row_invalid"
            )
        station = str(raw.get("station_id") or "").strip().upper()
        selected = raw.get("selected_models")
        if (
            raw.get("p0_no_live_binding_review") == "ELIGIBLE"
            and isinstance(selected, list)
            and len(set(map(str, selected))) >= 2
        ):
            eligible.add(station)
    return tuple(sorted(eligible))


def _packet_bound_stations(
    *,
    packet: Mapping[str, Any],
    build_manifest: Mapping[str, Any],
    selection_manifest: Mapping[str, Any],
    selection_manifest_path: Path,
) -> tuple[str, ...]:
    """Bind one packet to the exact OOS/build generation that emitted it."""

    build_identity = {
        key: value
        for key, value in build_manifest.items()
        if key != "content_sha256"
    }
    if (
        build_manifest.get("schema")
        != "bayes_basket_target_45station_18z_no_live_packet_build_v1"
        or build_manifest.get("content_sha256")
        != content_sha256(build_identity)
    ):
        raise ContractError(
            "expansion_rehearsal_packet_build_manifest_invalid"
        )
    if (
        str(build_manifest.get("source_manifest") or "")
        != str(selection_manifest_path.resolve())
        or str(
            build_manifest.get("source_manifest_content_sha256") or ""
        )
        != str(selection_manifest.get("content_sha256") or "")
    ):
        raise ContractError(
            "expansion_rehearsal_packet_selection_generation_mismatch"
        )
    rows = build_manifest.get("station_states")
    if not isinstance(rows, list) or len(rows) != 45:
        raise ContractError(
            "expansion_rehearsal_packet_build_station_states_invalid"
        )
    ready = tuple(sorted(
        str(row.get("station_id") or "").strip().upper()
        for row in rows
        if isinstance(row, Mapping)
        and row.get("state") == "ACTIVATION_PACKET_READY"
    ))
    packet_stations = packet.get("station_ids")
    if (
        not isinstance(packet_stations, list)
        or tuple(sorted(map(str, packet_stations))) != ready
        or int(
            build_manifest.get("activation_packet_ready_station_count")
            or -1
        )
        != len(ready)
    ):
        raise ContractError(
            "expansion_rehearsal_packet_station_generation_mismatch"
        )
    return ready


def _model_bound_stations(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT DISTINCT s.station_id,s.selected_models_json "
        "FROM calibration_ensemble_selection_evidence s "
        "JOIN calibration_ensemble_artifact e "
        "ON e.selection_evidence_sha256=s.content_sha256 "
        "JOIN calibration_activation a "
        "ON a.artifact_kind='MODEL_ENSEMBLE' "
        "AND a.artifact_id=e.ensemble_artifact_id "
        "AND a.station_id=e.station_id "
        "WHERE a.authority_scope='PRODUCTION_NO_LIVE_ONLY'"
    ).fetchall()
    result: set[str] = set()
    for station, raw_models in rows:
        try:
            models = json.loads(str(raw_models))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(models, list)
            and len(models) == len(set(map(str, models)))
            and len(models) >= 2
        ):
            result.add(str(station).strip().upper())
    return tuple(sorted(result))


def run_rehearsal(
    *,
    ssot_path: Path,
    forward_store_path: Path,
    candidate_packet_path: Path,
    selection_manifest_path: Path,
    artifact_packet_path: Path | None = None,
) -> dict[str, Any]:
    ssot = ssot_path.expanduser().resolve(strict=True)
    forward = forward_store_path.expanduser().resolve(strict=True)
    required_free = (
        MIN_TARGET_FREE_BYTES
        + 2 * int(ssot.stat().st_size)
        + 2 * int(forward.stat().st_size)
    )
    free_before = (
        int(os.statvfs("/private/tmp").f_bavail)
        * int(os.statvfs("/private/tmp").f_frsize)
    )
    if free_before < required_free:
        raise ContractError("expansion_rehearsal_free_space_insufficient")
    candidate_packet = load_bounded_json(
        candidate_packet_path.expanduser().resolve(strict=True)
    )
    selection_manifest = load_bounded_json(
        selection_manifest_path.expanduser().resolve(strict=True)
    )
    if not isinstance(candidate_packet, Mapping):
        raise ContractError("expansion_rehearsal_candidate_packet_invalid")
    if not isinstance(selection_manifest, Mapping):
        raise ContractError("expansion_rehearsal_selection_manifest_invalid")
    eligible = _eligible_stations(selection_manifest)
    packet_build_manifest: Mapping[str, Any] | None = None

    with tempfile.TemporaryDirectory(
        prefix="bayes-target-45station-rehearsal-",
        dir="/private/tmp",
    ) as raw_temp:
        temp_root = Path(raw_temp)
        ssot_clone = temp_root / "target-clone.db"
        forward_clone = temp_root / "forward-clone.db"
        _clone(ssot, ssot_clone, max_bytes=MAX_TARGET_DB_BYTES)
        _clone(
            forward,
            forward_clone,
            max_bytes=MAX_FORWARD_STORE_BYTES,
        )
        with sqlite3.connect(ssot_clone, timeout=60.0) as ssot_conn:
            ssot_conn.execute("PRAGMA busy_timeout=60000")
            probability_schema = apply_probability_evidence_schema(
                ssot_conn,
                commit=True,
            )
            probability_bridge_schema = install_probability_bridge_schema(
                ssot_conn,
                commit=True,
            )
            bridge_backfill = backfill_probability_bridges(ssot_conn)
            ssot_conn.commit()
            before_bound = _model_bound_stations(ssot_conn)
            if artifact_packet_path is None:
                ingress = {
                    "state": "NOT_REQUESTED",
                    "counts": {},
                }
            else:
                resolved_artifact_packet = (
                    artifact_packet_path.expanduser().resolve(strict=True)
                )
                packet = load_bounded_json(resolved_artifact_packet)
                if not isinstance(packet, Mapping):
                    raise ContractError(
                        "expansion_rehearsal_artifact_packet_invalid"
                    )
                raw_build_manifest = load_bounded_json(
                    resolved_artifact_packet.with_name("manifest.json")
                )
                if not isinstance(raw_build_manifest, Mapping):
                    raise ContractError(
                        "expansion_rehearsal_packet_build_manifest_invalid"
                    )
                packet_build_manifest = raw_build_manifest
                eligible = _packet_bound_stations(
                    packet=packet,
                    build_manifest=packet_build_manifest,
                    selection_manifest=selection_manifest,
                    selection_manifest_path=(
                        selection_manifest_path.expanduser().resolve(
                            strict=True
                        )
                    ),
                )
                ingress = ingest_artifact_bundle(
                    ssot_conn,
                    packet,
                    commit=True,
                )
                replay = ingest_artifact_bundle(
                    ssot_conn,
                    packet,
                    commit=True,
                )
                if any(
                    int(value or 0) != 0
                    for value in (replay.get("counts") or {}).values()
                ):
                    raise ContractError(
                        "expansion_rehearsal_artifact_replay_not_idempotent"
                    )
                ingress = {
                    **ingress,
                    "idempotent_replay_counts": replay.get("counts") or {},
                }
            after_bound = _model_bound_stations(ssot_conn)
            ssot_conn.execute("PRAGMA query_only=ON")
            with sqlite3.connect(
                forward_clone,
                timeout=60.0,
            ) as forward_conn:
                forward_conn.execute("PRAGMA query_only=ON")
                gate = evaluate_45station_no_live_expansion(
                    ssot_conn,
                    forward_conn,
                    candidate_packet,
                )
            _quick_check(ssot_conn, name="ssot_clone_after")
        eligible_bound = tuple(
            sorted(set(eligible) & set(after_bound))
        )
        pending_eligible = tuple(
            sorted(set(eligible) - set(after_bound))
        )
        temporary_removed = False

    temporary_removed = not temp_root.exists()
    identity = {
        "schema": SCHEMA,
        "state": (
            "READY_FOR_CONTROLLED_EXPANSION_REHEARSAL"
            if (
                gate.get("state") == "READY"
                and eligible_bound
                and not pending_eligible
            )
            else "BLOCKED_FAIL_CLOSED"
        ),
        "gate_state": gate.get("state"),
        "gate_blockers": list(gate.get("blockers") or []),
        "eligible_station_ids": list(eligible),
        "eligible_station_count": len(eligible),
        "packet_build_manifest_content_sha256": (
            packet_build_manifest.get("content_sha256")
            if packet_build_manifest is not None
            else None
        ),
        "packet_generation_bound": packet_build_manifest is not None,
        "model_bound_station_ids_before": list(before_bound),
        "model_bound_station_ids_after": list(after_bound),
        "eligible_model_bound_station_ids": list(eligible_bound),
        "eligible_model_bound_station_count": len(eligible_bound),
        "pending_eligible_model_binding_station_ids": list(
            pending_eligible
        ),
        "resident_vertical_chain_station_ids": list(
            gate.get("resident_vertical_chain_station_ids") or []
        ),
        "resident_vertical_chain_station_count": int(
            gate.get("resident_vertical_chain_station_count") or 0
        ),
        "resident_materialization_complete": (
            gate.get("resident_materialization_state") == "COMPLETE"
        ),
        "artifact_ingress": ingress,
            "clone_probability_schema": probability_schema,
            "clone_probability_bridge_schema": probability_bridge_schema,
            "clone_probability_bridge_backfill": bridge_backfill,
        "temporary_clones_removed": temporary_removed,
        "resource_bounds": {
            "free_bytes_before": free_before,
            "required_free_bytes": required_free,
            "max_target_database_bytes": MAX_TARGET_DB_BYTES,
            "max_forward_store_bytes": MAX_FORWARD_STORE_BYTES,
        },
        "boundary": {
            "source_databases_opened_read_only": True,
            "writes_production_database": False,
            "writes_forward_store": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
            "execution_mode": "production_no_live",
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssot", type=Path, default=DEFAULT_SSOT)
    parser.add_argument(
        "--forward-store",
        type=Path,
        default=DEFAULT_FORWARD,
    )
    parser.add_argument(
        "--candidate-packet",
        type=Path,
        default=DEFAULT_CANDIDATE_PACKET,
    )
    parser.add_argument(
        "--selection-manifest",
        type=Path,
        default=DEFAULT_SELECTION_MANIFEST,
    )
    parser.add_argument("--artifact-packet", type=Path)
    args = parser.parse_args()
    result = run_rehearsal(
        ssot_path=args.ssot,
        forward_store_path=args.forward_store,
        candidate_packet_path=args.candidate_packet,
        selection_manifest_path=args.selection_manifest,
        artifact_packet_path=args.artifact_packet,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return (
        0
        if result["state"]
        == "READY_FOR_CONTROLLED_EXPANSION_REHEARSAL"
        else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
