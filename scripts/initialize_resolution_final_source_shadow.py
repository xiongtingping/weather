#!/usr/bin/env python3
"""Initialize the isolated registry used only for final X/S/B accumulation.

This never touches ``data/archive.db`` and never initializes posterior, facts,
decisions, intents or order tables.  It is an explicit one-time shadow-only
operation; target contracts, final sources and final labels stay append-only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolution_final_source_shadow_writer import initialize_isolated_resolution_final_source_staging_schema
from market_resolution_finality_shadow_writer import initialize_isolated_market_resolution_finality_schema
from resolution_label_shadow_writer import initialize_isolated_resolution_label_staging_schema
from resolution_observed_x_s_pair_shadow import initialize_isolated_resolution_observed_x_s_pair_schema
from resolution_target_contract_shadow_writer import initialize_isolated_resolution_target_contract_schema
from resolution_target_capture_cursor import initialize_resolution_target_capture_cursor
from sequential_bayes_public_ssot_shadow_writer import (
    SCHEMA_VERSION as SENTINEL_WRITER_SCHEMA,
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)


SCHEMA_VERSION = "initialize_resolution_final_source_shadow_v1"
CONFIRMATION_PHRASE = "INITIALIZE_ISOLATED_FINAL_LABEL_SHADOW"


def _safe_shadow_db(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    shadow_root = (ROOT / "data" / "shadow").resolve()
    if resolved == shadow_root or shadow_root not in resolved.parents:
        raise ValueError("resolution_final_source_shadow_db_must_be_under_data_shadow")
    if resolved == (ROOT / "data" / "archive.db").resolve():
        raise ValueError("resolution_final_source_shadow_db_must_not_be_archive")
    return resolved


def _initialize(conn: sqlite3.Connection) -> None:
    # This is intentionally the minimal isolation sentinel.  The final-label
    # registry is not a Transaction-B/public-fact shadow database.
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {SHADOW_MANIFEST_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            activation_state TEXT NOT NULL,
            writer_schema TEXT NOT NULL
        )"""
    )
    conn.execute(
        f"INSERT OR IGNORE INTO {SHADOW_MANIFEST_TABLE}(singleton,activation_state,writer_schema) VALUES(1,?,?)",
        (SHADOW_MANIFEST_STATE, SENTINEL_WRITER_SCHEMA),
    )
    initialize_isolated_resolution_target_contract_schema(conn)
    initialize_resolution_target_capture_cursor(
        conn,
        forward_capture_not_before=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    initialize_isolated_market_resolution_finality_schema(conn)
    initialize_isolated_resolution_final_source_staging_schema(conn)
    initialize_isolated_resolution_observed_x_s_pair_schema(conn)
    initialize_isolated_resolution_label_staging_schema(conn)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "shadow" / "resolution_final_labels.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    db = _safe_shadow_db(args.db)
    plan = {
        "schema": SCHEMA_VERSION,
        "db": str(db),
        "state": "plan_only" if not args.apply else "apply_requested",
        "confirmation_phrase_required": CONFIRMATION_PHRASE,
        "tables": [SHADOW_MANIFEST_TABLE, "resolution_target_capture_cursor", "resolution_target_contracts", "market_resolution_finality_attestations", "resolution_final_source_observations", "resolution_observed_x_s_pairs", "resolution_final_labels"],
        "boundary": {
            "shadow_only": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "writes_decisions": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
    }
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.confirm != CONFIRMATION_PHRASE:
        print(json.dumps({**plan, "ok": False, "state": "blocked", "block_reasons": ["confirmation_phrase_invalid"]}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _initialize(conn)
        conn.commit()
    print(json.dumps({**plan, "ok": True, "state": "initialized"}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
