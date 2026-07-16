"""Read-only integrity audit for the isolated final-label accumulation DB."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from resolution_final_source_shadow_writer import SOURCE_TABLE
from resolution_label_shadow_writer import LABEL_TABLE
from resolution_observed_x_s_pair_shadow import PAIR_TABLE
from resolution_target_contract_shadow_writer import TARGET_TABLE
from resolution_target_capture_cursor import CURSOR_TABLE, load_resolution_target_capture_not_before
from market_resolution_finality_shadow_writer import FINALITY_TABLE
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE


SCHEMA_VERSION = "resolution_final_label_shadow_registry_audit_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def build_resolution_final_label_shadow_registry_audit(*, db_path: str | Path) -> dict[str, Any]:
    """Audit only the registry boundary; never reads or writes archive.db."""

    path = Path(db_path).expanduser().resolve()
    required = (SHADOW_MANIFEST_TABLE, CURSOR_TABLE, TARGET_TABLE, FINALITY_TABLE, SOURCE_TABLE, PAIR_TABLE, LABEL_TABLE)
    if not path.exists():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "missing",
            "db": str(path),
            "block_reasons": ["resolution_final_label_shadow_db_missing"],
            "read_only": True,
        }
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        missing = [table for table in required if not _table_exists(conn, table)]
        sentinel = conn.execute(
            f"SELECT activation_state,writer_schema FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
        ).fetchone() if not missing else None
        blockers: list[str] = []
        if missing:
            blockers.append("resolution_final_label_shadow_schema_incomplete")
        if sentinel is None or str(sentinel[0] or "") != SHADOW_MANIFEST_STATE:
            blockers.append("resolution_final_label_shadow_manifest_invalid")
        counts = {
            "target_contracts": int(conn.execute(f"SELECT COUNT(*) FROM {TARGET_TABLE}").fetchone()[0]) if TARGET_TABLE not in missing else None,
            "market_finality_attestations": int(conn.execute(f"SELECT COUNT(*) FROM {FINALITY_TABLE}").fetchone()[0]) if FINALITY_TABLE not in missing else None,
            "final_sources": int(conn.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE}").fetchone()[0]) if SOURCE_TABLE not in missing else None,
            "observed_x_s_pairs": int(conn.execute(f"SELECT COUNT(*) FROM {PAIR_TABLE}").fetchone()[0]) if PAIR_TABLE not in missing else None,
            "final_labels": int(conn.execute(f"SELECT COUNT(*) FROM {LABEL_TABLE}").fetchone()[0]) if LABEL_TABLE not in missing else None,
        }
        try:
            forward_capture_not_before = load_resolution_target_capture_not_before(conn)
        except Exception:
            forward_capture_not_before = ""
            blockers.append("resolution_target_capture_cursor_invalid")
    state = "initialized_empty" if not blockers and not any(int(value or 0) for value in counts.values()) else "accumulating" if not blockers else "blocked"
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": state,
        "db": str(path),
        "counts": counts,
        "forward_capture_not_before": forward_capture_not_before,
        "block_reasons": sorted(set(blockers)),
        "boundary": {
            "shadow_only": True,
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


__all__ = ["SCHEMA_VERSION", "build_resolution_final_label_shadow_registry_audit"]
