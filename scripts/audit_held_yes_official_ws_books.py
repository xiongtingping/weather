#!/usr/bin/env python3
"""Bounded, read-only CLOB-WS book evidence for active held YES tokens.

This is deliberately separate from the full-scope balance reconciler.  It
never contacts the local order service, alters the held-token inventory, or
turns a ``price_change`` into an initial order book.  Its only input is the
auxiliary official-WebSocket manifest and its exact token shards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.held_yes_registry import (
    classify_held_yes_tokens,
    quote_required_token_ids,
    registry_classification_complete,
)

DEFAULT_INVENTORY = ROOT / "data" / "reported_trajectory_held_yes_tokens.json"
DEFAULT_MANIFEST = ROOT / "data" / "market_ws_held_yes_quotes.json"
DEFAULT_SHARD_DIR = ROOT / "data" / "market_ws_held_yes_quote_shards"
DEFAULT_REGISTRY_DB = ROOT / "data" / "archive.db"
DEFAULT_OUTPUT = (
    ROOT / "output" / "audits" / "reported_trajectory_held_yes_official_ws_book_probe_latest.json"
)

INVENTORY_SCHEMA = "reported_trajectory_held_yes_inventory_v1"
MANIFEST_SCHEMA = "clob_ws_held_yes_quote_manifest_v1"
TOKEN_SCHEMA = "clob_ws_held_yes_token_quote_snapshot_v1"
SOURCE = "official_polymarket_clob_websocket"
MAX_ACTIVE_TOKENS = 64
MAX_INPUT_BYTES = 256 * 1024


def _canonical_sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope_sha(token_ids: list[str]) -> str:
    return hashlib.sha256("\x00".join(token_ids).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_mapping(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"symlink_forbidden:{path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_INPUT_BYTES:
        raise RuntimeError(f"input_bytes_invalid:{path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeError(f"mapping_required:{path}")
    return dict(value)


def _ordered_tokens(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    tokens = [str(token).strip() for token in value]
    if not tokens or any(not token for token in tokens):
        return None
    return tokens if tokens == sorted(set(tokens)) else None


def _snapshot_hash_valid(value: Mapping[str, Any]) -> bool:
    supplied = value.get("snapshot_sha256")
    unsigned = {key: item for key, item in value.items() if key != "snapshot_sha256"}
    return isinstance(supplied, str) and len(supplied) == 64 and supplied == _canonical_sha(unsigned)


def _quote_is_complete_book(quote: Any, token_id: str) -> tuple[bool, list[str]]:
    if not isinstance(quote, Mapping):
        return False, ["quote_missing"]
    reasons: list[str] = []
    if quote.get("token_id") != token_id:
        reasons.append("quote_token_mismatch")
    if quote.get("source") != "clob_ws":
        reasons.append("quote_source_not_clob_ws")
    # `price_change` only remains usable *after* a full book has been
    # materialized.  This receipt therefore never accepts it as an initial
    # book, even if it happens to contain bid/ask-looking fields.
    if quote.get("source_detail") != "book":
        reasons.append("initial_complete_book_required")
    if not isinstance(quote.get("bids"), list) or not isinstance(quote.get("asks"), list):
        reasons.append("book_sides_missing")
    if not isinstance(quote.get("book_observed_at"), str) or not quote.get("book_observed_at"):
        reasons.append("book_observed_at_missing")
    return not reasons, reasons


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _registry_classification(
    *, registry_path: Path, token_ids: list[str],
) -> list[dict[str, Any]]:
    if registry_path.is_symlink() or not registry_path.is_file():
        raise RuntimeError("held_yes_registry_path_invalid")
    with sqlite3.connect(f"file:{registry_path}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return classify_held_yes_tokens(
            conn, token_ids=token_ids, official_source_lane="awc"
        )


def run(
    *,
    inventory_path: Path,
    manifest_path: Path,
    shard_dir: Path,
    output_path: Path,
    registry_path: Path = DEFAULT_REGISTRY_DB,
) -> dict[str, Any]:
    inventory = _read_mapping(inventory_path)
    nonzero_tokens = _ordered_tokens(inventory.get("active_token_ids"))
    if (
        inventory.get("schema") != INVENTORY_SCHEMA
        or not nonzero_tokens
        or len(nonzero_tokens) > MAX_ACTIVE_TOKENS
    ):
        raise RuntimeError("active_held_yes_inventory_invalid")
    try:
        registry_records = _registry_classification(
            registry_path=registry_path, token_ids=nonzero_tokens
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        registry_records = [{
            "token_id": token_id,
            "classification": "REGISTRY_UNRESOLVED",
            "registry_resolved": False,
            "quote_required": False,
            "tradable": False,
            "non_tradeable_reasons": ["official_registry_classification_unavailable"],
            "registry": None,
        } for token_id in nonzero_tokens]
    quote_required_tokens = sorted(quote_required_token_ids(registry_records))
    classification_complete = registry_classification_complete(registry_records)
    active_tokens = quote_required_tokens

    manifest = _read_mapping(manifest_path) if active_tokens else {}
    expected_scope_sha = _scope_sha(active_tokens)
    manifest_scope = _ordered_tokens(manifest.get("scope_token_ids"))
    manifest_valid = bool(
        active_tokens
        and manifest.get("schema") == MANIFEST_SCHEMA
        and manifest.get("source") == SOURCE
        and manifest.get("evidence_only") is True
        and manifest.get("authorizes_order") is False
        # A complete scope is only usable from the listener's explicit
        # `ready` state.  In particular, an old root that still names a
        # shard cannot bridge a reconnect marked `missing_evidence`.
        and manifest.get("state") == "ready"
        and manifest_scope == active_tokens
        and manifest.get("scope_sha256") == expected_scope_sha
        and _snapshot_hash_valid(manifest)
    )
    declared_complete = {
        str(token).strip() for token in manifest.get("complete_book_token_ids") or []
        if str(token).strip()
    }
    rows: list[dict[str, Any]] = []
    for token_id in active_tokens:
        shard_path = shard_dir / f"{hashlib.sha256(token_id.encode('utf-8')).hexdigest()}.json"
        shard: dict[str, Any] | None = None
        shard_error = ""
        try:
            shard = _read_mapping(shard_path)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            shard_error = f"{type(exc).__name__}:{exc}"
        shard_valid = False
        quote_valid = False
        quote_reasons: list[str] = []
        if shard is not None:
            shard_valid = bool(
                shard.get("schema") == TOKEN_SCHEMA
                and shard.get("token_id") == token_id
                and shard.get("scope_token_ids") == active_tokens
                and shard.get("scope_sha256") == expected_scope_sha
                and shard.get("source") == SOURCE
                and shard.get("evidence_only") is True
                and shard.get("authorizes_order") is False
                and _snapshot_hash_valid(shard)
            )
            quote_valid, quote_reasons = _quote_is_complete_book(shard.get("quote"), token_id)
        complete = bool(manifest_valid and token_id in declared_complete and shard_valid and quote_valid)
        reasons: list[str] = []
        if not manifest_valid:
            reasons.append("auxiliary_official_ws_manifest_invalid")
        if token_id not in declared_complete:
            reasons.append("official_ws_initial_complete_book_not_received")
        if not shard_valid:
            reasons.append("official_ws_token_shard_missing_or_invalid")
        reasons.extend(quote_reasons)
        if shard_error:
            reasons.append("token_shard_read_failed")
        rows.append({
            "token_id": token_id,
            "complete_official_book": complete,
            "shard_path": str(shard_path),
            "shard_snapshot_sha256": shard.get("snapshot_sha256") if shard else None,
            "shard_read_error": shard_error or None,
            "blocking_reasons": sorted(set(reasons)),
        })
    books_complete = bool(
        (not active_tokens)
        or (manifest_valid and all(row["complete_official_book"] for row in rows))
    )
    complete = bool(classification_complete and books_complete)
    result: dict[str, Any] = {
        "schema": "reported_trajectory_held_yes_official_ws_book_probe_v1",
        "observed_at": _now(),
        "source": SOURCE,
        "nonzero_token_ids": nonzero_tokens,
        "quote_required_token_ids": active_tokens,
        "scope_token_ids": active_tokens,
        "scope_sha256": expected_scope_sha,
        "inventory_path": str(inventory_path),
        "inventory_active_projection_source": inventory.get("active_token_ids_source"),
        "inventory_active_projection_receipt_sha256": inventory.get("active_token_ids_receipt_sha256"),
        "manifest_path": str(manifest_path),
        "manifest_snapshot_sha256": manifest.get("snapshot_sha256"),
        "manifest_connection_observed_at": manifest.get("connection_observed_at"),
        "manifest_state": manifest.get("state"),
        "manifest_complete_book_token_ids": sorted(declared_complete),
        "registry_classification_complete": classification_complete,
        "registry_classification": registry_records,
        "rows": rows,
        "complete": complete,
        "state": (
            "READY_NO_ORDER_AUTHORIZATION"
            if complete and active_tokens
            else "READY_NONTRADABLE_HELD_YES_NO_ORDER_AUTHORIZATION"
            if complete
            else "BLOCKED_FAIL_CLOSED"
        ),
        "fail_closed": not complete,
        "active_projection_unchanged": True,
        "evidence_only": True,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "price_change_used_as_initial_book": False,
    }
    result["receipt_sha256"] = _canonical_sha(result)
    _atomic_write(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--shard-dir", type=Path, default=DEFAULT_SHARD_DIR)
    parser.add_argument("--registry-db", type=Path, default=DEFAULT_REGISTRY_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        result = run(
            inventory_path=args.inventory.expanduser().resolve(),
            manifest_path=args.manifest.expanduser().resolve(),
            shard_dir=args.shard_dir.expanduser().resolve(),
            output_path=args.output.expanduser().resolve(),
            registry_path=args.registry_db.expanduser().resolve(),
        )
        print(json.dumps({
            key: result[key] for key in (
                "schema", "state", "complete", "scope_token_ids", "receipt_sha256",
                "calls_order_gateway", "orders_submitted",
            )
        }, ensure_ascii=False, sort_keys=True))
        return 0 if result["complete"] else 2
    except Exception as exc:
        print(json.dumps({
            "schema": "reported_trajectory_held_yes_official_ws_book_probe_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "reason": f"{type(exc).__name__}:{exc}",
            "calls_order_gateway": False,
            "orders_submitted": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
