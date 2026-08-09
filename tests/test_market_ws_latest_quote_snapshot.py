from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from scripts import market_ws_listener as listener
from scripts.market_ws_listener import (
    LatestQuoteSnapshot,
    TokenBinding,
    _iter_payload_messages,
    rows_from_ws_message,
)


def test_registry_read_only_context_manager_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "registry.db"
    with sqlite3.connect(db) as setup:
        setup.execute("CREATE TABLE marker(value INTEGER)")
    monkeypatch.setenv("PM_MARKET_WS_DB_PATH", str(db))
    with listener._open_registry_ro() as conn:
        assert conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_target_activation_enables_all_yes_book_evidence(monkeypatch, tmp_path) -> None:
    activation = tmp_path / "target.json"
    activation.write_text(
        json.dumps(
            {
                "schema": "bayes_basket_target_production_no_live_activation_v1",
                "state": "production_no_live",
                "execution_mode": "production_no_live",
                "target_only": True,
                "production_mutation": False,
                "live_decision_write_authorized": False,
                "live_order_authorized": False,
                "live_submit_authorized": False,
                "places_orders": False,
                "writes_production_db": False,
                "calls_order_gateway": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(listener, "TARGET_ACTIVATION_PATH", activation)
    assert listener._target_yes_quotes_enabled() is True

    unsafe = json.loads(activation.read_text(encoding="utf-8"))
    unsafe["live_submit_authorized"] = True
    activation.write_text(
        json.dumps(unsafe),
        encoding="utf-8",
    )
    assert listener._target_yes_quotes_enabled() is False

    legacy = dict(unsafe)
    legacy["live_submit_authorized"] = False
    legacy["schema"] = "basket_plus_hard_no_production_activation_v1"
    activation.write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )
    assert listener._target_yes_quotes_enabled() is False


def test_catalog_marker_is_short_cached_across_shard_probes(monkeypatch):
    class Cursor:
        def fetchone(self):
            return (
                12,
                "2026-07-24T00:00:00Z",
                "2026-07-24",
                "yes-max",
                "no-max",
            )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            calls.append("execute")
            return Cursor()

    calls = []
    listener._BINDING_MARKER_CACHE.clear()
    monkeypatch.setenv("PM_MARKET_WS_CATALOG_MARKER_CACHE_SEC", "60")
    monkeypatch.setattr(listener, "_open_registry_ro", lambda: Connection())

    first = listener._binding_catalog_marker(3, 2000, True)
    second = listener._binding_catalog_marker(3, 2000, True)

    assert first == second
    assert calls == ["execute"]


def test_catalog_marker_rebuilds_when_timezone_scope_can_cross_midnight(monkeypatch):
    class Cursor:
        def fetchone(self):
            return (
                12,
                "2026-07-24T00:00:00Z",
                "2026-07-24",
                "yes-max",
                "no-max",
            )

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            calls.append("execute")
            return Cursor()

    calls = []
    epoch = [1_800_000_000.0]
    listener._BINDING_MARKER_CACHE.clear()
    monkeypatch.setenv("PM_MARKET_WS_CATALOG_MARKER_CACHE_SEC", "3600")
    monkeypatch.setattr(listener, "_open_registry_ro", lambda: Connection())
    monkeypatch.setattr(listener.time, "time", lambda: epoch[0])

    first = listener._binding_catalog_marker(3, 2000, True)
    epoch[0] += 61
    second = listener._binding_catalog_marker(3, 2000, True)

    assert first != second
    assert calls == ["execute", "execute"]


def test_latest_quote_snapshot_is_atomic_hashed_and_non_authorizing(tmp_path) -> None:
    path = tmp_path / "latest.json"
    state = LatestQuoteSnapshot(path, min_write_interval_sec=0)
    binding = TokenBinding("token-yes", "slug", "2026-07-16", "38°C", "YES")
    rows = rows_from_ws_message(
        {
            "event_type": "book", "asset_id": "token-yes",
            "tick_size": "0.001",
            "bids": [{"price": "0.60", "size": "4"}],
            "asks": [{"price": "0.61", "size": "5"}],
        },
        {"token-yes": binding},
        depth_levels=5,
        include_source_details={"book"},
        created_at="2026-07-16T12:00:00.100Z",
    )
    state.connected(0)
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "token-yes",
            "tick_size": "0.001",
            "bids": [{"price": "0.60", "size": "4"}],
            "asks": [{"price": "0.61", "size": "5"}],
        },
        observed_at="2026-07-16T12:00:00.100Z",
        depth_levels=5,
    )
    state.observe(rows, observed_at="2026-07-16T12:00:00.100Z")
    payload = json.loads(path.read_text())
    digest = payload.pop("snapshot_sha256")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert digest == hashlib.sha256(canonical).hexdigest()
    assert payload["state"] == "connected"
    assert payload["authorizes_order"] is False
    assert payload["quotes_by_token"]["token-yes"]["best_bid"] == 0.6
    assert payload["quotes_by_token"]["token-yes"]["ask_depth_topN"] == 5.0
    assert payload["quotes_by_token"]["token-yes"]["asks"] == [{"price": 0.61, "size": 5}]
    assert payload["quotes_by_token"]["token-yes"]["tick_size"] == 0.001


