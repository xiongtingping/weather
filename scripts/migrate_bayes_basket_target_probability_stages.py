#!/usr/bin/env python3
"""Migrate the target-only probability stages to P0 → P0-M → P1.

The migration is intentionally narrow. It preserves calibration, registry,
canonical facts, decisions and reconciliation receipts. The obsolete
probability table may be rebuilt only while empty. Preliminary plans created
under the superseded stage names are removed because they have no executable
authority and cannot be relabelled without corrupting lineage.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.activation import load_target_activation  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import (  # noqa: E402
    DEFAULT_TARGET_DB,
    resolve_target_database,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    assert_connection_target_capacity,
)
from bayes_basket_target.ssot import (  # noqa: E402
    PRODUCTION_TARGET_DDL,
    _execute_sql_script_transactional,
    production_ssot_schema_status,
)


CONFIRMATION = "MIGRATE_TARGET_PROBABILITY_STAGES_P0_P0M_P1"


def migrate(
    *,
    db_path: Path,
    activation_path: Path,
    confirm: str,
) -> dict[str, Any]:
    if confirm != CONFIRMATION:
        raise ContractError("probability_stage_migration_confirmation_invalid")
    activation = load_target_activation(activation_path)
    if (
        activation.get("execution_mode") != "production_no_live"
        or activation.get("live_submit_authorized") is not False
        or activation.get("calls_order_gateway") is not False
        or activation.get("places_orders") is not False
    ):
        raise ContractError("probability_stage_migration_no_live_boundary_invalid")
    db = resolve_target_database(db_path)
    with sqlite3.connect(db, timeout=60.0) as conn:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=FULL")
        assert_connection_target_capacity(conn)
        probability_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_probability_envelope"
            ).fetchone()[0]
        )
        if probability_count:
            raise ContractError(
                "probability_stage_migration_requires_empty_probability_table"
            )
        unsafe_counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "basket_target_order_intents",
                "basket_target_capital_reservations",
                "basket_target_order_events",
            )
        }
        if any(unsafe_counts.values()):
            raise ContractError("probability_stage_migration_execution_state_not_empty")
        preliminary_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM basket_p0_preselection_plan"
            ).fetchone()[0]
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            for trigger in (
                "basket_probability_envelope_no_update",
                "basket_probability_envelope_no_delete",
                "basket_p0_preselection_no_update",
                "basket_p0_preselection_no_delete",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for index in (
                "basket_probability_envelope_asof_idx",
                "basket_p0_preselection_scope_idx",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {index}")
            conn.execute("DROP TABLE basket_probability_envelope")
            conn.execute("DROP TABLE basket_p0_preselection_plan")
            _execute_sql_script_transactional(conn, PRODUCTION_TARGET_DDL)
            status = production_ssot_schema_status(conn)
            if not status["ok"]:
                raise ContractError("probability_stage_migration_schema_invalid")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    identity = {
        "schema": "bayes_basket_target_probability_stage_migration_v1",
        "state": "COMMITTED",
        "database": str(db),
        "old_probability_rows": probability_count,
        "removed_incompatible_preliminary_rows": preliminary_count,
        "preserved_auto_trade_decisions": True,
        "preserved_execution_cycles": True,
        "unsafe_execution_counts": unsafe_counts,
        "stage_contract": ["P0", "P0-M", "P1"],
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--activation",
        type=Path,
        default=(
            ROOT
            / "config/trading/"
            "bayes_basket_target_production_no_live_activation_v1.json"
        ),
    )
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    result = migrate(
        db_path=args.db,
        activation_path=args.activation,
        confirm=args.confirm,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
