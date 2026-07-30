from __future__ import annotations

import sqlite3
from pathlib import Path

from bayes_basket_target.atomic_ledger import (
    apply_target_ledger_schema,
    initialize_capital_pool,
)
from scripts.apply_bayes_basket_target_prepared_supersession_schema import (
    migrate,
)


def test_prepared_supersession_migration_is_additive_and_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "target.db"
    with sqlite3.connect(database) as conn:
        apply_target_ledger_schema(conn)
        initialize_capital_pool(
            conn,
            balance_usdc=12.0,
            capital_epoch_id="epoch-test",
            updated_at_utc="2026-07-29T04:00:00Z",
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "basket_target_prepared_supersession_no_update"
        )
        conn.execute(
            "DROP TRIGGER IF EXISTS "
            "basket_target_prepared_supersession_no_delete"
        )
        conn.execute(
            "DROP INDEX IF EXISTS "
            "basket_target_prepared_supersession_new_scope_idx"
        )
        conn.execute(
            "DROP TABLE basket_target_prepared_supersession_receipts"
        )
        conn.commit()

    first = migrate(db_path=database, confirmed_writer_stopped=False)
    second = migrate(db_path=database, confirmed_writer_stopped=False)

    assert first["state"] == "migrated"
    assert second["state"] == "unchanged"
    assert first["quick_check"] == second["quick_check"] == "ok"
    assert first["performs_legacy_backfill"] is False
    assert first["calls_order_gateway"] is False
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version "
            "FROM basket_target_capital_pool"
        ).fetchone() == (12.0, 0.0, "epoch-test", 0)
        assert conn.execute(
            "SELECT COUNT(*) FROM "
            "basket_target_prepared_supersession_receipts"
        ).fetchone()[0] == 0