def test_tick_size_change_updates_active_quote_and_hash(tmp_path) -> None:
    path = tmp_path / "latest.json"
    state = LatestQuoteSnapshot(path, min_write_interval_sec=0)
    state.connected(0, ["token"])
    state.observe_market_message(
        {
            "event_type": "book",
            "asset_id": "token",
            "tick_size": "0.01",
            "bids": [{"price": "0.40", "size": "2"}],
            "asks": [{"price": "0.42", "size": "3"}],
        },
        observed_at="2026-07-16T12:00:00.000Z",
        depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "token",
            "slug": "s",
            "event_date": "2026-07-16",
            "outcome": "38°C",
            "side": "YES",
            "best_bid": 0.40,
            "best_ask": 0.42,
            "spread": 0.02,
            "bid_depth_topN": 2,
            "ask_depth_topN": 3,
            "source": "clob_ws",
            "source_detail": "book",
            "created_at": "2026-07-16T12:00:00.000Z",
        }],
        observed_at="2026-07-16T12:00:00.000Z",
    )
    before = json.loads(path.read_text())
    state.observe_market_message(
        {
            "event_type": "tick_size_change",
            "asset_id": "token",
            "new_tick_size": "0.001",
        },
        observed_at="2026-07-16T12:00:00.100Z",
        depth_levels=5,
    )
    after = json.loads(path.read_text())
    assert before["quotes_by_token"]["token"]["tick_size"] == 0.01
    assert after["quotes_by_token"]["token"]["tick_size"] == 0.001
    assert after["snapshot_sha256"] != before["snapshot_sha256"]


def test_price_change_mutates_full_book_before_atomic_snapshot(tmp_path) -> None:
    path = tmp_path / "latest.json"
    state = LatestQuoteSnapshot(path, min_write_interval_sec=0)
    state.connected(0, ["token"])
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "token",
            "bids": [{"price": "0.40", "size": "2"}],
            "asks": [{"price": "0.42", "size": "3"}],
        }, observed_at="2026-07-16T12:00:00.000Z", depth_levels=5,
    )
    state.observe_market_message(
        {"event_type": "price_change", "price_changes": [
            {"asset_id": "token", "side": "SELL", "price": "0.42", "size": "0"},
            {"asset_id": "token", "side": "SELL", "price": "0.41", "size": "7"},
        ]}, observed_at="2026-07-16T12:00:00.200Z", depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "token", "slug": "s", "event_date": "2026-07-16",
            "outcome": "38°C", "side": "YES", "best_bid": 0.40, "best_ask": 0.41,
            "spread": 0.01, "bid_depth_topN": 0, "ask_depth_topN": 0,
            "source": "clob_ws", "source_detail": "price_change",
            "created_at": "2026-07-16T12:00:00.200Z",
        }], observed_at="2026-07-16T12:00:00.200Z",
    )
    quote = json.loads(path.read_text())["quotes_by_token"]["token"]
    assert quote["asks"] == [{"price": 0.41, "size": 7}]
    assert quote["book_observed_at"] == "2026-07-16T12:00:00.200Z"


def _quiet_book_row(created_at: str) -> dict:
    return {
        "token_id": "token", "slug": "s", "event_date": "2026-07-16",
        "outcome": "38°C", "side": "YES", "best_bid": 0.40, "best_ask": 0.42,
        "spread": 0.02, "bid_depth_topN": 2, "ask_depth_topN": 3,
        "source": "clob_ws", "source_detail": "book", "created_at": created_at,
    }


def _observe_quiet_book(state: LatestQuoteSnapshot, observed_at: str) -> None:
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "token",
            "bids": [{"price": "0.40", "size": "2"}],
            "asks": [{"price": "0.42", "size": "3"}],
        }, observed_at=observed_at, depth_levels=5,
    )
    state.observe([_quiet_book_row(observed_at)], observed_at=observed_at)


def test_unchanged_book_republishes_attestation_once_it_ages(tmp_path) -> None:
    path = tmp_path / "latest.json"
    state = LatestQuoteSnapshot(path, min_write_interval_sec=0)
    state.connected(0, ["token"])
    _observe_quiet_book(state, "2026-07-16T12:00:00.000Z")
    assert json.loads(path.read_text())["quotes_by_token"]["token"][
        "book_observed_at"
    ] == "2026-07-16T12:00:00.000Z"

    # Re-observing the same book inside the interval stays deduplicated.
    _observe_quiet_book(state, "2026-07-16T12:00:05.000Z")
    assert json.loads(path.read_text())["quotes_by_token"]["token"][
        "book_observed_at"
    ] == "2026-07-16T12:00:00.000Z"

    # A book that never moves must not silently age out of every freshness
    # bound downstream; the attestation advances once the interval elapses.
    _observe_quiet_book(state, "2026-07-16T12:00:31.000Z")
    quote = json.loads(path.read_text())["quotes_by_token"]["token"]
    assert quote["book_observed_at"] == "2026-07-16T12:00:31.000Z"
    assert quote["asks"] == [{"price": 0.42, "size": 3}]


def test_unchanged_book_refresh_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_WS_BOOK_FRESHNESS_REFRESH_SEC", "0")
    path = tmp_path / "latest.json"
    state = LatestQuoteSnapshot(path, min_write_interval_sec=0)
    state.connected(0, ["token"])
    _observe_quiet_book(state, "2026-07-16T12:00:00.000Z")
    _observe_quiet_book(state, "2026-07-16T13:00:00.000Z")
    assert json.loads(path.read_text())["quotes_by_token"]["token"][
        "book_observed_at"
    ] == "2026-07-16T12:00:00.000Z"


def test_snapshot_disconnect_is_explicit(tmp_path) -> None:
    path = tmp_path / "latest.json"
    state = LatestQuoteSnapshot(path, min_write_interval_sec=0)
    state.connected(2)
    state.disconnected(2)
    assert json.loads(path.read_text())["state"] == "disconnected"


