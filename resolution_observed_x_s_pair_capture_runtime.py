"""Shadow-only capture cycle for raw final X/S calibration pairs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from market_resolution_finality_shadow_writer import list_market_resolution_finality_attestations_shadow
from resolution_final_source_runtime import PHYSICAL_SOURCE_ID, SETTLEMENT_SOURCE_ID
from resolution_final_source_shadow_writer import SOURCE_TABLE
from resolution_observed_x_s_pair_shadow import PAIR_TABLE, build_observed_x_s_pair, persist_observed_x_s_pair_shadow
from resolution_target_contract_shadow_writer import list_latest_resolution_target_contracts_shadow


SCHEMA_VERSION = "resolution_observed_x_s_pair_capture_cycle_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _load_sources(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT source_id,event_date,observation_json FROM {SOURCE_TABLE}
            WHERE source_id IN (?,?) ORDER BY event_date,source_id""",
        (PHYSICAL_SOURCE_ID, SETTLEMENT_SOURCE_ID),
    ).fetchall()
    return {(_text(row[0]), _text(row[1])[:10]): dict(json.loads(row[2])) for row in rows}


def _pair_exists(conn: sqlite3.Connection, *, contract_sha: str, event_date: str) -> bool:
    return bool(conn.execute(
        f"SELECT 1 FROM {PAIR_TABLE} WHERE target_contract_sha256=? AND event_date=? LIMIT 1",
        (_text(contract_sha), _text(event_date)[:10]),
    ).fetchone())


def run_resolution_observed_x_s_pair_capture_cycle(
    *, shadow_db_path: str | Path, captured_at: str, max_targets: int = 4
) -> dict[str, Any]:
    """Append pairs only after both final sources and finality are present."""

    path = Path(shadow_db_path).expanduser().resolve()
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["observed_x_s_pair_shadow_db_missing"]}
    try:
        with sqlite3.connect(path) as conn:
            contracts = {_text(row.get("target_contract_sha256")): row for row in list_latest_resolution_target_contracts_shadow(conn)}
            attestations = list_market_resolution_finality_attestations_shadow(conn)[: max(1, int(max_targets))]
            sources = _load_sources(conn)
            existing = {(_text(row.get("target_contract_sha256")), _text(row.get("event_date"))[:10]) for row in attestations if _pair_exists(conn, contract_sha=_text(row.get("target_contract_sha256")), event_date=_text(row.get("event_date")))}
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"observed_x_s_pair_preflight_failed:{type(exc).__name__}"]}
    outcomes: list[dict[str, Any]] = []
    for attestation in attestations:
        contract_sha = _text(attestation.get("target_contract_sha256"))
        day = _text(attestation.get("event_date"))[:10]
        base = {"target_contract_sha256": contract_sha, "event_date": day}
        if (contract_sha, day) in existing:
            outcomes.append({**base, "state": "skipped_existing_observed_x_s_pair"})
            continue
        contract = contracts.get(contract_sha)
        physical = sources.get((PHYSICAL_SOURCE_ID, day))
        settlement = sources.get((SETTLEMENT_SOURCE_ID, day))
        if not contract or not physical or not settlement:
            missing = [name for name, value in (("target_contract", contract), ("physical_x", physical), ("settlement_s", settlement)) if not value]
            outcomes.append({**base, "state": "pending_final_sources_or_contract", "block_reasons": [f"observed_x_s_pair_{name}_missing" for name in missing]})
            continue
        pair = build_observed_x_s_pair(
            target_contract=contract,
            market_finality_attestation=attestation,
            physical_final_source=physical,
            settlement_final_source=settlement,
            captured_at=captured_at,
        )
        if pair.get("ok") is not True:
            outcomes.append({**base, "state": "blocked", "block_reasons": pair.get("block_reasons") or []})
            continue
        try:
            with sqlite3.connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if _pair_exists(conn, contract_sha=contract_sha, event_date=day):
                    conn.rollback()
                    outcomes.append({**base, "state": "skipped_concurrent_observed_x_s_pair"})
                    continue
                append = persist_observed_x_s_pair_shadow(conn, pair)
                conn.commit()
            outcomes.append({**base, "state": "appended_observed_x_s_pair", "append": append})
        except Exception as exc:
            outcomes.append({**base, "state": "blocked", "block_reasons": [f"observed_x_s_pair_append_failed:{type(exc).__name__}"]})
    blockers = [row for row in outcomes if row.get("state") == "blocked"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "observed_x_s_pair_appended" if any(row.get("state") == "appended_observed_x_s_pair" for row in outcomes) else "pending_or_empty",
        "targets_considered": len(attestations),
        "outcomes": outcomes,
        "boundary": {"shadow_only": True, "bridge_calibration_only": True, "writes_archive_db": False, "writes_posterior": False, "places_orders": False},
    }


__all__ = ["SCHEMA_VERSION", "run_resolution_observed_x_s_pair_capture_cycle"]
