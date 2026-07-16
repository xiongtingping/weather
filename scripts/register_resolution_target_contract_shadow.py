#!/usr/bin/env python3
"""Plan or append one raw-bound L0 target contract to the isolated registry."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_rule_raw_archive import load_market_rule_raw_snapshot
from market_rule_target_contract_parser import parse_market_rule_target_contract_extraction
from resolution_authority_contract_runtime import load_resolution_authority_contract
from resolution_target_contract_runtime import build_market_resolution_target_contract_proposal
from resolution_target_contract_shadow_writer import persist_resolution_target_contract_shadow


SCHEMA_VERSION = "register_resolution_target_contract_shadow_v1"
CONFIRMATION_PHRASE = "APPEND_ISOLATED_TARGET_CONTRACT"


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_shadow_db(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = (ROOT / "data" / "shadow").resolve()
    if root not in resolved.parents:
        raise ValueError("target_contract_shadow_db_must_be_under_data_shadow")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Target identity manifest; rule fields are parsed only from the retained raw snapshot.")
    parser.add_argument("--market-rule-archive-dir", type=Path, default=ROOT / "data" / "shadow" / "market_rule_raw")
    parser.add_argument("--contract", type=Path, default=ROOT / "config" / "resolution_authorities" / "zuuu_amsc_wunderground_daily_high_v4.json")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "shadow" / "resolution_final_labels.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    input_row = _mapping(json.loads(args.input.read_text(encoding="utf-8")))
    authority, authority_audit = load_resolution_authority_contract(args.contract)
    raw = load_market_rule_raw_snapshot(
        archive_dir=args.market_rule_archive_dir,
        manifest_ref=str(input_row.get("market_rule_manifest_ref") or ""),
    )
    parser_result = parse_market_rule_target_contract_extraction(
        raw.get("raw_payload", b""),
        public_slug=str(input_row.get("public_slug") or ""),
        resolution_local_date=str(input_row.get("resolution_local_date") or ""),
    )
    proposal = build_market_resolution_target_contract_proposal(
        authority_contract=authority,
        target_identity=str(input_row.get("target_identity") or ""),
        public_slug=str(input_row.get("public_slug") or ""),
        resolution_local_date=str(input_row.get("resolution_local_date") or ""),
        station_id=str(input_row.get("station_id") or ""),
        resolution_source=str(input_row.get("resolution_source") or ""),
        market_rule_raw_payload=raw.get("raw_payload", b""),
        market_rule_raw_ref=str(raw.get("raw_ref") or ""),
        market_rule_raw_sha256=str(raw.get("raw_sha256") or ""),
        market_rule_extraction=_mapping(parser_result.get("extraction")),
        supersedes_target_contract_sha256=str(input_row.get("supersedes_target_contract_sha256") or ""),
    )
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "authority_audit": authority_audit,
        "raw_manifest_ref": raw.get("manifest_ref"),
        "rule_parser": parser_result,
        "proposal": proposal,
        "boundary": {
            "shadow_only": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }
    if proposal.get("ok") is not True:
        result.update({"ok": False, "state": "blocked", "block_reasons": proposal.get("block_reasons") or []})
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    if not args.apply:
        result.update({"ok": True, "state": "plan_only", "confirmation_phrase_required": CONFIRMATION_PHRASE})
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.confirm != CONFIRMATION_PHRASE:
        result.update({"ok": False, "state": "blocked", "block_reasons": ["confirmation_phrase_invalid"]})
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    db = _safe_shadow_db(args.db)
    with sqlite3.connect(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        append = persist_resolution_target_contract_shadow(conn, proposal, created_at=_now_utc())
        conn.commit()
    result.update({"ok": True, "state": "appended", "append": append, "db": str(db)})
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