def test_sharded_snapshot_keeps_manifest_small_and_writes_only_target_quote(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    state.connected(0, ["no-token"])
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "no-token",
            "bids": [{"price": "0.88", "size": "8"}],
            "asks": [{"price": "0.89", "size": "9"}],
        },
        observed_at="2026-07-17T12:00:00.000Z",
        depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "no-token", "slug": "slug", "event_date": "2026-07-17",
            "outcome": "35°C", "side": "NO", "best_bid": 0.88, "best_ask": 0.89,
            "spread": 0.01, "bid_depth_topN": 8, "ask_depth_topN": 9,
            "source": "clob_ws", "source_detail": "book",
            "created_at": "2026-07-17T12:00:00.000Z",
        }],
        observed_at="2026-07-17T12:00:00.000Z",
    )
    payload = json.loads(manifest.read_text())
    assert payload["quote_storage"] == "sha256_token_shards_v1"
    assert payload["quotes_by_token"] == {}
    assert manifest.stat().st_size < 1000
    shard = json.loads((shard_dir / f"{hashlib.sha256(b'no-token').hexdigest()}.json").read_text())
    supplied = shard.pop("snapshot_sha256")
    assert supplied == hashlib.sha256(
        json.dumps(shard, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert shard["token_id"] == "no-token"
    assert shard["quote"]["asks"] == [{"price": 0.89, "size": 9}]
    assert shard["shard_index"] == 0
    assert shard["shard_active"] is True


def test_sharded_token_update_does_not_bypass_large_root_manifest_throttle(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=60)
    token = "throttled-token"
    state.connected(0, [token], expected_shards=1)
    root_before = json.loads(manifest.read_text(encoding="utf-8"))

    state.observe_market_message(
        {
            "event_type": "book", "asset_id": token,
            "bids": [{"price": "0.40", "size": "2"}],
            "asks": [{"price": "0.41", "size": "3"}],
        },
        observed_at="2026-08-01T12:00:00.000Z", depth_levels=5,
    )
    state.observe_with_shard(
        [{
            "token_id": token, "slug": "slug", "event_date": "2026-08-01",
            "outcome": "30°C", "side": "NO", "best_bid": 0.4, "best_ask": 0.41,
            "spread": 0.01, "bid_depth_topN": 2, "ask_depth_topN": 3,
            "source": "clob_ws", "source_detail": "book",
            "created_at": "2026-08-01T12:00:00.000Z",
        }],
        observed_at="2026-08-01T12:00:00.000Z", shard_index=0,
    )

    # The target shard is current immediately, while the shared root remains
    # throttled instead of serialising a large pending-topology list per tick.
    root_after = json.loads(manifest.read_text(encoding="utf-8"))
    shard = json.loads(
        (shard_dir / f"{hashlib.sha256(token.encode()).hexdigest()}.json").read_text()
    )
    assert root_after == root_before
    assert shard["quote"]["book_observed_at"] == "2026-08-01T12:00:00.000Z"
    assert shard["quote"]["asks"] == [{"price": 0.41, "size": 3}]


def test_sharded_snapshot_removes_only_old_dead_writer_temps(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))

    dead = shard_dir / ".dead.json.999999999.tmp"
    dead.write_text("stale\n", encoding="utf-8")
    dead_alt = shard_dir / ".dead-alt.json.tmp.999999997"
    dead_alt.write_text("stale\n", encoding="utf-8")
    dead_manifest = manifest.with_name(f".{manifest.name}.999999998.tmp")
    dead_manifest.write_text("stale\n", encoding="utf-8")
    dead_manifest_alt = manifest.with_name(f".{manifest.name}.tmp.999999996")
    dead_manifest_alt.write_text("stale\n", encoding="utf-8")
    old = time.time() - 120.0
    for path in (dead, dead_alt, dead_manifest, dead_manifest_alt):
        os.utime(path, (old, old))
    current = shard_dir / f".current.json.{os.getpid()}.tmp"
    current.write_text("active\n", encoding="utf-8")
    os.utime(current, (old, old))
    current_manifest = manifest.with_name(f".{manifest.name}.{os.getpid()}.tmp")
    current_manifest.write_text("active\n", encoding="utf-8")
    os.utime(current_manifest, (old, old))

    LatestQuoteSnapshot(manifest, min_write_interval_sec=0)

    assert not dead.exists()
    assert not dead_alt.exists()
    assert not dead_manifest.exists()
    assert not dead_manifest_alt.exists()
    assert current.exists()
    assert current_manifest.exists()


def test_connecting_shard_activates_only_after_every_initial_book(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(tmp_path / "shards"))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    state.connecting(0, ["a", "b"], expected_shards=1)

    initial = json.loads(manifest.read_text())
    assert initial["state"] == "disconnected"
    assert initial["active_shards"] == []
    assert initial["subscribed_token_count"] == 0
    assert state.shard_materialization_counts(0) == (0, 2)

    for token_id in ("a", "b"):
        observed_at = f"2026-07-18T03:00:0{1 if token_id == 'a' else 2}.000Z"
        state.observe_market_message(
            {
                "event_type": "book", "asset_id": token_id,
                "bids": [{"price": "0.90", "size": "4"}],
                "asks": [{"price": "0.91", "size": "5"}],
            },
            observed_at=observed_at,
            depth_levels=5,
        )
        state.observe(
            [{
                "token_id": token_id, "slug": "slug", "event_date": "2026-07-18",
                "outcome": "35°C", "side": "NO", "best_bid": 0.90, "best_ask": 0.91,
                "spread": 0.01, "bid_depth_topN": 4, "ask_depth_topN": 5,
                "source": "clob_ws", "source_detail": "book", "created_at": observed_at,
            }],
            observed_at=observed_at,
        )
        activated = state.activate_shard_if_materialized(0)
        if token_id == "a":
            assert activated is False
            assert state.active_shards == set()
            assert state.shard_materialization_counts(0) == (1, 2)

    assert activated is True
    assert state.active_shards == {0}
    assert state.shard_materialization_counts(0) == (2, 2)
    ready = json.loads(manifest.read_text())
    assert ready["state"] == "connected"
    assert ready["active_shards"] == [0]
    assert ready["subscribed_token_count"] == 2
    assert ready["materialized_token_count"] == 2
    assert ready["provisional_token_count"] == 0
    for token_id in ("a", "b"):
        token_path = tmp_path / "shards" / f"{hashlib.sha256(token_id.encode()).hexdigest()}.json"
        assert json.loads(token_path.read_text())["shard_active"] is True


def test_catalog_reassignment_keeps_old_token_until_new_generation_commits(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    state.connected(0, ["old-token"])
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "old-token",
            "bids": [{"price": "0.40", "size": "2"}],
            "asks": [{"price": "0.41", "size": "3"}],
        },
        observed_at="2026-07-18T12:00:00.000Z",
        depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "old-token", "slug": "slug", "event_date": "2026-07-18",
            "outcome": "35°C", "side": "NO", "best_bid": 0.40, "best_ask": 0.41,
            "source": "clob_ws", "source_detail": "book", "created_at": "2026-07-18T12:00:00.000Z",
        }],
        observed_at="2026-07-18T12:00:00.000Z",
    )
    old_path = shard_dir / f"{hashlib.sha256(b'old-token').hexdigest()}.json"
    assert old_path.exists()

    state.connecting(0, ["new-token"], expected_shards=1)
    # The old generation remains readable while the replacement is only
    # provisional.  This is the window that previously produced false quote
    # missing reports.
    assert old_path.exists()
    old_payload = json.loads(old_path.read_text())
    assert old_payload["shard_active"] is True
    assert json.loads(manifest.read_text())["active_shards"] == [0]

    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "new-token",
            "bids": [{"price": "0.50", "size": "2"}],
            "asks": [{"price": "0.51", "size": "3"}],
        },
        observed_at="2026-07-18T12:00:01.000Z",
        depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "new-token", "slug": "slug", "event_date": "2026-07-18",
            "outcome": "36°C", "side": "NO", "best_bid": 0.50, "best_ask": 0.51,
            "source": "clob_ws", "source_detail": "book", "created_at": "2026-07-18T12:00:01.000Z",
        }],
        observed_at="2026-07-18T12:00:01.000Z",
    )
    assert state.activate_shard_if_materialized(0) is True
    assert not old_path.exists()
    ready = json.loads(manifest.read_text())
    assert ready["materialized_token_count"] == 1
    assert ready["unmaterialized_tokens"] == []


