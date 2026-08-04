from __future__ import annotations

import hashlib
import json
import sqlite3

from scripts import audit_held_yes_official_ws_books as probe


def _write_json(path, value) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _signed(value: dict) -> dict:
    return {**value, "snapshot_sha256": probe._canonical_sha(value)}


def _fixture(tmp_path, *, source_detail: str = "book"):
    tokens = ["111", "222"]
    inventory = tmp_path / "inventory.json"
    manifest = tmp_path / "manifest.json"
    shard_dir = tmp_path / "shards"
    registry = tmp_path / "registry.db"
    shard_dir.mkdir()
    with sqlite3.connect(registry) as conn:
        conn.execute(
            """CREATE TABLE reported_trajectory_market_registry(
                 yes_token TEXT,station_id TEXT,event_date TEXT,slug TEXT,
                 outcome TEXT,source_lane TEXT,source_eligible INTEGER,
                 market_active INTEGER,market_accepting_orders INTEGER,
                 refreshed_at TEXT
               )"""
        )
        conn.executemany(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (token, "KAAA", "2026-08-01", f"market-{token}", "30C", "awc", 1, 1, 1, "2026-08-01T00:00:00Z")
                for token in tokens
            ],
        )
    _write_json(inventory, {
        "schema": probe.INVENTORY_SCHEMA,
        "active_token_ids": tokens,
        "active_token_ids_source": "official_local_clob_execution_guard_snapshot",
        "active_token_ids_receipt_sha256": "0" * 64,
    })
    manifest_core = {
        "schema": probe.MANIFEST_SCHEMA,
        "state": "ready",
        "connection_observed_at": "2026-08-01T00:00:00Z",
        "scope_token_ids": tokens,
        "scope_sha256": probe._scope_sha(tokens),
        "complete_book_token_ids": tokens,
        "quote_storage": "sha256_token_shards_v1",
        "source": probe.SOURCE,
        "evidence_only": True,
        "authorizes_order": False,
    }
    _write_json(manifest, _signed(manifest_core))
    for token_id in tokens:
        quote = {
            "token_id": token_id,
            "source": "clob_ws",
            "source_detail": source_detail,
            "book_observed_at": "2026-08-01T00:00:00Z",
            "bids": [{"price": 0.5, "size": 3}],
            "asks": [{"price": 0.6, "size": 4}],
        }
        shard_core = {
            "schema": probe.TOKEN_SCHEMA,
            "token_id": token_id,
            "scope_token_ids": tokens,
            "scope_sha256": probe._scope_sha(tokens),
            "quote": quote,
            "source": probe.SOURCE,
            "evidence_only": True,
            "authorizes_order": False,
        }
        filename = hashlib.sha256(token_id.encode("utf-8")).hexdigest() + ".json"
        _write_json(shard_dir / filename, _signed(shard_core))
    return inventory, manifest, shard_dir, registry


def test_complete_initial_books_are_evidence_only_and_never_authorize_order(tmp_path) -> None:
    inventory, manifest, shard_dir, registry = _fixture(tmp_path)
    result = probe.run(
        inventory_path=inventory,
        manifest_path=manifest,
        shard_dir=shard_dir,
        output_path=tmp_path / "receipt.json",
        registry_path=registry,
    )

    assert result["complete"] is True
    assert result["state"] == "READY_NO_ORDER_AUTHORIZATION"
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0
    assert result["active_projection_unchanged"] is True


def test_price_change_cannot_be_promoted_to_an_initial_book(tmp_path) -> None:
    inventory, manifest, shard_dir, registry = _fixture(tmp_path, source_detail="price_change")
    result = probe.run(
        inventory_path=inventory,
        manifest_path=manifest,
        shard_dir=shard_dir,
        output_path=tmp_path / "receipt.json",
        registry_path=registry,
    )

    assert result["complete"] is False
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["price_change_used_as_initial_book"] is False
    assert all(
        "initial_complete_book_required" in row["blocking_reasons"]
        for row in result["rows"]
    )


def test_closed_held_yes_is_explicitly_nontradeable_and_needs_no_ws_book(tmp_path) -> None:
    inventory, manifest, shard_dir, registry = _fixture(tmp_path)
    with sqlite3.connect(registry) as conn:
        conn.execute(
            "UPDATE reported_trajectory_market_registry "
            "SET market_active=0,market_accepting_orders=0"
        )

    result = probe.run(
        inventory_path=inventory,
        manifest_path=manifest,
        shard_dir=shard_dir,
        output_path=tmp_path / "receipt.json",
        registry_path=registry,
    )

    assert result["complete"] is True
    assert result["quote_required_token_ids"] == []
    assert result["rows"] == []
    assert result["state"] == "READY_NONTRADABLE_HELD_YES_NO_ORDER_AUTHORIZATION"
    assert all(
        row["classification"] == "REGISTRY_NONTRADABLE"
        and row["tradable"] is False
        and set(row["non_tradeable_reasons"])
        == {"registry_market_inactive", "registry_market_not_accepting_orders"}
        for row in result["registry_classification"]
    )
