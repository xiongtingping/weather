"""Forward-only sidecar that captures and registers new Chengdu target rules.

It reads only discovery identity rows from ``archive.db`` and writes only the
isolated target-rule raw archive plus target-contract registry.  It never
reads market prices/outcomes, never writes production facts and never enters a
posterior or order path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from market_rule_raw_archive import fetch_and_archive_market_rule_raw_snapshot, load_market_rule_raw_snapshot
from market_rule_target_contract_parser import parse_market_rule_target_contract_extraction
from resolution_authority_contract_runtime import validate_resolution_authority_contract
from resolution_target_capture_cursor import load_resolution_target_capture_not_before
from resolution_target_contract_runtime import build_market_resolution_target_contract_proposal
from resolution_target_contract_shadow_writer import (
    load_latest_resolution_target_contract_shadow,
    persist_resolution_target_contract_shadow,
)


SCHEMA_VERSION = "target_rule_shadow_capture_cycle_v1"
TARGET_IDENTITY_PREFIX = "market_resolution_target_v1:"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_forward_chengdu_targets(*, archive_db_path: str | Path, not_before: str, max_targets: int) -> list[dict[str, str]]:
    path = Path(archive_db_path).expanduser().resolve()
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            """
            SELECT slug,event_date,first_seen_at
            FROM weather_market_discovery
            WHERE lower(slug) LIKE 'highest-temperature-in-chengdu-%'
              AND first_seen_at >= ?
            ORDER BY first_seen_at,slug
            LIMIT ?
            """,
            (_text(not_before), max(1, int(max_targets))),
        ).fetchall()
    return [
        {"slug": _text(row[0]), "event_date": _text(row[1])[:10], "first_seen_at": _text(row[2])}
        for row in rows
        if _text(row[0]) and _text(row[1])
    ]


def run_target_rule_shadow_capture_cycle(
    *,
    archive_db_path: str | Path,
    shadow_db_path: str | Path,
    market_rule_archive_dir: str | Path,
    authority_contract: Mapping[str, Any],
    captured_at: str,
    max_targets: int = 8,
    fetcher: Callable[..., Mapping[str, Any]] = fetch_and_archive_market_rule_raw_snapshot,
) -> dict[str, Any]:
    """Capture only targets first discovered after the immutable watermark."""

    shadow_path = Path(shadow_db_path).expanduser().resolve()
    if not shadow_path.exists():
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["target_rule_shadow_registry_missing"],
            "writes_archive_db": False,
            "places_orders": False,
        }
    authority_audit = validate_resolution_authority_contract(authority_contract)
    if authority_audit.get("ok") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": list(authority_audit.get("blockers") or ["resolution_authority_contract_invalid"]),
            "writes_archive_db": False,
            "places_orders": False,
        }
    authority_sha256 = _text(authority_audit.get("contract_sha256"))
    try:
        with sqlite3.connect(shadow_path) as shadow:
            not_before = load_resolution_target_capture_not_before(shadow)
            targets = _read_forward_chengdu_targets(
                archive_db_path=archive_db_path,
                not_before=not_before,
                max_targets=max_targets,
            )
            existing = {
                (row["slug"], row["event_date"]): load_latest_resolution_target_contract_shadow(
                    shadow,
                    target_identity=f"{TARGET_IDENTITY_PREFIX}{row['slug']}",
                    resolution_local_date=row["event_date"],
                )
                for row in targets
            }
    except Exception as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": [f"target_rule_shadow_capture_preflight_failed:{type(exc).__name__}"],
            "writes_archive_db": False,
            "places_orders": False,
        }

    outcomes: list[dict[str, Any]] = []
    for target in targets:
        slug, day = target["slug"], target["event_date"]
        target_identity = f"{TARGET_IDENTITY_PREFIX}{slug}"
        predecessor = existing[(slug, day)]
        if predecessor is not None and _text(predecessor.get("authority_contract_sha256")) == authority_sha256:
            outcomes.append({**target, "state": "skipped_existing_target_contract"})
            continue
        supersedes_sha256 = _text(_mapping(predecessor).get("target_contract_sha256"))
        try:
            archived = dict(
                fetcher(
                    archive_dir=market_rule_archive_dir,
                    target_identity=target_identity,
                    public_slug=slug,
                    resolution_local_date=day,
                )
            )
            if archived.get("ok") is not True:
                raise ValueError("raw_capture_not_ok")
            raw = load_market_rule_raw_snapshot(
                archive_dir=market_rule_archive_dir,
                manifest_ref=_text(archived.get("manifest_ref")),
            )
            parsed = parse_market_rule_target_contract_extraction(
                raw.get("raw_payload", b""), public_slug=slug, resolution_local_date=day
            )
            proposal = build_market_resolution_target_contract_proposal(
                authority_contract=authority_contract,
                target_identity=target_identity,
                public_slug=slug,
                resolution_local_date=day,
                station_id="ZUUU",
                resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                market_rule_raw_payload=raw.get("raw_payload", b""),
                market_rule_raw_ref=_text(raw.get("raw_ref")),
                market_rule_raw_sha256=_text(raw.get("raw_sha256")),
                market_rule_extraction=parsed.get("extraction") if parsed.get("ok") is True else {},
                supersedes_target_contract_sha256=supersedes_sha256,
            )
            if proposal.get("ok") is not True:
                outcomes.append({**target, "state": "blocked", "block_reasons": proposal.get("block_reasons") or []})
                continue
            with sqlite3.connect(shadow_path) as shadow:
                shadow.execute("BEGIN IMMEDIATE")
                concurrent_latest = load_latest_resolution_target_contract_shadow(
                    shadow, target_identity=target_identity, resolution_local_date=day
                )
                if concurrent_latest is not None and _text(concurrent_latest.get("authority_contract_sha256")) == authority_sha256:
                    shadow.rollback()
                    outcomes.append({**target, "state": "skipped_concurrent_target_contract"})
                    continue
                if _text(_mapping(concurrent_latest).get("target_contract_sha256")) != supersedes_sha256:
                    shadow.rollback()
                    outcomes.append({**target, "state": "blocked", "block_reasons": ["target_rule_shadow_concurrent_revision_drift"]})
                    continue
                append = persist_resolution_target_contract_shadow(shadow, proposal, created_at=captured_at)
                shadow.commit()
            outcomes.append(
                {
                    **target,
                    "state": "appended_successor" if supersedes_sha256 else "appended",
                    "append": append,
                    "raw_manifest_ref": raw.get("manifest_ref"),
                    "authority_contract_sha256": authority_sha256,
                    "supersedes_target_contract_sha256": supersedes_sha256,
                }
            )
        except Exception as exc:
            outcomes.append({**target, "state": "blocked", "block_reasons": [f"target_rule_shadow_capture_failed:{type(exc).__name__}"]})
    blockers = [item for item in outcomes if item.get("state") == "blocked"]
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "captured" if outcomes else "no_forward_targets",
        "forward_capture_not_before": not_before,
        "targets_considered": len(targets),
        "outcomes": outcomes,
        "boundary": {
            "reads_archive_db_identity_only": True,
            "writes_archive_db": False,
            "writes_shadow_registry_only": True,
            "does_not_read_market_outcome": True,
            "does_not_read_market_price": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "TARGET_IDENTITY_PREFIX", "run_target_rule_shadow_capture_cycle"]