def test_failed_reconnect_keeps_last_committed_generation(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    state.connected(0, ["stable"])
    state.observe_market_message(
        {
            "event_type": "book", "asset_id": "stable",
            "bids": [{"price": "0.60", "size": "2"}],
            "asks": [{"price": "0.61", "size": "3"}],
        },
        observed_at="2026-07-18T12:00:00.000Z",
        depth_levels=5,
    )
    state.observe(
        [{
            "token_id": "stable", "slug": "slug", "event_date": "2026-07-18",
            "outcome": "37°C", "side": "NO", "best_bid": 0.60, "best_ask": 0.61,
            "source": "clob_ws", "source_detail": "book", "created_at": "2026-07-18T12:00:00.000Z",
        }],
        observed_at="2026-07-18T12:00:00.000Z",
    )
    old_generation = json.loads(manifest.read_text())["active_shard_generations"]["0"]
    state.connecting(0, ["stable", "new-token"], expected_shards=1)
    state.disconnected(0)
    payload = json.loads(manifest.read_text())
    assert payload["active_shards"] == [0]
    assert payload["active_shard_generations"]["0"] == old_generation
    assert payload["materialized_token_count"] == 1
    assert payload["pending_shards"] == [0]
    assert payload["pending_subscribed_token_count"] == 2
    assert payload["pending_materialized_token_count"] == 0
    assert set(payload["unmaterialized_tokens"]) == {"stable", "new-token"}
    assert (shard_dir / f"{hashlib.sha256(b'stable').hexdigest()}.json").exists()
    assert not (shard_dir / f"{hashlib.sha256(b'new-token').hexdigest()}.json").exists()


def test_restart_restores_committed_generation_before_staging_reconnect(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    first = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    first.connected(0, ["stable"])
    first.observe_market_message(
        {
            "event_type": "book", "asset_id": "stable",
            "bids": [{"price": "0.60", "size": "2"}],
            "asks": [{"price": "0.61", "size": "3"}],
        }, observed_at="2026-07-18T12:00:00.000Z", depth_levels=5,
    )
    first.observe(
        [{
            "token_id": "stable", "slug": "slug", "event_date": "2026-07-18",
            "outcome": "37°C", "side": "NO", "best_bid": 0.60, "best_ask": 0.61,
            "source": "clob_ws", "source_detail": "book", "created_at": "2026-07-18T12:00:00.000Z",
        }], observed_at="2026-07-18T12:00:00.000Z",
    )
    old_path = shard_dir / f"{hashlib.sha256(b'stable').hexdigest()}.json"
    assert old_path.exists()

    # A daemon restart must not erase or demote the committed evidence.
    second = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    assert second.active_shards == {0}
    assert second.shard_tokens[0] == {"stable"}
    assert json.loads(manifest.read_text())["materialized_token_count"] == 1
    second.connecting(0, ["stable", "replacement"], expected_shards=1)
    after_connect = json.loads(manifest.read_text())
    assert after_connect["active_shards"] == [0]
    assert after_connect["materialized_token_count"] == 1
    assert old_path.exists()


def test_restart_shrink_retires_obsolete_shards_only_after_full_materialization(
    tmp_path, monkeypatch
) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))

    def materialize(state: LatestQuoteSnapshot, token_id: str, price: float, second: int) -> None:
        observed_at = f"2026-07-18T12:01:{second:02d}.000Z"
        state.observe_market_message(
            {
                "event_type": "book", "asset_id": token_id,
                "bids": [{"price": str(price), "size": "2"}],
                "asks": [{"price": str(price + 0.01), "size": "3"}],
            },
            observed_at=observed_at,
            depth_levels=5,
        )
        state.observe(
            [{
                "token_id": token_id, "slug": "slug", "event_date": "2026-07-18",
                "outcome": "37°C", "side": "NO", "best_bid": price,
                "best_ask": price + 0.01, "spread": 0.01,
                "bid_depth_topN": 2, "ask_depth_topN": 3,
                "source": "clob_ws", "source_detail": "book", "created_at": observed_at,
            }],
            observed_at=observed_at,
        )

    def token_path(token_id: str):
        return shard_dir / f"{hashlib.sha256(token_id.encode()).hexdigest()}.json"

    def assert_valid_token_file(token_id: str, shard_index: int, generation: int) -> None:
        path = token_path(token_id)
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        supplied = payload.pop("snapshot_sha256")
        assert supplied == hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert payload["shard_index"] == shard_index
        assert payload["shard_generation"] == generation
        assert payload["shard_active"] is True

    # Commit ten shards, then simulate a daemon restart restoring all ten.
    first = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    for index in range(10):
        token_id = f"token-{index}"
        first.connected(index, [token_id], expected_shards=10)
        materialize(first, token_id, 0.40 + index * 0.01, index)
    committed = json.loads(manifest.read_text(encoding="utf-8"))
    assert committed["active_shards"] == list(range(10))
    assert committed["expected_shards"] == 10
    obsolete_paths = {index: token_path(f"token-{index}") for index in range(5, 10)}
    current_paths = {index: token_path(f"token-{index}") for index in range(5)}
    assert all(path.exists() for path in (*obsolete_paths.values(), *current_paths.values()))

    replacement = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)
    assert replacement.active_shards == set(range(10))
    assert replacement.expected_shards == 10

    # Stage the five-shard replacement.  Restored shards 5-9 stay committed
    # and readable while any of 0-4 is still provisional.
    for index in range(5):
        replacement.connecting(index, [f"token-{index}"], expected_shards=5)
    staged = json.loads(manifest.read_text(encoding="utf-8"))
    assert staged["active_shards"] == list(range(10))
    assert staged["expected_shards"] == 10
    assert staged["pending_shards"] == list(range(5))
    assert all(path.exists() for path in obsolete_paths.values())

    for index in range(5):
        token_id = f"token-{index}"
        materialize(replacement, token_id, 0.50 + index * 0.01, 20 + index)
        assert replacement.activate_shard_if_materialized(index) is True
        retired = replacement.reconcile_runtime_topology(5)
        if index < 4:
            assert retired is False
            interim = json.loads(manifest.read_text(encoding="utf-8"))
            assert interim["active_shards"] == list(range(10))
            assert interim["expected_shards"] == 10
            assert all(path.exists() for path in obsolete_paths.values())
        else:
            assert retired is True

    shrunk = json.loads(manifest.read_text(encoding="utf-8"))
    assert shrunk["active_shards"] == list(range(5))
    assert shrunk["expected_shards"] == 5
    assert all(index <= 4 for index in shrunk["active_shards"])
    assert all(not path.exists() for path in obsolete_paths.values())
    for index, path in current_paths.items():
        assert_valid_token_file(
            f"token-{index}",
            index,
            int(shrunk["active_shard_generations"][str(index)]),
        )

    # Regrow after the shrink and require the complete ten-shard topology
    # before making 5-9 active again; this catches oscillation/reuse bugs.
    for index in range(10):
        replacement.connecting(index, [f"token-{index}"], expected_shards=10)
    assert json.loads(manifest.read_text(encoding="utf-8"))["active_shards"] == list(range(5))
    for index in range(10):
        materialize(replacement, f"token-{index}", 0.60 + index * 0.01, 40 + index)
        assert replacement.activate_shard_if_materialized(index) is True
        if index < 9:
            assert replacement.reconcile_runtime_topology(10) is False
    assert replacement.reconcile_runtime_topology(10) is True
    regrown = json.loads(manifest.read_text(encoding="utf-8"))
    assert regrown["active_shards"] == list(range(10))
    assert regrown["expected_shards"] == 10
    for index in range(10):
        assert_valid_token_file(
            f"token-{index}",
            index,
            int(regrown["active_shard_generations"][str(index)]),
        )


