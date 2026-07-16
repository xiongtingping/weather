"""Forward-only capture of resolved-market attestations for frozen targets."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import URLError
from zoneinfo import ZoneInfo

from market_resolution_finality_runtime import build_market_resolution_finality_attestation_from_finality_capture
from market_rule_raw_archive import (
    fetch_and_archive_market_rule_raw_snapshot,
    load_market_rule_raw_object,
    load_market_rule_raw_snapshot,
)
from market_resolution_finality_shadow_writer import (
    load_market_resolution_finality_attestation_shadow,
    persist_market_resolution_finality_attestation_shadow,
)
from resolution_target_contract_shadow_writer import list_latest_resolution_target_contracts_shadow


SCHEMA_VERSION = "market_finality_shadow_capture_cycle_v1"
RESOLUTION_TIMEZONE = "Asia/Shanghai"
PENDING_PARSER_REASONS = {
    "market_finality_capture_parser:market_finality_parser_event_not_closed",
    "market_finality_capture_parser:market_finality_parser_not_all_markets_resolved",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_aware_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _finality_poll_not_before(*, resolution_local_date: str) -> datetime | None:
    try:
        day = datetime.fromisoformat(_text(resolution_local_date)[:10]).date()
    except ValueError:
        return None
    # ZUUU daily-high truth is a Chengdu station-local day.  Gamma finality
    # must never be polled before that physical day has ended; resolution may
    # occur later and is handled by the normal pending/retry loop.
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=ZoneInfo(RESOLUTION_TIMEZONE))


def run_market_finality_shadow_capture_cycle(
    *,
    shadow_db_path: str | Path,
    market_rule_archive_dir: str | Path,
    authority_contract: Mapping[str, Any],
    captured_at: str,
    max_targets: int = 8,
    fetcher: Callable[..., Mapping[str, Any]] = fetch_and_archive_market_rule_raw_snapshot,
) -> dict[str, Any]:
    """Poll only frozen L0 contracts; pending market state is not an error."""

    path = Path(shadow_db_path).expanduser().resolve()
    if not path.exists():
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["market_finality_shadow_registry_missing"]}
    captured = _parse_aware_datetime(captured_at)
    if captured is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["market_finality_shadow_capture_time_invalid"],
        }
    try:
        with sqlite3.connect(path) as conn:
            contracts = list_latest_resolution_target_contracts_shadow(conn)[: max(1, int(max_targets))]
            existing = {
                _text(contract.get("target_contract_sha256")): load_market_resolution_finality_attestation_shadow(
                    conn,
                    target_contract_sha256=_text(contract.get("target_contract_sha256")),
                    event_date=_text(contract.get("resolution_local_date")),
                )
                for contract in contracts
            }
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"market_finality_shadow_capture_preflight_failed:{type(exc).__name__}"]}
    outcomes: list[dict[str, Any]] = []
    for contract in contracts:
        contract_sha = _text(contract.get("target_contract_sha256"))
        slug = _text(contract.get("public_slug"))
        target_identity = _text(contract.get("target_identity"))
        day = _text(contract.get("resolution_local_date"))[:10]
        base = {"target_contract_sha256": contract_sha, "public_slug": slug, "event_date": day}
        if existing.get(contract_sha) is not None:
            outcomes.append({**base, "state": "skipped_existing_finality"})
            continue
        not_before = _finality_poll_not_before(resolution_local_date=day)
        if not_before is None:
            outcomes.append({**base, "state": "blocked", "block_reasons": ["market_finality_resolution_local_date_invalid"]})
            continue
        if captured.astimezone(ZoneInfo(RESOLUTION_TIMEZONE)) < not_before:
            outcomes.append(
                {
                    **base,
                    "state": "deferred_event_day_open",
                    "finality_poll_not_before": not_before.isoformat(),
                }
            )
            continue
        try:
            original = load_market_rule_raw_object(
                archive_dir=market_rule_archive_dir,
                raw_ref=_text((contract.get("market_rule_snapshot") or {}).get("raw_ref")),
            )
            archived = dict(
                fetcher(
                    archive_dir=market_rule_archive_dir,
                    target_identity=target_identity,
                    public_slug=slug,
                    resolution_local_date=day,
                )
            )
            if archived.get("ok") is not True:
                raise ValueError("market_finality_raw_capture_not_ok")
            capture = load_market_rule_raw_snapshot(
                archive_dir=market_rule_archive_dir,
                manifest_ref=_text(archived.get("manifest_ref")),
            )
            attestation = build_market_resolution_finality_attestation_from_finality_capture(
                authority_contract=authority_contract,
                target_contract=contract,
                market_rule_raw_payload=original.get("raw_payload", b""),
                finality_raw_capture=capture,
            )
            if attestation.get("ok") is not True:
                reasons = list(attestation.get("block_reasons") or [])
                pending = bool(reasons) and set(reasons).issubset(PENDING_PARSER_REASONS)
                outcomes.append({**base, "state": "pending" if pending else "blocked", "block_reasons": reasons})
                continue
            with sqlite3.connect(path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if load_market_resolution_finality_attestation_shadow(
                    conn, target_contract_sha256=contract_sha, event_date=day
                ) is not None:
                    conn.rollback()
                    outcomes.append({**base, "state": "skipped_concurrent_finality"})
                    continue
                append = persist_market_resolution_finality_attestation_shadow(conn, attestation, created_at=captured_at)
                conn.commit()
            outcomes.append({**base, "state": "attested", "append": append})
        except (URLError, TimeoutError) as exc:
            outcomes.append(
                {
                    **base,
                    "state": "retryable_capture_error",
                    "retry_reason": f"market_finality_shadow_capture_retryable:{type(exc).__name__}",
                }
            )
        except Exception as exc:
            outcomes.append({**base, "state": "blocked", "block_reasons": [f"market_finality_shadow_capture_failed:{type(exc).__name__}"]})
    blockers = [row for row in outcomes if row.get("state") == "blocked"]
    retryable = [row for row in outcomes if row.get("state") == "retryable_capture_error"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "finality_attested" if any(row.get("state") == "attested" for row in outcomes) else "pending_or_empty",
        "targets_considered": len(contracts),
        "targets_polled": sum(
            row.get("state")
            not in {"deferred_event_day_open", "skipped_existing_finality"}
            for row in outcomes
        ),
        "retryable_capture_error_count": len(retryable),
        "outcomes": outcomes,
        "boundary": {
            "shadow_only": True,
            "resolution_timezone": RESOLUTION_TIMEZONE,
            "poll_before_station_local_day_end": False,
            "retryable_network_error_blocks_live_probability": False,
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "run_market_finality_shadow_capture_cycle"]
