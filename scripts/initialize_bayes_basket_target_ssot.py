#!/usr/bin/env python3
"""Initialize the isolated Bayes × basket target no-live SSOT database.

This command creates only ``data/bayes_basket_target_ssot.db`` by default. It
does not open, copy, back up, pause, restart, or mutate the resident
``data/archive.db`` production line. A zero balance is intentional until a
separate target capital epoch is explicitly supplied; no-live readiness may
be rehearsed without reserving live capital.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import load_activation  # noqa: E402
from bayes_basket_target.atomic_ledger import initialize_capital_pool, record_writer_lock_wait, target_ledger_schema_status  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB, TARGET_ACTIVATION_DDL, resolve_target_database  # noqa: E402
from bayes_basket_target.market_registry import apply_target_market_registry_schema  # noqa: E402
from bayes_basket_target.ssot import apply_production_ssot_schema, production_ssot_schema_status  # noqa: E402


SCHEMA = "bayes_basket_target_ssot_initialization_v1"
DEFAULT_CAPITAL_EPOCH = "bayes-basket-target-production-no-live-bootstrap-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def initialize(
    *,
    db: Path,
    activation_path: Path,
    capital_balance_usdc: float = 0.0,
    capital_epoch_id: str = DEFAULT_CAPITAL_EPOCH,
) -> dict[str, Any]:
    db_path = resolve_target_database(db)
    # The schema-dispatching loader, as the writer gate has used since the
    # go-live flip pointed the default activation at the live contract:
    # the no-live-only loader refuses that file on sight
    # (target_activation_schema_invalid), so initializing a target store
    # against the production activation failed before it began.
    activation = load_activation(activation_path)
    if db_path.exists() and db_path.is_dir():
        raise RuntimeError("target_database_path_is_directory")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed = db_path.exists()
    initialized_at = _now()
    with closing(sqlite3.connect(db_path, timeout=60.0)) as conn:
        with conn:
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("PRAGMA foreign_keys=ON")
            lock_started_ns = time.perf_counter_ns()
            conn.execute("BEGIN IMMEDIATE")
            lock_acquired_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            try:
                schema_result = apply_production_ssot_schema(conn, commit=False)
                registry_schema_result = apply_target_market_registry_schema(conn, commit=False)
                conn.execute(TARGET_ACTIVATION_DDL)
                ledger_status = target_ledger_schema_status(conn)
                if not ledger_status["ok"]:
                    raise RuntimeError("target_ledger_schema_incomplete")
                pool_result = initialize_capital_pool(
                    conn,
                    balance_usdc=float(capital_balance_usdc),
                    capital_epoch_id=str(capital_epoch_id),
                    updated_at_utc=initialized_at,
                    commit=False,
                )
                row = conn.execute(
                    "SELECT generation,state,strategy_id,activation_sha256 "
                    "FROM basket_strategy_activation_state WHERE singleton=1"
                ).fetchone()
                if row is not None:
                    if str(row[2]) != str(activation["strategy_id"]):
                        raise RuntimeError("target_activation_owner_drift")
                    if str(row[3]) != str(activation["activation_contract_sha256"]):
                        raise RuntimeError("target_activation_contract_drift")
                    generation = int(row[0])
                    state = str(row[1])
                else:
                    generation = 1
                    state = "enabled"
                    payload = {**activation, "generation": generation, "activated_at": initialized_at}
                    conn.execute(
                        """INSERT INTO basket_strategy_activation_state(
                             singleton,generation,state,strategy_id,activation_sha256,activation_json,updated_at
                           ) VALUES(1,?,'enabled',?,?,?,?)""",
                        (
                            generation,
                            activation["strategy_id"],
                            activation["activation_contract_sha256"],
                            json.dumps(payload, ensure_ascii=False, sort_keys=True),
                            initialized_at,
                        ),
                    )
                record_writer_lock_wait(
                    conn,
                    operation="target_ssot_initialization",
                    wait_ms=(time.perf_counter_ns() - lock_started_ns) / 1_000_000.0,
                    acquired_at_utc=lock_acquired_at,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    with closing(
        sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
    ) as conn:
        with conn:
            final_schema = production_ssot_schema_status(conn)
            final_ledger = target_ledger_schema_status(conn)
            activation_row = conn.execute(
                "SELECT generation,state,strategy_id,activation_sha256 "
                "FROM basket_strategy_activation_state WHERE singleton=1"
            ).fetchone()
    result = {
        "schema": SCHEMA,
        "state": "already_initialized" if existed else "initialized",
        "database": str(db_path),
        "database_scope": "isolated_target_no_live",
        "shared_live_database": str((ROOT / "data" / "archive.db").resolve()),
        "activation": {
            "path": str(activation_path.expanduser().resolve()),
            "strategy_id": activation["strategy_id"],
            "activation_contract_sha256": activation["activation_contract_sha256"],
            "execution_mode": activation["execution_mode"],
            "live_submit_authorized": False,
            "places_orders": False,
            "calls_order_gateway": False,
            "generation": int(activation_row[0]),
            "state": str(activation_row[1]),
        },
        "capital_pool": pool_result,
        "schema_status": final_schema,
        "market_registry_schema": registry_schema_result,
        "ledger_status": final_ledger,
        "boundary": {
            "writes_shared_live_database": False,
            "writes_orders": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "restarts_launchd": False,
            "stops_protected_live_line": False,
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--activation",
        type=Path,
        default=ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json",
    )
    parser.add_argument("--capital-balance-usdc", type=float, default=0.0)
    parser.add_argument("--capital-epoch-id", default=DEFAULT_CAPITAL_EPOCH)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=ROOT / "output/audits/bayes_basket_target_ssot_initialization.json",
    )
    args = parser.parse_args()
    try:
        result = initialize(
            db=args.db,
            activation_path=args.activation,
            capital_balance_usdc=args.capital_balance_usdc,
            capital_epoch_id=args.capital_epoch_id,
        )
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "state": "blocked_fail_closed", "reason": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
        return 2
    receipt = args.receipt.expanduser().resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "state": result["state"], "database": result["database"], "receipt": str(receipt)}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
