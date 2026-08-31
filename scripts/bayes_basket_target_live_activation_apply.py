#!/usr/bin/env python3
"""Align the target DB activation row with the live file contract.

The go-live migration widened the intents CHECK and the code constants moved
to ``production_live``, but nothing ever wrote the live contract into
``basket_strategy_activation_state``.  The row kept the no-live contract, so
every cycle read a live file, compared its hash against a no-live row, and
published ``blocked_fail_closed`` with reason
``target_activation_invalid:RuntimeError:target_activation_state_drift``.
This is the missing half of that migration: one row, one compare-and-swap.

Two deliberate differences from the no-live applier next to it.

The rollback point is the previous row written to a small JSON receipt, not a
copy of the database.  ``_backup_database`` there takes a full SQLite backup;
the SSOT is 27 GB on a volume with 30 GB free, which is what killed the first
go-live attempt with ENOSPC, and the go-live commit already ruled that a
whole-file restore is the worse rollback besides -- it discards every
unrelated production write since.  A one-row change gets a one-row rollback.

And quiescence is required, not optional.  The no-live applier has an online
CAS path whose safety argument is that both generations prohibit every live
boundary, so no exchange state can exist either side of the swap.  That
argument does not survive the mode this script writes: the generation it
installs is precisely the one that may reach the exchange.  So the resident
writer must be down.

This script writes one row.  It submits no order, opens no gateway, and does
not touch data/live_paused.flag or config/trading/live_intent_v1.json -- the
operator's switches stay the operator's.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import (  # noqa: E402
    assert_target_database_owner,
    load_activation,
)
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    is_isolated_target_database,
    resolve_target_database,
)
from bayes_basket_target.migration_quiescence import (  # noqa: E402
    isolated_target_writer_quiescence_receipt,
)
from bayes_basket_target.resource_bounds import write_bounded_json  # noqa: E402
from bayes_basket_target.ssot import production_ssot_schema_status  # noqa: E402

SCHEMA = "bayes_basket_target_live_activation_apply_v1"
LIVE_ACTIVATION_PATH = (
    ROOT / "config" / "trading"
    / "bayes_basket_target_production_live_activation_v1.json"
)
DEFAULT_ROLLBACK_PATH = ROOT / "data" / "live_activation_rollback_point.json"

# The same split open_target_writer_if_enabled asserts.  Restated here rather
# than imported so this script refuses a contract the worker would refuse,
# without importing the worker and its resident side effects.
LIVE_MUST_BE_TRUE = (
    "live_decision_write_authorized",
    "live_order_authorized",
    "live_submit_authorized",
    "places_orders",
    "calls_order_gateway",
    "network_access",
)
LIVE_MUST_BE_FALSE = ("writes_production_db", "production_mutation")


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _assert_operator_armed() -> dict[str, Any]:
    """Read, never write, the two switches that are the operator's alone.

    This script installs the DB half of a decision the operator makes in
    ``live_intent_v1.json``.  Installing it while that file says no-live
    would be this script deciding instead, so it refuses.  The pause flag is
    reported but not required: pausing is how an operator stops submission
    without unwinding the contract, and it must stay usable.
    """

    intent_path = ROOT / "config" / "trading" / "live_intent_v1.json"
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- reported, not raised through
        raise RuntimeError(f"live_intent_unreadable:{type(exc).__name__}") from exc
    if intent.get("live_enabled") is not True:
        raise RuntimeError("live_intent_not_armed")
    pause_flag = ROOT / "data" / "live_paused.flag"
    return {
        "live_intent_path": str(intent_path),
        "live_enabled": True,
        "live_pause_flag_present": pause_flag.exists(),
    }


def _validate_live_contract(activation: Mapping[str, Any]) -> None:
    if activation.get("execution_mode") != "production_live":
        raise RuntimeError("live_activation_execution_mode_invalid")
    missing_true = [k for k in LIVE_MUST_BE_TRUE if activation.get(k) is not True]
    if missing_true:
        raise RuntimeError(
            "live_activation_boundary_not_true:" + ",".join(missing_true)
        )
    wrong_false = [k for k in LIVE_MUST_BE_FALSE if activation.get(k) is not False]
    if wrong_false:
        raise RuntimeError(
            "live_activation_boundary_not_false:" + ",".join(wrong_false)
        )


def apply_live_activation(
    *,
    db_path: Path,
    activation_path: Path,
    rollback_path: Path,
    apply: bool,
) -> dict[str, Any]:
    db = resolve_target_database(db_path)
    if not is_isolated_target_database(db):
        raise RuntimeError("live_activation_requires_isolated_target_database")

    operator = _assert_operator_armed()
    # load_activation, not load_target_activation: the narrow loader refuses
    # the live schema on sight, which is the same mistake that shut the
    # writer gate for seven hours on 2026-08-30.
    activation = load_activation(activation_path)
    _validate_live_contract(activation)
    next_sha = str(activation["activation_contract_sha256"])

    quiescence = isolated_target_writer_quiescence_receipt(db)
    if quiescence.get("quiescent") is not True and apply:
        raise RuntimeError("live_activation_writer_quiescence_not_proven")

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0) as conn:
        try:
            assert_target_database_owner(conn)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        status = production_ssot_schema_status(conn)
        if not status.get("ok") or not status.get("ledger_schema_ok"):
            raise RuntimeError("target_ssot_schema_not_ready")
        row = conn.execute(
            "SELECT generation,state,strategy_id,activation_sha256,activation_json "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
    if row is None:
        raise RuntimeError("live_activation_current_row_missing")
    previous = {
        "generation": int(row[0] or 0),
        "state": str(row[1] or ""),
        "strategy_id": str(row[2] or ""),
        "activation_sha256": str(row[3] or ""),
        "activation_json": str(row[4] or ""),
    }
    if previous["strategy_id"] != str(activation["strategy_id"]):
        raise RuntimeError("live_activation_strategy_id_mismatch")

    plan = {
        "schema": SCHEMA,
        "database": str(db),
        "activation_path": str(activation_path),
        "operator": operator,
        "writer_quiescence": quiescence,
        "previous_generation": previous["generation"],
        "previous_activation_sha256": previous["activation_sha256"],
        "next_generation": previous["generation"] + 1,
        "next_activation_sha256": next_sha,
        "already_applied": previous["activation_sha256"] == next_sha,
        "applied": False,
        "orders_submitted": 0,
        "places_orders": False,
        "calls_order_gateway": False,
        "planned_at": _now(),
    }
    if plan["already_applied"]:
        plan["state"] = "ALREADY_LIVE"
        return plan
    if not apply:
        plan["state"] = "PLANNED"
        return plan

    # The rollback point is the previous row, written before the swap.  It is
    # a few hundred bytes and it is everything a rollback needs: the same
    # compare-and-swap run in reverse restores it exactly.
    write_bounded_json(
        rollback_path,
        {
            "schema": "bayes_basket_target_live_activation_rollback_point_v1",
            "database": str(db),
            "captured_at": _now(),
            "previous": previous,
            "replaced_by_activation_sha256": next_sha,
        },
    )

    activated_at = _now()
    generation = previous["generation"] + 1
    payload = {**activation, "generation": generation, "activated_at": activated_at}
    with sqlite3.connect(db, timeout=60.0, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """UPDATE basket_strategy_activation_state
                      SET generation=?, state='enabled', strategy_id=?,
                          activation_sha256=?, activation_json=?, updated_at=?
                    WHERE singleton=1
                      AND generation=?
                      AND activation_sha256=?""",
                (
                    generation,
                    str(activation["strategy_id"]),
                    next_sha,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    activated_at,
                    previous["generation"],
                    previous["activation_sha256"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("live_activation_compare_and_swap_failed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0) as conn:
        check = conn.execute(
            "SELECT generation,state,activation_sha256 "
            "FROM basket_strategy_activation_state WHERE singleton=1"
        ).fetchone()
    if (
        check is None
        or int(check[0]) != generation
        or str(check[1]) != "enabled"
        or str(check[2]) != next_sha
    ):
        raise RuntimeError("live_activation_post_write_verification_failed")

    plan["state"] = "APPLIED"
    plan["applied"] = True
    plan["applied_at"] = activated_at
    plan["rollback_point"] = str(rollback_path)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument("--activation", type=Path, default=LIVE_ACTIVATION_PATH)
    parser.add_argument("--rollback-point", type=Path, default=DEFAULT_ROLLBACK_PATH)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the row; without it this plans and prints only",
    )
    args = parser.parse_args()
    try:
        receipt = apply_live_activation(
            db_path=args.db,
            activation_path=args.activation,
            rollback_path=args.rollback_point,
            apply=bool(args.apply),
        )
    except Exception as exc:  # noqa: BLE001 -- a receipt, not a traceback
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "state": "REFUSED",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "applied": False,
                    "orders_submitted": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