def test_repeated_reconnects_never_publish_partial_active_manifest(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "latest.json"
    shard_dir = tmp_path / "shards"
    monkeypatch.setenv("PM_MARKET_WS_SHARDED_SNAPSHOT", "1")
    monkeypatch.setenv("PM_MARKET_WS_QUOTE_SHARD_DIR", str(shard_dir))
    state = LatestQuoteSnapshot(manifest, min_write_interval_sec=0)

    def materialize(token_id: str, price: float, observed_at: str) -> None:
        state.observe_market_message(
            {
                "event_type": "book", "asset_id": token_id,
                "bids": [{"price": str(price), "size": "2"}],
                "asks": [{"price": str(price + 0.01), "size": "3"}],
            }, observed_at=observed_at, depth_levels=5,
        )
        state.observe(
            [{
                "token_id": token_id, "slug": "slug", "event_date": "2026-07-18",
                "outcome": "38°C", "side": "NO", "best_bid": price, "best_ask": price + 0.01,
                "source": "clob_ws", "source_detail": "book", "created_at": observed_at,
            }], observed_at=observed_at,
        )

    state.connecting(0, ["a", "b"], expected_shards=1)
    materialize("a", 0.40, "2026-07-18T12:00:00.000Z")
    assert state.activate_shard_if_materialized(0) is False
    materialize("b", 0.41, "2026-07-18T12:00:01.000Z")
    assert state.activate_shard_if_materialized(0) is True

    for cycle, tokens in enumerate((("b", "c"), ("c", "d"), ("d", "e")), start=2):
        state.connecting(0, tokens, expected_shards=1)
        before = json.loads(manifest.read_text())
        assert before["active_shards"] == [0]
        assert before["materialized_token_count"] == 2
        materialize(tokens[0], 0.40 + cycle * 0.01, f"2026-07-18T12:00:{cycle:02d}.000Z")
        assert state.activate_shard_if_materialized(0) is False
        during = json.loads(manifest.read_text())
        assert during["active_shards"] == [0]
        assert during["materialized_token_count"] == 2
        materialize(tokens[1], 0.41 + cycle * 0.01, f"2026-07-18T12:00:{cycle + 1:02d}.000Z")
        assert state.activate_shard_if_materialized(0) is True
        after = json.loads(manifest.read_text())
        assert after["active_shards"] == [0]
        assert after["materialized_token_count"] == 2
        assert after["unmaterialized_tokens"] == []


def test_proxy_none_is_strict_direct_during_network_switch(monkeypatch) -> None:
    for key in ("PM_MARKET_WS_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    assert listener._ws_proxy_candidates("none") == [("direct", None)]
    assert listener._ws_proxy_candidates("direct") == [("direct", None)]


def test_proxy_fallback_requires_explicit_direct_then_env(monkeypatch) -> None:
    for key in ("PM_MARKET_WS_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    assert listener._ws_proxy_candidates("direct_then_env") == [
        ("direct", None),
        ("http_proxy_env", "http://127.0.0.1:7890"),
    ]
    assert listener._ws_proxy_candidates("env") == [
        ("http_proxy_env", "http://127.0.0.1:7890"),
    ]


def test_clob_bound_held_yes_tokens_are_excluded_from_primary_registry_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "registry.db"
    today = listener.datetime.now(listener.timezone.utc).date().isoformat()
    with sqlite3.connect(database) as conn:
        conn.execute(
            """CREATE TABLE reported_trajectory_market_registry(
                 slug TEXT,event_date TEXT,outcome TEXT,station_id TEXT,
                 yes_token TEXT,no_token TEXT,
                 settlement_timezone TEXT,source_lane TEXT,source_eligible INTEGER,
                 market_active INTEGER,market_accepting_orders INTEGER,refreshed_at TEXT
               )"""
        )
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry VALUES(
                 'current',?,'30°C','TEST','yes-current','no-current','UTC','awc',1,1,1,
                 '2026-08-01T00:00:00Z'
               )""",
            (today,),
        )
        conn.execute(
            """INSERT INTO reported_trajectory_market_registry VALUES(
                 'held','2000-01-01','31°C','TEST','456','no-held','UTC','awc',1,1,1,
                 '2026-08-01T00:00:00Z'
               )"""
        )
    inventory = tmp_path / "held_yes.json"
    inventory.write_text(
        json.dumps(
            {
                "schema": "reported_trajectory_held_yes_inventory_v1",
                "complete": True,
                "token_ids": ["123", "456"],
                "active_token_ids": ["456"],
                "active_token_ids_source": "official_local_clob_execution_guard_snapshot",
                "active_token_ids_observed_at": "2026-08-01T00:00:00.000Z",
                "active_token_ids_receipt_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PM_MARKET_WS_DB_PATH", str(database))
    monkeypatch.setattr(listener, "HELD_YES_INVENTORY_PATH", inventory)
    monkeypatch.setattr(listener, "_target_yes_quotes_enabled", lambda: True)
    listener._BINDINGS_CACHE.clear()
    listener._BINDING_MARKER_CACHE.clear()

    bindings = listener._load_token_bindings(days_ahead=0, max_assets=10)

    assert set(bindings) == {"yes-current"}
    assert {binding.side for binding in bindings.values()} == {"YES"}
    assert listener._held_yes_active_token_scope()[0] == ("456",)

    monkeypatch.setattr(listener, "_target_yes_quotes_enabled", lambda: False)
    listener._BINDINGS_CACHE.clear()
    listener._BINDING_MARKER_CACHE.clear()
    bindings = listener._load_token_bindings(days_ahead=0, max_assets=10)

    # The production market socket is pure-YES.  An invalid/disabled target
    # activation fails closed to an empty catalog; it never falls back to a
    # retired hard-NO token subscription.
    assert bindings == {}
    assert listener._held_yes_active_token_scope()[0] == ("456",)


def test_invalid_or_over_cap_held_yes_scope_never_enters_primary_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = tmp_path / "held_yes.json"
    inventory.write_text(
        json.dumps(
            {
                "schema": "reported_trajectory_held_yes_inventory_v1",
                "complete": True,
                "token_ids": ["123", "456"],
                "active_token_ids": ["123", "456"],
                "active_token_ids_source": "official_local_clob_execution_guard_snapshot",
                "active_token_ids_observed_at": "2026-08-01T00:00:00.000Z",
                "active_token_ids_receipt_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(listener, "HELD_YES_INVENTORY_PATH", inventory)
    listener._BINDINGS_CACHE.clear()
    listener._BINDING_MARKER_CACHE.clear()

    assert listener._held_yes_active_token_scope() == ((), "registry_unresolved")

    class Cursor:
        def fetchone(self):
            return (0, "", "", "", "")

        def fetchall(self):
            return []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            return Cursor()

    monkeypatch.setattr(listener, "_open_registry_ro", lambda: Connection())
    assert listener._load_token_bindings(days_ahead=0, max_assets=1) == {}

    inventory.write_text("{not-json", encoding="utf-8")
    assert listener._held_yes_active_token_scope() == ((), "invalid")


def test_held_yes_auxiliary_requires_full_book_and_never_writes_empty_evidence(tmp_path: Path) -> None:
    manifest = tmp_path / "held.json"
    shard_dir = tmp_path / "held-shards"
    state = listener.HeldYesQuoteSnapshot(manifest, shard_dir=shard_dir, depth_levels=5)
    token = "456"
    observed_at = "2026-08-01T12:00:00.000Z"

    state.begin_connection([token], observed_at=observed_at)
    token_path = shard_dir / f"{hashlib.sha256(token.encode()).hexdigest()}.json"
    initial = json.loads(manifest.read_text(encoding="utf-8"))
    assert initial["state"] == "missing_evidence"
    assert initial["missing_complete_book_token_ids"] == [token]
    assert not token_path.exists()

    # A delta cannot establish depth before a complete official book.
    assert state.observe_market_message(
        {
            "event_type": "price_change",
            "price_changes": [{"asset_id": token, "side": "BUY", "price": "0.4", "size": "3"}],
        },
        observed_at="2026-08-01T12:00:01.000Z",
    ) is False
    assert not token_path.exists()

    # Empty arrays are a real complete book, distinct from absent evidence.
    assert state.observe_market_message(
        {"event_type": "book", "asset_id": token, "bids": [], "asks": []},
        observed_at="2026-08-01T12:00:02.000Z",
    ) is True
    ready = json.loads(manifest.read_text(encoding="utf-8"))
    shard = json.loads(token_path.read_text(encoding="utf-8"))
    assert ready["state"] == "ready"
    assert ready["complete_book_token_ids"] == [token]
    assert shard["schema"] == "clob_ws_held_yes_token_quote_snapshot_v1"
    assert shard["quote"]["bids"] == []
    assert shard["quote"]["asks"] == []
    assert shard["quote"]["book_observed_at"] == "2026-08-01T12:00:02.000Z"


def test_held_yes_auxiliary_scope_switch_invalidates_old_shard_before_new_book(tmp_path: Path) -> None:
    manifest = tmp_path / "held.json"
    shard_dir = tmp_path / "held-shards"
    state = listener.HeldYesQuoteSnapshot(manifest, shard_dir=shard_dir)
    old_token, new_token = "123", "456"
    old_path = shard_dir / f"{hashlib.sha256(old_token.encode()).hexdigest()}.json"

    state.begin_connection([old_token], observed_at="2026-08-01T12:00:00.000Z")
    state.observe_market_message(
        {"event_type": "book", "asset_id": old_token, "bids": [], "asks": []},
        observed_at="2026-08-01T12:00:01.000Z",
    )
    assert old_path.exists()

    state.begin_connection([new_token], observed_at="2026-08-01T12:01:00.000Z")
    switched = json.loads(manifest.read_text(encoding="utf-8"))
    assert switched["scope_token_ids"] == [new_token]
    assert switched["state"] == "missing_evidence"
    assert not old_path.exists()


def test_initial_subscription_book_array_is_typed_as_book() -> None:
    raw = json.dumps([
        {
            "market": "condition-1",
            "asset_id": "token-yes",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.60", "size": "8"}],
        },
        {
            "market": "condition-2",
            "asset_id": "token-empty",
            "bids": [],
            "asks": [],
        },
    ])

    messages = list(_iter_payload_messages(raw))

    assert [message["event_type"] for message in messages] == ["book", "book"]
    assert messages[0]["asset_id"] == "token-yes"


def test_single_shard_reconnect_preserves_sibling_and_recovers(monkeypatch, tmp_path) -> None:
    calls = {0: 0, 1: 0}

    async def fake_batch(_args, _assets, _bindings, **kwargs):
        index = int(kwargs["shard_index"])
        calls[index] += 1
        if index == 0 and calls[index] == 1:
            raise ConnectionError("network switched")
        return index + 1, (index + 1) * 10, 0

    monkeypatch.setattr(listener, "_listen_asset_batch", fake_batch)
    monkeypatch.setattr(listener, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setenv("PM_MARKET_WS_SHARD_RETRY_BASE_SEC", "0.01")
    args = argparse.Namespace(reload_sec=1.0)

    async def run_both():
        return await asyncio.gather(
            listener._listen_asset_batch_resilient(
                args, ["a"], {}, total_assets=2, shard_index=0, shard_count=2,
            ),
            listener._listen_asset_batch_resilient(
                args, ["b"], {}, total_assets=2, shard_index=1, shard_count=2,
            ),
        )

    results = asyncio.run(run_both())
    assert results == [(1, 10, 0), (2, 20, 0)]
    assert calls == {0: 2, 1: 1}


def test_initial_materialization_catalog_change_reloads_before_retry(
    monkeypatch, tmp_path
) -> None:
    calls = []

    async def fake_batch(_args, asset_ids, _bindings, **kwargs):
        calls.append(list(asset_ids))
        if len(calls) == 1:
            raise listener.SubscriptionCatalogChanged("catalog_changed_during_materialization")
        return 1, 10, 0

    monkeypatch.setattr(listener, "_listen_asset_batch", fake_batch)
    monkeypatch.setattr(
        listener,
        "_load_current_shard_catalog",
        lambda *_args, **_kwargs: (["new-token"], {}),
    )
    monkeypatch.setenv("PM_MARKET_WS_SHARD_RETRY_BASE_SEC", "0.01")
    args = argparse.Namespace(reload_sec=1.0)

    result = asyncio.run(
        listener._listen_asset_batch_resilient(
            args,
            ["stale-token"],
            {},
            total_assets=1,
            shard_index=0,
            shard_count=1,
        )
    )

    assert result == (1, 10, 0)
    assert calls == [["stale-token"], ["new-token"]]


def test_shard_reconnect_preserves_first_transport_error(monkeypatch, tmp_path) -> None:
    errors = iter([ConnectionError("first transport failure"), TimeoutError("last timeout")])

    async def fake_batch(*_args, **_kwargs):
        try:
            raise next(errors)
        except StopIteration:
            raise TimeoutError("last timeout")

    monkeypatch.setattr(listener, "_listen_asset_batch", fake_batch)
    monkeypatch.setattr(listener, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setenv("PM_MARKET_WS_SHARD_RETRY_BASE_SEC", "0.01")
    monkeypatch.setenv("PM_MARKET_WS_SHARD_RETRY_BUDGET_SEC", "0.06")
    args = argparse.Namespace(reload_sec=0.06)
    with pytest.raises(listener.ShardReconnectError) as caught:
        asyncio.run(
            listener._listen_asset_batch_resilient(
                args, ["a"], {}, total_assets=2, shard_index=0, shard_count=2,
            )
        )
    message = str(caught.value)
    assert "first_error=ConnectionError: first transport failure" in message
    assert "last_error=TimeoutError: last timeout" in message


def test_cancelled_error_never_enters_success_tuple_path() -> None:
    value = asyncio.CancelledError()
    assert isinstance(value, BaseException)
    assert not isinstance(value, Exception)
    written, received, skipped, errors = listener._aggregate_shard_results(
        [(2, 20, 3), value, ConnectionError("primary transport failure")]
    )
    assert (written, received, skipped) == (2, 20, 3)
    assert errors == [
        "shard=1 CancelledError: ",
        "shard=2 ConnectionError: primary transport failure",
    ]


def test_planned_refresh_is_deferred_outside_release_window(monkeypatch) -> None:
    monkeypatch.setenv("PM_MARKET_WS_RELEASE_GUARD_BEFORE_SEC", "30")
    monkeypatch.setenv("PM_MARKET_WS_RELEASE_GUARD_AFTER_SEC", "180")
    # 15:59:50 UTC is ten seconds before a boundary; refresh resumes at 16:03.
    assert listener._release_guard_delay_sec(16 * 3600 - 10) == pytest.approx(190.0)
    # 16:01:30 remains protected for another ninety seconds.
    assert listener._release_guard_delay_sec(16 * 3600 + 90) == pytest.approx(90.0)
    assert listener._release_guard_delay_sec(16 * 3600 + 5 * 60) == 0.0


def test_unchanged_catalog_does_not_require_socket_refresh(monkeypatch) -> None:
    args = argparse.Namespace(days_ahead=2, max_assets=4000, shard_size=2)
    bindings = {
        token: TokenBinding(token, "slug", "2026-07-18", "30°C", "NO")
        for token in ("a", "b", "c")
    }
    monkeypatch.setattr(listener, "_load_token_bindings", lambda *_args: bindings)
    assert listener._subscription_catalog_unchanged(
        args, ["a", "b"], shard_index=0, shard_count=2,
    )
    assert listener._subscription_catalog_unchanged(
        args, ["c"], shard_index=1, shard_count=2,
    )


def test_catalog_change_rolls_only_the_affected_shard(monkeypatch, tmp_path) -> None:
    calls = []

    async def fake_batch(_args, assets, _bindings, **kwargs):
        calls.append((int(kwargs["shard_index"]), tuple(assets)))
        if len(calls) == 1:
            raise listener.SubscriptionCatalogChanged("changed")
        return 1, 2, 3

    refreshed = {
        "a": TokenBinding("a", "slug", "2026-07-18", "30°C", "NO"),
        "b": TokenBinding("b", "slug", "2026-07-18", "31°C", "NO"),
    }
    monkeypatch.setattr(listener, "_listen_asset_batch", fake_batch)
    monkeypatch.setattr(listener, "_load_token_bindings", lambda *_args: refreshed)
    monkeypatch.setattr(listener, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    args = argparse.Namespace(days_ahead=2, max_assets=4000, shard_size=2, reload_sec=300)
    result = asyncio.run(
        listener._listen_asset_batch_resilient(
            args,
            ["old"],
            {"old": TokenBinding("old", "slug", "2026-07-18", "29°C", "NO")},
            total_assets=2,
            shard_index=0,
            shard_count=1,
        )
    )
    assert result == (1, 2, 3)
    assert calls == [(0, ("old",)), (0, ("a", "b"))]
