"""Capture Wunderground final S only after an immutable market-finality receipt."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from market_resolution_finality_shadow_writer import list_market_resolution_finality_attestations_shadow
from resolution_final_source_dispatcher import build_wunderground_final_settlement_source_proposal
from resolution_final_source_runtime import SETTLEMENT_SOURCE_ID
from resolution_final_source_shadow_writer import SOURCE_TABLE, persist_final_source_observation_shadow
from resolution_target_contract_shadow_writer import list_latest_resolution_target_contracts_shadow
from wunderground_final_raw_archive import (
    fetch_and_archive_wunderground_daily_raw_capture,
    load_wunderground_daily_raw_capture,
)


SCHEMA_VERSION = "settlement_final_source_shadow_capture_cycle_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _settlement_already_persisted(conn: sqlite3.Connection, event_date: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {SOURCE_TABLE} WHERE source_id=? AND event_date=? LIMIT 1",
        (SETTLEMENT_SOURCE_ID, _text(event_date)[:10]),
    ).fetchone()
    return row is not None


def run_settlement_final_source_shadow_capture_cycle(
    *,
    shadow_db_path: str | Path,
    wunderground_raw_archive_dir: str | Path,
    authority_contract: Mapping[str, Any],
    captured_at: str,
    max_targets: int = 4,
    fetcher: Callable[..., Mapping[str, Any]] = fetch_and_archive_wunderground_daily_raw_capture,
) -> dict[str, Any]:
    """Append S only when finality and raw/parser lineage all close."""

    path = Path(shadow_db_path).expanduser().resolve()
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["settlement_final_source_shadow_registry_missing"]}
    try:
        with sqlite3.connect(path) as conn:
            latest_contracts = list_latest_resolution_target_contracts_shadow(conn)
            contracts = {
                _text(row.get("target_contract_sha256")): row
                for row in latest_contracts
            }
            latest_contract_by_target_day = {
                (_text(row.get("target_identity")), _text(row.get("resolution_local_date"))[:10]): row
                for row in latest_contracts
            }
            attestations = list_market_resolution_finality_attestations_shadow(conn)[: max(1, int(max_targets))]
            existing_days = {
                _text(item.get("event_date")): _settlement_already_persisted(conn, _text(item.get("event_date")))
                for item in attestations
            }
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"settlement_final_source_preflight_failed:{type(exc).__name__}"]}

    outcomes: list[dict[str, Any]] = []
    for attestation in attestations:
        contract_sha = _text(attestation.get("target_contract_sha256"))
        day = _text(attestation.get("event_date"))[:10]
        base = {"target_contract_sha256": contract_sha, "event_date": day}
        contract = contracts.get(contract_sha)
        if not contract:
            latest = latest_contract_by_target_day.get((_text(attestation.get("target_identity")), day))
            if latest is not None:
                outcomes.append({
                    **base,
                    "state": "skipped_superseded_target_contract_finality",
                    "latest_target_contract_sha256": _text(latest.get("target_contract_sha256")),
                })
                continue
            outcomes.append({**base, "state": "blocked", "block_reasons": ["settlement_final_source_target_contract_missing"]})
            continue
        if existing_days.get(day):
            outcomes.append({**base, "state": "skipped_existing_settlement_source"})
            continue
        try:
            archived = dict(
                fetcher(
                    archive_dir=wunderground_raw_archive_dir,
                    event_date=day,
                    authority_contract=authority_contract,
                )
            )
            if archived.get("ok") is not True:
                raise ValueError("wunderground_raw_capture_not_ok")
            raw = load_wunderground_daily_raw_capture(
                archive_dir=wunderground_raw_archive_dir,
                manifest_ref=_text(archived.get("manifest_ref")),
            )
            proposal = build_wunderground_final_settlement_source_proposal(
                authority_contract=authority_contract,
                raw_capture=raw,
                # Parsing happens after the network response and immutable raw
                # load.  The cycle start supplied by the caller is not a valid
                # parser completion timestamp.
                parser_completed_at=_now_utc(),
                market_finality_attestation=attestation,
            )
            if proposal.get("ok") is not True:
                outcomes.append({**base, "state": "pending_settlement_scalar", "block_reasons": proposal.get("block_reasons") or []})
                continue
            observation = (proposal.get("source_result") or {}).get("source_observation")
            with sqlite3.connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if _settlement_already_persisted(conn, day):
                    conn.rollback()
                    outcomes.append({**base, "state": "skipped_concurrent_settlement_source"})
                    continue
                append = persist_final_source_observation_shadow(conn, observation)
                conn.commit()
            outcomes.append({**base, "state": "appended_settlement_source", "append": append})
        except Exception as exc:
            outcomes.append({**base, "state": "blocked", "block_reasons": [f"settlement_final_source_capture_failed:{type(exc).__name__}"]})
    blockers = [item for item in outcomes if item.get("state") == "blocked"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "settlement_source_appended" if any(item.get("state") == "appended_settlement_source" for item in outcomes) else "pending_or_empty",
        "targets_considered": len(attestations),
        "outcomes": outcomes,
        "boundary": {
            "shadow_only": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "run_settlement_final_source_shadow_capture_cycle"]
