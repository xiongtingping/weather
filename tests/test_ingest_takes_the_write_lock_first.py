"""A batch that looks rows up before writing them takes the write lock first.

2026-09-26: three production cycles of two hundred ended ``blocked_fail_closed
· database is locked`` before their decision loop (07:30, 10:00, 12:40Z).  The
blocked rows of two of them name the failed statement: the evidence cache's
``UPDATE basket_official_ws_evidence_cache``.  The batch ran in a DEFERRED
savepoint, read row after row (most quotes already current), and its first
write had to promote a read snapshot that another SSOT writer had committed
past -- SQLITE_BUSY_SNAPSHOT (517), returned at once, busy_timeout never
consulted.  The forward TAF inbox (the P0-M stage, 10:00Z) has the same shape.

Each test commits from a second connection at the exact moment the batch's
first write starts -- after its look-up, before its write -- on a file-backed
WAL database, and pins both sides: with the lock taken up front the batch
commits and the other writer waits its turn; on the pre-fix deferred path the
same setup fails 517.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.market_ingress as ingress
import bayes_basket_target.native_ensemble_training_facts as taf_facts
from bayes_basket_target.market_ingress import materialize_official_ws_evidence_cache
from bayes_basket_target.native_ensemble_training_facts import ingest_forward_taf_inbox
from bayes_basket_target.sqlite_busy import (
    SQLITE_BUSY_SNAPSHOT,
    begin_immediate_if_idle,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.pr12_atomic_test_support import install_canonical_v12
from bayes_basket_target.market_registry import apply_target_market_registry_schema
from tests.test_bayes_basket_target_market_ingress import EXCHANGE_SIZING, _canonical
from tests.test_tiebreak_lane import _insert
from tests.test_forward_taf_ingest_cost import _capture, _inbox


def _wal(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _other_writer(path: Path) -> sqlite3.Connection:
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("PRAGMA busy_timeout=0")
    other.execute("CREATE TABLE IF NOT EXISTS other_writer(x)")
    return other


def _commit_elsewhere_at(conn: sqlite3.Connection, other: sqlite3.Connection, prefix: str) -> list[str]:
    """When ``conn`` starts its first statement beginning with ``prefix``,
    ``other`` tries to commit.  What happened is recorded."""

    seen: list[str] = []

    def trace(sql: str) -> None:
        if seen or not sql.lstrip().upper().startswith(prefix):
            return
        try:
            other.execute("INSERT INTO other_writer VALUES(1)")
            seen.append("other_committed")
        except sqlite3.OperationalError as exc:
            seen.append(f"other_refused:{exc}")

    conn.set_trace_callback(trace)
    return seen


def test_begin_immediate_if_idle_owns_only_a_transaction_it_opened(tmp_path) -> None:
    conn = _wal(tmp_path / "t.db")
    conn.execute("CREATE TABLE t(x)")
    other = _other_writer(tmp_path / "t.db")
    assert begin_immediate_if_idle(conn) is True
    with pytest.raises(sqlite3.OperationalError):
        other.execute("INSERT INTO other_writer VALUES(1)")
    conn.rollback()
    conn.execute("BEGIN")
    assert begin_immediate_if_idle(conn) is False
    conn.rollback()


# ---------------------------------------------------------------------------
# The market-evidence cache.


def _manifest_at(directory: Path, *, observed: str, ask: float) -> Path:
    """The ingress test's one-token manifest, at a chosen time and ask."""

    directory.mkdir(parents=True)
    quote = {
        "token_id": "yes-38", "slug": "highest-temperature-in-test-on-july-23-2026",
        "event_date": "2026-07-23", "outcome": "38°C", "side": "YES",
        "best_bid": 0.4, "best_ask": ask,
        "bids": [{"price": 0.4, "size": 10.0}], "asks": [{"price": ask, "size": 10.0}],
        "book_observed_at": observed, "created_at": observed,
    }
    shard_dir = directory / "shards"
    shard_dir.mkdir()
    core = {
        "schema": "clob_ws_token_quote_snapshot_v1", "token_id": "yes-38", "quote": quote,
        "shard_index": 0, "shard_generation": 1, "shard_active": True,
        "source": "official_polymarket_clob_websocket", "evidence_only": True,
        "authorizes_order": False,
    }
    shard = {**core, "snapshot_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    (shard_dir / "yes.json").write_text(json.dumps(shard), encoding="utf-8")
    root = {
        "schema": "clob_ws_latest_quote_snapshot_v1", "state": "connected",
        "active_shards": [0], "expected_shards": 1, "active_shard_generations": {"0": 1},
        "quote_storage": "sha256_token_shards_v1", "quote_shard_dir": str(shard_dir),
        "quotes_by_token": {}, "materialized_token_count": 1, "unmaterialized_tokens": [],
        "source": "official_polymarket_clob_websocket", "evidence_only": True,
        "authorizes_order": False,
    }
    root["snapshot_sha256"] = hashlib.sha256(_canonical(root)).hexdigest()
    path = directory / "manifest.json"
    path.write_text(json.dumps(root), encoding="utf-8")
    return path


def _evidence_store(tmp_path: Path, monkeypatch) -> tuple[sqlite3.Connection, Path, Path]:
    monkeypatch.setattr(ingress, "WS_EVIDENCE_CACHE_RETAIN_DAYS", 100_000)
    db = tmp_path / "ssot.db"
    conn = _wal(db)
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    # A file-backed store reads the real registry and its snapshot; the
    # in-memory test table is refused here by design.
    apply_target_market_registry_schema(conn)
    registry_sha = "e" * 64
    _insert(conn, "basket_target_market_registry_snapshot", {
        "snapshot_sha256": registry_sha, "source_scope": "shared_live_registry_export_only",
    })
    _insert(conn, "basket_target_market_registry", {
        "snapshot_sha256": registry_sha, "slug": "highest-temperature-in-test-on-july-23-2026",
        "event_date": "2026-07-23", "outcome": "38°C", "city_slug": "test", "station_id": "TEST",
        "settlement_timezone": "UTC", "yes_token": "yes-38", "market_active": 1,
        "market_accepting_orders": 1, "source_eligible": 1, "source_lane": "awc",
        "registry_json": json.dumps({"market_id": "m-38", "lower": 38.0, "upper": 38.0, **EXCHANGE_SIZING}),
        "refreshed_at": "2026-07-23T04:00:00Z",
    })
    first = _manifest_at(tmp_path / "a", observed="2026-07-23T05:00:00Z", ask=0.42)
    assert materialize_official_ws_evidence_cache(conn, manifest_path=first, commit=True)["state"] == "COMMITTED"
    later = _manifest_at(tmp_path / "b", observed="2026-07-23T05:05:00Z", ask=0.45)
    return conn, db, later


def test_the_evidence_cache_keeps_its_write_when_another_writer_commits_mid_batch(tmp_path, monkeypatch) -> None:
    conn, db, later = _evidence_store(tmp_path, monkeypatch)
    other = _other_writer(db)
    seen = _commit_elsewhere_at(conn, other, "UPDATE BASKET_OFFICIAL_WS_EVIDENCE_CACHE")

    receipt = materialize_official_ws_evidence_cache(conn, manifest_path=later, commit=True)

    assert receipt["state"] == "COMMITTED"
    assert receipt["receipts"][0]["wrote"] is True
    # The lock was held from before the look-up, so the other writer waited.
    assert seen == ["other_refused:database is locked"]
    assert not conn.in_transaction
    conn.set_trace_callback(None)
    assert conn.execute(
        "SELECT observed_at FROM basket_official_ws_evidence_cache"
    ).fetchone()[0] == "2026-07-23T05:05:00Z"


def test_the_deferred_path_the_fix_replaced_fails_517_in_the_same_setup(tmp_path, monkeypatch) -> None:
    """Pins that the test above exercises the race, not an idle database."""

    conn, db, later = _evidence_store(tmp_path, monkeypatch)
    monkeypatch.setattr(ingress, "begin_immediate_if_idle", lambda _conn: False)
    other = _other_writer(db)
    seen = _commit_elsewhere_at(conn, other, "UPDATE BASKET_OFFICIAL_WS_EVIDENCE_CACHE")

    with pytest.raises(sqlite3.OperationalError) as caught:
        materialize_official_ws_evidence_cache(conn, manifest_path=later, commit=True)

    assert seen == ["other_committed"]
    assert caught.value.sqlite_errorcode == SQLITE_BUSY_SNAPSHOT
    assert not conn.in_transaction


# ---------------------------------------------------------------------------
# The forward TAF inbox (the P0-M stage).


def _taf_store(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    db = tmp_path / "ssot.db"
    conn = _wal(db)
    apply_production_ssot_schema(conn)
    install_canonical_v12(conn)
    conn.execute(
        "INSERT INTO calibration_station_registry("
        "station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        ("ZUUU", "ZUUU", "Chengdu", "Asia/Shanghai", "AWC",
         "daily_high_local_midnight_to_midnight", "v1", "official", "zuuu".ljust(64, "e")),
    )
    assert ingest_forward_taf_inbox(conn, _inbox([_capture("ZUUU", "2026-08-20", 1)]))["wrote_count"] == 1
    return conn, db


def _replay_then_new() -> dict:
    # The inbox's usual shape: a replay the insert only looks up, then a new
    # capture it writes.
    return _inbox([_capture("ZUUU", "2026-08-20", 1), _capture("ZUUU", "2026-08-20", 2)])


def test_the_forward_taf_inbox_keeps_its_write_when_another_writer_commits_mid_batch(tmp_path) -> None:
    conn, db = _taf_store(tmp_path)
    other = _other_writer(db)
    seen = _commit_elsewhere_at(conn, other, "INSERT INTO CALIBRATION_FORWARD_TAF_CAPTURE")

    receipt = ingest_forward_taf_inbox(conn, _replay_then_new())

    assert receipt["wrote_count"] == 1
    assert seen == ["other_refused:database is locked"]
    assert not conn.in_transaction
    conn.set_trace_callback(None)
    assert conn.execute("SELECT COUNT(*) FROM calibration_forward_taf_capture").fetchone()[0] == 2


def test_the_forward_taf_deferred_path_fails_517_in_the_same_setup(tmp_path, monkeypatch) -> None:
    conn, db = _taf_store(tmp_path)
    monkeypatch.setattr(taf_facts, "begin_immediate_if_idle", lambda _conn: False)
    other = _other_writer(db)
    seen = _commit_elsewhere_at(conn, other, "INSERT INTO CALIBRATION_FORWARD_TAF_CAPTURE")

    with pytest.raises(sqlite3.OperationalError) as caught:
        ingest_forward_taf_inbox(conn, _replay_then_new())

    assert seen == ["other_committed"]
    assert caught.value.sqlite_errorcode == SQLITE_BUSY_SNAPSHOT
    assert not conn.in_transaction


def test_inside_a_callers_transaction_the_batch_nests_and_leaves_it_open(tmp_path) -> None:
    conn, _db = _taf_store(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    receipt = ingest_forward_taf_inbox(conn, _replay_then_new())
    assert receipt["wrote_count"] == 1
    assert conn.in_transaction, "the caller's transaction is the caller's to end"
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM calibration_forward_taf_capture").fetchone()[0] == 1
