#!/usr/bin/env python3
"""Migrate the isolated target SSOT without stopping shared resident services."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.artifact_ingress import (  # noqa: E402
    ingest_artifact_bundle,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    DEFAULT_TARGET_PAUSE_MARKER,
    resolve_target_database,
)
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    transaction_fenced_writer_quiescence_receipt,
)
from bayes_basket_target.probability_bridge import (  # noqa: E402
    backfill_probability_bridges,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json,
    write_bounded_json,
)
from bayes_basket_target.ssot import production_ssot_schema_status  # noqa: E402
from scripts.bayes_basket_target_production_ssot_apply import (  # noqa: E402
    PAUSE_MARKER_TOKEN,
    apply_controlled_migration,
)
from scripts.ingest_bayes_basket_target_artifacts import (  # noqa: E402
    packet_binding_counts,
    validate_packet_sha256,
)
from scripts.ingest_bayes_basket_probability_evidence import (  # noqa: E402
    ingest as ingest_probability_evidence,
)


SCHEMA = "bayes_basket_target_transaction_fenced_cutover_v1"
DEFAULT_SCHEMA_BOOTSTRAP_PACKET = (
    ROOT
    / "output/audits/"
    "bayes_basket_target_no_live_vertical_slice_RKPK_v3.json"
)
DEFAULT_ACTIVATION = (
    ROOT
    / "config/trading/"
    "bayes_basket_target_production_no_live_activation_v1.json"
)
SERVICE_LABELS = (
    "com.weather.weather_basket_yes",
    "com.weather.market_ws",
    "com.weather.clob_order_service",
)
SYSTEM_SERVICE_DOMAIN = "system"
SERVICE_DOMAIN = f"gui/{os.getuid()}"
REQUIRED_SERVICE_DOMAIN = "user"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _launchctl_print(domain: str, label: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["/bin/launchctl", "print", f"{domain}/{label}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"required_{REQUIRED_SERVICE_DOMAIN}_service_probe_failed:{label}"
        ) from exc


def _reject_system_service_residue(label: str) -> None:
    completed = _launchctl_print(SYSTEM_SERVICE_DOMAIN, label)
    if completed.returncode == 0:
        raise RuntimeError(f"forbidden_system_service_residue:{label}")


def _service_pid(label: str) -> int:
    _reject_system_service_residue(label)
    completed = _launchctl_print(SERVICE_DOMAIN, label)
    if completed.returncode != 0:
        raise RuntimeError(f"required_user_service_missing:{label}")
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("pid = "):
            pid = int(line.split("=", 1)[1].strip())
            if pid <= 0:
                break
            return pid
    raise RuntimeError(f"required_user_service_pid_missing:{label}")


def _service_loaded(label: str) -> bool:
    _reject_system_service_residue(label)
    return _launchctl_print(SERVICE_DOMAIN, label).returncode == 0


def _service_pids() -> dict[str, int]:
    return {label: _service_pid(label) for label in SERVICE_LABELS}


def _quick_check(conn: sqlite3.Connection) -> None:
    result = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    if result != ["ok"]:
        raise RuntimeError("transaction_fenced_cutover_quick_check_failed")


def _assert_no_live_activation(path: Path) -> dict[str, Any]:
    activation = load_target_activation(path)
    if activation.get("execution_mode") != "production_no_live":
        raise RuntimeError("transaction_fenced_activation_mode_invalid")
    if activation.get("target_only") is not True:
        raise RuntimeError("transaction_fenced_activation_not_target_only")
    for key in (
        "live_decision_write_authorized",
        "live_order_authorized",
        "live_submit_authorized",
        "production_mutation",
        "places_orders",
        "writes_production_db",
        "calls_order_gateway",
        "network_access",
        "authorizes_order",
    ):
        if activation.get(key) is not False:
            raise RuntimeError(
                f"transaction_fenced_activation_{key}_must_be_false"
            )
    return activation


def _validate_schema_bootstrap_packet(packet: dict[str, Any]) -> str:
    packet_sha = validate_packet_sha256(packet, required=True)
    if packet.get("station_ids") != ["RKPK"]:
        raise ContractError(
            "transaction_fenced_schema_bootstrap_station_binding_invalid"
        )
    vertical = packet.get("vertical_slice")
    if (
        not isinstance(vertical, dict)
        or vertical.get("mode") != "single_station_exact_scope"
        or vertical.get("station_id") != "RKPK"
        or vertical.get("batch_expansion_authorized") is not False
    ):
        raise ContractError(
            "transaction_fenced_schema_bootstrap_vertical_scope_invalid"
        )
    if packet.get("execution_mode") != "production_no_live":
        raise ContractError("transaction_fenced_schema_bootstrap_mode_invalid")
    for key in (
        "live_order_authorized",
        "live_submit_authorized",
        "places_orders",
        "calls_order_gateway",
    ):
        if packet.get(key) is not False:
            raise ContractError(
                f"transaction_fenced_schema_bootstrap_{key}_must_be_false"
            )
    if int(packet.get("orders_submitted") or 0) != 0:
        raise ContractError(
            "transaction_fenced_schema_bootstrap_orders_submitted_nonzero"
        )
    return packet_sha


def run_cutover(
    *,
    db_path: Path,
    packet_path: Path,
    activation_path: Path,
    backup_path: Path,
    pause_path: Path,
    lock_timeout_seconds: float,
    target_writer_stopped: bool = False,
    probability_evidence_packet_path: Path | None = None,
    expected_probability_station_id: str | None = None,
    require_no_probability_activations: bool = False,
) -> dict[str, Any]:
    db = resolve_target_database(db_path)
    packet_file = packet_path.expanduser().resolve()
    probability_evidence_packet_file = (
        probability_evidence_packet_path.expanduser().resolve()
        if probability_evidence_packet_path is not None
        else None
    )
    activation_file = activation_path.expanduser().resolve()
    backup = backup_path.expanduser().resolve()
    pause = pause_path.expanduser().resolve()
    if not db.is_file() or db.is_symlink():
        raise RuntimeError("transaction_fenced_target_database_invalid")
    if not packet_file.is_file() or packet_file.is_symlink():
        raise RuntimeError("transaction_fenced_packet_path_invalid")
    if not activation_file.is_file() or activation_file.is_symlink():
        raise RuntimeError("transaction_fenced_activation_path_invalid")
    if probability_evidence_packet_file is not None and (
        not probability_evidence_packet_file.is_file()
        or probability_evidence_packet_file.is_symlink()
    ):
        raise RuntimeError(
            "transaction_fenced_probability_evidence_packet_path_invalid"
        )
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("transaction_fenced_backup_path_exists")

    activation = _assert_no_live_activation(activation_file)
    raw_packet = load_bounded_json(packet_file)
    if not isinstance(raw_packet, dict):
        raise RuntimeError("transaction_fenced_packet_mapping_required")
    packet = dict(raw_packet)
    packet_sha = _validate_schema_bootstrap_packet(packet)
    probability_evidence_packet: dict[str, Any] | None = None
    if probability_evidence_packet_file is not None:
        raw_probability_evidence_packet = load_bounded_json(
            probability_evidence_packet_file
        )
        if not isinstance(raw_probability_evidence_packet, dict):
            raise RuntimeError(
                "transaction_fenced_probability_evidence_packet_mapping_required"
            )
        probability_evidence_packet = dict(
            raw_probability_evidence_packet
        )
    if target_writer_stopped:
        if _service_loaded("com.weather.weather_basket_yes"):
            raise RuntimeError(
                "transaction_fenced_target_writer_expected_stopped"
            )
        pids_before = {
            label: _service_pid(label)
            for label in SERVICE_LABELS
            if label != "com.weather.weather_basket_yes"
        }
    else:
        pids_before = _service_pids()

    pause_preexisted = pause.exists()
    pause_original: bytes | None = None
    if pause_preexisted:
        if pause.is_symlink() or not pause.is_file():
            raise RuntimeError(
                "transaction_fenced_pause_preexisting_path_invalid"
            )
        pause_original = pause.read_bytes()
        if len(pause_original) > 4096:
            raise RuntimeError(
                "transaction_fenced_pause_preexisting_bytes_exceeded"
            )
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(
        PAUSE_MARKER_TOKEN + "\ntransaction-fenced-schema-bootstrap-v3\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    committed = False
    commit_attempted = False
    migration: dict[str, Any] | None = None
    staged: dict[str, Any] | None = None
    probability_bridge_backfill: dict[str, Any] | None = None
    probability_evidence_ingress: dict[str, Any] | None = None
    fence: dict[str, Any] | None = None
    try:
        with sqlite3.connect(
            db,
            timeout=float(lock_timeout_seconds),
            isolation_level=None,
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                f"PRAGMA busy_timeout={int(lock_timeout_seconds * 1000)}"
            )
            conn.execute("PRAGMA synchronous=FULL")
            _quick_check(conn)
            conn.execute("BEGIN IMMEDIATE")
            lock_acquired_after_seconds = time.monotonic() - started
            try:
                fence = transaction_fenced_writer_quiescence_receipt(
                    conn,
                    db_path=db,
                )
                migration = apply_controlled_migration(
                    conn,
                    db_path=db,
                    backup_path=backup,
                    pause_file=pause,
                    legacy_archive_table=(
                        "auto_trade_decisions_legacy_v4_transaction_fenced"
                    ),
                    confirmed=True,
                    writer_quiescence_receipt=fence,
                )
                probability_bridge_backfill = (
                    backfill_probability_bridges(conn)
                )
                staged = ingest_artifact_bundle(
                    conn,
                    packet,
                    commit=True,
                    finalize_transaction=False,
                )
                if staged.get("state") != "STAGED_IN_CALLER_TRANSACTION":
                    raise RuntimeError(
                        "transaction_fenced_packet_not_staged"
                    )
                if probability_evidence_packet is not None:
                    probability_evidence_ingress = (
                        ingest_probability_evidence(
                            conn,
                            probability_evidence_packet,
                            commit=True,
                            finalize_transaction=False,
                            expected_station_id=(
                                expected_probability_station_id
                            ),
                            require_no_activations=(
                                require_no_probability_activations
                            ),
                        )
                    )
                    if (
                        probability_evidence_ingress.get("state")
                        != "STAGED_IN_CALLER_TRANSACTION"
                    ):
                        raise RuntimeError(
                            "transaction_fenced_probability_evidence_not_staged"
                        )
                status_before_commit = production_ssot_schema_status(conn)
                if status_before_commit.get("ok") is not True:
                    raise RuntimeError(
                        "transaction_fenced_schema_postcheck_failed"
                    )
                bindings_before_commit = packet_binding_counts(conn, packet)
                _quick_check(conn)
                commit_attempted = True
                conn.commit()
                committed = True
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise

            _quick_check(conn)
            status_after_commit = production_ssot_schema_status(conn)
            if status_after_commit.get("ok") is not True:
                raise RuntimeError(
                    "transaction_fenced_schema_commit_not_visible"
                )
            idempotent = ingest_artifact_bundle(
                conn,
                packet,
                commit=True,
            )
            if (
                idempotent.get("state") != "COMMITTED"
                or any(
                    int(value or 0) != 0
                    for value in idempotent.get("counts", {}).values()
                )
            ):
                raise RuntimeError(
                    "transaction_fenced_packet_replay_not_idempotent"
                )
            bindings_after_replay = packet_binding_counts(conn, packet)
            if bindings_after_replay != bindings_before_commit:
                raise RuntimeError(
                    "transaction_fenced_packet_binding_drift"
                )
            probability_evidence_idempotent = None
            if probability_evidence_packet is not None:
                probability_evidence_idempotent = (
                    ingest_probability_evidence(
                        conn,
                        probability_evidence_packet,
                        commit=True,
                        expected_station_id=(
                            expected_probability_station_id
                        ),
                        require_no_activations=(
                            require_no_probability_activations
                        ),
                    )
                )
                if (
                    probability_evidence_idempotent.get("state")
                    != "COMMITTED"
                    or any(
                        receipt.get("wrote") is True
                        for key in (
                            "artifact_receipts",
                            "promotion_receipts",
                            "activation_receipts",
                        )
                        for receipt in (
                            probability_evidence_idempotent.get(key) or ()
                        )
                    )
                ):
                    raise RuntimeError(
                        "transaction_fenced_probability_evidence_replay_not_idempotent"
                    )
    except Exception:
        # Once COMMIT has been attempted its outcome may be unknown even when
        # sqlite raises.  Preserve the pre-cutover image so the outer
        # maintenance wrapper can always restore before restarting the writer.
        if not commit_attempted and not committed and backup.exists():
            backup.unlink(missing_ok=True)
        raise
    finally:
        if pause_preexisted:
            assert pause_original is not None
            pause.write_bytes(pause_original)
        else:
            pause.unlink(missing_ok=True)

    if target_writer_stopped:
        if _service_loaded("com.weather.weather_basket_yes"):
            raise RuntimeError(
                "transaction_fenced_target_writer_reappeared"
            )
        pids_after = {
            label: _service_pid(label)
            for label in SERVICE_LABELS
            if label != "com.weather.weather_basket_yes"
        }
    else:
        pids_after = _service_pids()
    if pids_after != pids_before:
        raise RuntimeError("transaction_fenced_shared_service_pid_changed")
    elapsed_seconds = time.monotonic() - started
    return {
        "schema": SCHEMA,
        "state": "MATERIALIZED",
        "completed_at_utc": _utc_now(),
        "required_domain": REQUIRED_SERVICE_DOMAIN,
        "service_domain": SERVICE_DOMAIN,
        "database": str(db),
        "packet_sha256": packet_sha,
        "schema_bootstrap_artifact": {
            "path": str(packet_file),
            "sha256": packet_sha,
            "artifact_type": "schema_bootstrap",
            "not_production_station_scope": True,
            "caller_scope_contract": "canonical45",
        },
        "activation": str(activation_file),
        "activation_sha256": str(
            activation.get("activation_sha256") or ""
        ),
        "migration": migration,
        "artifact_ingress": staged,
        "probability_evidence_packet": (
            str(probability_evidence_packet_file)
            if probability_evidence_packet_file is not None
            else None
        ),
        "probability_evidence_ingress": probability_evidence_ingress,
        "probability_evidence_idempotent_replay": (
            probability_evidence_idempotent
            if probability_evidence_packet is not None
            else None
        ),
        "probability_bridge_backfill": probability_bridge_backfill,
        "artifact_bindings": bindings_after_replay,
        "idempotent_second_commit_counts": idempotent["counts"],
        "backup_path": str(backup),
        "transaction_fence": fence,
        "lock_acquired_after_seconds": lock_acquired_after_seconds,
        "elapsed_seconds": elapsed_seconds,
        "service_pids_before": pids_before,
        "service_pids_after": pids_after,
        "service_pid_stability": True,
        "target_writer_stopped": bool(target_writer_stopped),
        "pause_marker_restored": pause_preexisted,
        "pause_marker_removed": not pause_preexisted,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "authorizes_order": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "boundary": {
            "stops_services": False,
            "restarts_services": False,
            "writes_archive_database": False,
            "writes_target_database": True,
            "target_database_transaction_fenced": True,
            "protected_resident_pid_preserved": True,
            "target_writer_managed_by_outer_cutover": bool(
                target_writer_stopped
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--packet",
        type=Path,
        default=DEFAULT_SCHEMA_BOOTSTRAP_PACKET,
        help="schema bootstrap artifact only; not a production station scope",
    )
    parser.add_argument(
        "--activation",
        type=Path,
        default=DEFAULT_ACTIVATION,
    )
    parser.add_argument(
        "--pause-file",
        type=Path,
        default=DEFAULT_TARGET_PAUSE_MARKER,
    )
    parser.add_argument("--backup-db", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--probability-evidence-packet",
        type=Path,
        help=(
            "optional content-addressed P0-history/P1-trajectory packet to "
            "stage in the same migration transaction"
        ),
    )
    parser.add_argument(
        "--expected-probability-station-id",
        help=(
            "require every probability-evidence artifact and in-packet "
            "reference to remain inside this station scope"
        ),
    )
    parser.add_argument(
        "--require-no-probability-activations",
        action="store_true",
        help="reject candidate packets containing any activation row",
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=float,
        default=60.0,
    )
    parser.add_argument(
        "--confirm-transaction-fenced-cutover",
        action="store_true",
    )
    parser.add_argument(
        "--target-writer-stopped",
        action="store_true",
        help=(
            "require reported_trajectory_no to remain absent while preserving "
            "market_ws and clob_order_service PIDs"
        ),
    )
    args = parser.parse_args()
    if not args.confirm_transaction_fenced_cutover:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "state": "BLOCKED_FAIL_CLOSED",
                    "reason": "explicit_cutover_confirmation_required",
                    "required_domain": REQUIRED_SERVICE_DOMAIN,
                    "service_domain": SERVICE_DOMAIN,
                    "stops_services": False,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    try:
        result = run_cutover(
            db_path=args.db,
            packet_path=args.packet,
            activation_path=args.activation,
            backup_path=args.backup_db,
            pause_path=args.pause_file,
            lock_timeout_seconds=args.lock_timeout_seconds,
            target_writer_stopped=args.target_writer_stopped,
            probability_evidence_packet_path=(
                args.probability_evidence_packet
            ),
            expected_probability_station_id=(
                args.expected_probability_station_id
            ),
            require_no_probability_activations=(
                args.require_no_probability_activations
            ),
        )
        write_bounded_json(args.receipt.expanduser().resolve(), result)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "state": "BLOCKED_FAIL_CLOSED",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "required_domain": REQUIRED_SERVICE_DOMAIN,
                    "service_domain": SERVICE_DOMAIN,
                    "stops_services": False,
                    "restarts_services": False,
                    "calls_order_gateway": False,
                    "orders_submitted": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "schema": result["schema"],
                "state": result["state"],
                "required_domain": result["required_domain"],
                "service_domain": result["service_domain"],
                "packet_sha256": result["packet_sha256"],
                "schema_bootstrap_artifact": result[
                    "schema_bootstrap_artifact"
                ],
                "service_pid_stability": result[
                    "service_pid_stability"
                ],
                "orders_submitted": 0,
                "receipt": str(args.receipt.expanduser().resolve()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
