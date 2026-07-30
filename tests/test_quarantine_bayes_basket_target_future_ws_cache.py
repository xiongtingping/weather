from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

from bayes_basket_target.market_ingress import materialize_official_ws_evidence_cache
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.quarantine_bayes_basket_target_future_ws_cache import run


def _manifest(tmp_path: Path) -> Path:
    quote = {
        "token_id": "yes-38",
        "slug": "highest-temperature-in-test-on-july-23-2026",
        "event_date": "2026-07-23",
        "outcome": "38°C",
        "side": "YES",
        "bids": [{"price": 0.4, "size": 10.0}],
        "asks": [{"price": 0.42, "size": 10.0}],
        "book_observed_at": "2026-07-23T05:00:00Z",
        "created_at": "2026-07-23T05:00:00Z",
    }
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    core = {
        "schema": "clob_ws_token_quote_snapshot_v1",
        "token_id": "yes-38",
        "quote": quote,
        "shard_index": 0,
        "shard_generation": 1,
        "shard_active": True,
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    shard = {
        **core,
        "snapshot_sha256": hashlib.sha256(
            json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (shard_dir / "yes.json").write_text(json.dumps(shard), encoding="utf-8")
    root = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "active_shards": [0],
        "expected_shards": 1,
        "active_shard_generations": {"0": 1},
        "quote_storage": "sha256_token_shards_v1",
        "quote_shard_dir": str(shard_dir),
        "quotes_by_token": {},
        "materialized_token_count": 1,
        "unmaterialized_tokens": [],
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    root["snapshot_sha256"] = hashlib.sha256(
        json.dumps(root, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


def test_ws_cache_does_not_materialize_future_et_scope(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        conn.execute(
            """CREATE TABLE reported_trajectory_market_registry(
                slug TEXT,event_date TEXT,station_id TEXT,settlement_timezone TEXT,
                outcome TEXT,yes_token TEXT,registry_json TEXT,source_eligible INTEGER,
                market_active INTEGER,market_accepting_orders INTEGER)"""
        )
        conn.execute(
            "INSERT INTO reported_trajectory_market_registry VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "highest-temperature-in-test-on-july-23-2026", "2026-07-25", "TEST", "UTC", "38°C", "yes-38",
                json.dumps({"market_id": "m-38", "lower": 38.0, "upper": 38.0}), 1, 1, 1,
            ),
        )
        # The manifest contains a complete quote, but the ET day has not
        # started at the explicit clock.  The cache must reject it before any
        # row is written.
        try:
            materialize_official_ws_evidence_cache(
                conn,
                manifest_path=manifest,
                event_date="2026-07-25",
                as_of_utc="2026-07-24T21:47:49Z",
                commit=True,
            )
        except Exception as exc:
            assert "complete_scope_missing" in str(exc)
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_evidence_cache").fetchone()[0] == 0


def test_cleanup_purges_only_future_cache_rows(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE basket_official_ws_evidence_cache(
            scope_key TEXT PRIMARY KEY, evidence_id TEXT, market_id TEXT, station_id TEXT,
            event_date TEXT, observed_at TEXT, quote_version TEXT, manifest_sha256 TEXT,
            buckets_json TEXT, quote_source TEXT, evidence_only INTEGER, authorizes_order INTEGER,
            content_sha256 TEXT, updated_at TEXT)""")
        conn.execute(
            "INSERT INTO basket_official_ws_evidence_cache VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("TEST|2026-07-25", "e", "m", "TEST", "2026-07-25", "2026-07-24T21:00:00Z", "v", "a" * 64, "[]", "OFFICIAL_CLOB_WS", 1, 0, "b" * 64, "2026-07-24T21:00:00Z"),
        )
        conn.commit()
    out = tmp_path / "audit.json"
    result = run(db=db, as_of_utc="2026-07-24T21:47:49Z", output=out, purge=True)
    assert result["checks"]["purged"] is True
    assert result["remaining_future_count"] == 0
    audit = json.loads(out.read_text())
    assert audit["checks"]["protected_production_line_touched"] is False
