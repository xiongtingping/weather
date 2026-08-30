from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from bayes_basket_target.database import SHARED_LIVE_DB
from scripts.initialize_bayes_basket_target_ssot import initialize
from scripts import materialize_bayes_basket_target_market as cli


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
# 2026-07-23 ET starts at 04:00Z; keep the fixture inside the started ET day.
NOW = "2026-07-23T05:00:00Z"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _write_manifest(tmp_path: Path) -> Path:
    quote = {
        "token_id": "yes-38",
        "side": "YES",
        "best_bid": 0.40,
        "best_ask": 0.42,
        "bids": [{"price": 0.40, "size": 10.0}],
        "asks": [{"price": 0.42, "size": 10.0}],
        "book_observed_at": NOW,
        "created_at": NOW,
    }
    root = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "state": "connected",
        "active_shards": [0],
        "expected_shards": 1,
        "active_shard_generations": {"0": 1},
        "quote_storage": "inline_v1",
        "quotes_by_token": {"yes-38": quote},
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "unmaterialized_tokens": [],
    }
    root["snapshot_sha256"] = hashlib.sha256(_canonical(root)).hexdigest()
    path = tmp_path / "market_ws_latest_quotes.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


def _write_cash_phase(tmp_path: Path) -> Path:
    payload = {
        "schema": "bayes_basket_cash_phase_ingress_v1",
        "markets": [{
            "station_id": "TEST",
            "event_date": "2026-07-23",
            "cash_phase_input": {
                "policy_version": "cash-test-v1",
                "station_id": "TEST",
                "current_phase": "HEAVY",
                "event_date_local": "2026-07-23",
                "observed_at_utc": NOW,
                "station_timezone": "UTC",
                "checkpoint_id": "cash-checkpoint-1",
            },
        }],
    }
    path = tmp_path / "cash_phase.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _prepare_target_db(tmp_path: Path) -> Path:
    db = tmp_path / "target.db"
    initialize(db=db, activation_path=ACTIVATION)
    snapshot_sha = "b" * 64
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO basket_target_market_registry_snapshot "
            "(snapshot_sha256,source_schema,source_scope,exported_at_utc,source_database_path,"
            "row_count,ingested_at_utc,content_sha256) VALUES(?,?,?,?,?,?,?,?)",
            (
                snapshot_sha,
                "reported_trajectory_market_registry_v1",
                "shared_live_registry_export_only",
                NOW,
                "/isolated/source.db",
                1,
                NOW,
                snapshot_sha,
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_market_registry "
            "(snapshot_sha256,slug,event_date,outcome,city_slug,station_id,settlement_timezone,"
            "settlement_unit,source_lane,source_eligible,source_block_reason,resolution_url,"
            "rules_sha256,yes_token,no_token,market_active,market_accepting_orders,registry_json,refreshed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_sha,
                "highest-temperature-in-test-on-july-23-2026",
                "2026-07-23",
                "38°C",
                "test",
                "TEST",
                "UTC",
                "C",
                "amsc",
                1,
                "",
                "",
                "a" * 64,
                "yes-38",
                "no-38",
                1,
                1,
                json.dumps({"market_id": "market-test", "lower": 38.0, "upper": 38.0,
                            "order_min_size": 5, "order_price_min_tick_size": 0.01}),
                NOW,
            ),
        )
        conn.commit()
    return db


def _run_cli(
    *,
    db: Path,
    manifest: Path,
    cash_phase: Path,
    activation: Path = ACTIVATION,
    commit: bool = False,
    extra: list[str] | None = None,
) -> int:
    argv = [
        "materialize_bayes_basket_target_market.py",
        "--db", str(db),
        "--manifest", str(manifest),
        "--cash-phase-json", str(cash_phase),
        "--activation", str(activation),
        "--no-live-q0",
        "--observed-at-utc", NOW,
        "--remaining-budget-usdc", "20",
    ]
    if commit:
        argv.append("--commit")
    argv.extend(extra or [])
    old_argv = sys.argv
    try:
        sys.argv = argv
        return cli.main()
    finally:
        sys.argv = old_argv


def test_no_live_q0_cli_is_atomic_idempotent_and_orderless(tmp_path: Path) -> None:
    db = _prepare_target_db(tmp_path)
    manifest = _write_manifest(tmp_path)
    cash_phase = _write_cash_phase(tmp_path)

    assert _run_cli(db=db, manifest=manifest, cash_phase=cash_phase) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_confirmed_inventory_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_order_state_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0

    assert _run_cli(db=db, manifest=manifest, cash_phase=cash_phase, commit=True) == 0
    with sqlite3.connect(db) as conn:
        inventory = json.loads(conn.execute(
            "SELECT inventory_json FROM basket_confirmed_inventory_snapshot"
        ).fetchone()[0])
        order_state = json.loads(conn.execute(
            "SELECT order_state_json FROM basket_order_state_snapshot"
        ).fetchone()[0])
        assert inventory["filled_by_bucket"] == {"38°C": 0.0}
        assert inventory["production_no_live"] is True
        assert inventory["exchange_position_read"] is False
        assert order_state["pending_by_bucket"] == {"38°C": 0.0}
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0

    assert _run_cli(db=db, manifest=manifest, cash_phase=cash_phase, commit=True) == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_confirmed_inventory_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM basket_order_state_snapshot").fetchone()[0] == 1


def test_no_live_q0_cli_rejects_explicit_live_position_inputs(tmp_path: Path) -> None:
    db = _prepare_target_db(tmp_path)
    manifest = _write_manifest(tmp_path)
    cash_phase = _write_cash_phase(tmp_path)
    positions = tmp_path / "positions.json"
    positions.write_text("{}", encoding="utf-8")

    assert _run_cli(
        db=db,
        manifest=manifest,
        cash_phase=cash_phase,
        extra=["--official-positions-manifest", str(positions)],
    ) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM basket_official_ws_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_confirmed_inventory_snapshot").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM basket_order_state_snapshot").fetchone()[0] == 0


def test_no_live_q0_cli_does_not_open_shared_live_database(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    cash_phase = _write_cash_phase(tmp_path)
    assert _run_cli(db=SHARED_LIVE_DB, manifest=manifest, cash_phase=cash_phase) == 1
