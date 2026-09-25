"""Charter 178: reads whose answer cannot have changed are not read again.

Ranked by the first cycle measured with the statement ledger (2026-09-21
08:54Z, 1,745s deep in swap): lineage document bodies (33,075 calls,
175.8s), the scope-timezone index scan whose pages were evicted between
calls (6 calls, 64.3s), the METAR trajectory artifacts (3 calls, 60.8s),
the same-day climatology record (228 calls, 43.3s), the observed-high
registry read (145 calls, 36.3s), and the active registry rows behind every
envelope load (3,817 calls).  Each is either content-addressed or changes
only when a named table changes, and each is remembered on exactly that.

The same-day climatology read is gone rather than remembered: it fed only
the conditional tail skew's headroom, deleted 2026-09-22 (charter 43
amendment), so its test left with it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import market_registry as mr
from bayes_basket_target import materialization as mat
from bayes_basket_target import metar_physical_forward as mpf
from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target.contracts import ContractError

REF = mat.LINEAGE_DOCUMENT_REF_PREFIX


def _trace(conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return statements


# --- lineage documents -------------------------------------------------------

def _document_store(path: Path, bodies: dict[str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE basket_probability_lineage_document("
        "document_sha256 TEXT PRIMARY KEY, document TEXT NOT NULL, created_at_utc TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO basket_probability_lineage_document VALUES(?,?,?)",
        [(sha, body, "t") for sha, body in bodies.items()],
    )
    conn.commit()
    return conn


@pytest.fixture(autouse=True)
def _fresh():
    mat.forget_lineage_documents()
    mr._ACTIVE_ROWS_MEMO.clear(); mr._SCOPE_LANES_MEMO.clear()
    mr._SCOPE_TIMEZONE_TRIPLES_MEMO.clear(); mpf._STATION_ARTIFACTS_MEMO.clear()
    yield
    mat.forget_lineage_documents()


def test_a_body_is_read_once_per_database(tmp_path) -> None:
    sha = "a" * 64
    conn = _document_store(tmp_path / "one.db", {sha: '{"curve":[1,2,3]}'})
    lineage = {"curve": REF + sha, "plain": "x"}
    assert mat.rehydrate_lineage_documents(conn, lineage)["curve"] == '{"curve":[1,2,3]}'
    statements = _trace(conn)
    again = mat.rehydrate_lineage_documents(conn, lineage)
    assert again == {"curve": '{"curve":[1,2,3]}', "plain": "x"}
    assert not any("basket_probability_lineage_document" in sql for sql in statements), statements


def test_another_database_missing_the_body_still_fails_closed(tmp_path) -> None:
    sha = "b" * 64
    holder = _document_store(tmp_path / "holder.db", {sha: "body"})
    mat.rehydrate_lineage_documents(holder, {"k": REF + sha})
    empty = _document_store(tmp_path / "empty.db", {})
    with pytest.raises(ContractError, match="probability_lineage_document_missing"):
        mat.rehydrate_lineage_documents(empty, {"k": REF + sha})


def test_an_unnamed_database_is_never_remembered() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_lineage_document("
        "document_sha256 TEXT PRIMARY KEY, document TEXT NOT NULL, created_at_utc TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO basket_probability_lineage_document VALUES(?,?,?)", ("c" * 64, "body", "t"))
    mat.rehydrate_lineage_documents(conn, {"k": REF + "c" * 64})
    statements = _trace(conn)
    mat.rehydrate_lineage_documents(conn, {"k": REF + "c" * 64})
    assert any("basket_probability_lineage_document" in sql for sql in statements)
    assert not mat._LINEAGE_DOCUMENT_CACHE


def test_the_body_cache_is_bounded_by_bytes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mat, "_LINEAGE_DOCUMENT_CACHE_MAX_BYTES", 250)
    bodies = {f"{i:064x}": "x" * 100 for i in range(6)}
    conn = _document_store(tmp_path / "bounded.db", bodies)
    for sha in bodies:
        mat.rehydrate_lineage_documents(conn, {"k": REF + sha})
    assert mat._LINEAGE_DOCUMENT_CACHE_BYTES <= 250
    assert len(mat._LINEAGE_DOCUMENT_CACHE) == 2


# --- registry reads ------------------------------------------------------------

def _registry(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    mr.apply_target_market_registry_schema(conn)
    return conn


def _snapshot(conn, sha: str, *, day: str, tz: str = "America/New_York", token: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "(snapshot_sha256,source_schema,source_scope,exported_at_utc,"
        "source_database_path,row_count,ingested_at_utc,content_sha256) VALUES(?,?,?,?,?,?,?,?)",
        (sha, "reported_trajectory_market_registry_v1", "shared_live_registry_export_only",
         f"{day}T00:00:00Z", "/isolated/source.db", 1, f"{day}T00:00:00Z", sha),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sha, f"slug-{token}", day, "38°C", "test", "TEST", tz, "C", "awc", 1, "", "",
         "c" * 64, token, f"no-{token}", 1, 1, json.dumps({"market_id": token}), f"{day}T00:00:00Z"),
    )
    conn.commit()


def test_active_rows_are_remembered_until_a_snapshot_lands(tmp_path) -> None:
    conn = _registry(tmp_path / "registry.db")
    _snapshot(conn, "a" * 64, day="2026-09-22", token="yes-1")
    first = mr.load_active_registry_rows(conn, station_id="TEST", event_date="2026-09-22")
    statements = _trace(conn)
    second = mr.load_active_registry_rows(conn, station_id="TEST", event_date="2026-09-22")
    assert second == first
    assert not any("FROM basket_target_market_registry WHERE" in sql for sql in statements), statements
    second[0]["yes_token"] = "tampered"  # a caller's copy is its own
    assert mr.load_active_registry_rows(conn, station_id="TEST", event_date="2026-09-22") == first
    _snapshot(conn, "b" * 64, day="2026-09-22", token="yes-2")
    moved = mr.load_active_registry_rows(conn, station_id="TEST", event_date="2026-09-22")
    assert [row["yes_token"] for row in moved] == ["yes-2"]


def test_retention_of_the_oldest_snapshot_is_seen(tmp_path) -> None:
    conn = _registry(tmp_path / "registry.db")
    _snapshot(conn, "a" * 64, day="2026-09-20", token="old")
    _snapshot(conn, "b" * 64, day="2026-09-22", token="new")
    before = mr.registry_scope_timezone_triples(conn)
    assert ("TEST", "2026-09-20", "America/New_York") in before
    conn.execute("DELETE FROM basket_target_market_registry WHERE snapshot_sha256=?", ("a" * 64,))
    conn.commit()
    after = mr.registry_scope_timezone_triples(conn)
    assert ("TEST", "2026-09-20", "America/New_York") not in after
    lanes = mr.registry_scope_timezone_lanes(conn, station_id="TEST", event_date="2026-09-22")
    assert lanes == [("TEST", "2026-09-22", "America/New_York", "awc")]


def test_the_fingerprint_is_three_seeks_not_a_scan(tmp_path) -> None:
    conn = _registry(tmp_path / "registry.db")
    for sql in (
        f"SELECT MAX(rowid) FROM {mr.SNAPSHOT_TABLE}",
        f"SELECT MIN(rowid) FROM {mr.TABLE}",
        f"SELECT MAX(rowid) FROM {mr.TABLE}",
    ):
        plan = " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert plan.startswith("SEARCH"), (sql, plan)


# --- METAR trajectory artifacts ----------------------------------------------------

def test_trajectory_artifacts_are_parsed_once_until_the_table_moves(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "evidence.db"))
    conn.execute(
        "CREATE TABLE basket_probability_evidence_artifact(station_id TEXT, scope_key TEXT,"
        " trained_through_utc TEXT, valid_from_utc TEXT, artifact_json TEXT, artifact_kind TEXT)"
    )
    bucket = mpf._BUCKET_PREFERENCE[0]
    def add(station, trained, available, marker):
        conn.execute(
            "INSERT INTO basket_probability_evidence_artifact VALUES(?,?,?,?,?,?)",
            (station, f"{station}|x|{bucket}", trained, available,
             json.dumps({"remaining_warming_distribution": {"0.0": marker}, "content_sha256": "s" * 64}),
             "METAR_TRAJECTORY_LIKELIHOOD"),
        )
        conn.commit()
    noon = datetime(2026, 9, 25, 4, tzinfo=timezone.utc)
    add("ZUUU", "2026-09-20T00:00:00Z", "2026-09-21T16:00:00Z", 1)
    first = mpf._station_artifact_timeline(conn)
    statements = _trace(conn)
    assert mpf._station_artifact_timeline(conn) == first
    assert not any("artifact_json" in sql for sql in statements), statements
    chosen = mpf._artifact_as_of(conn, first["ZUUU"], noon)
    assert chosen["remaining_warming_distribution"] == {"0.0": 1}
    # The day's own artifact is parsed once, then remembered.
    statements.clear()
    assert mpf._artifact_as_of(conn, first["ZUUU"], noon) == chosen
    assert not any("artifact_json" in sql for sql in statements), statements
    add("ZUUU", "2026-09-21T00:00:00Z", "2026-09-22T16:00:00Z", 2)
    moved = mpf._station_artifact_timeline(conn)
    assert len(moved["ZUUU"]) == 2
    assert mpf._artifact_as_of(conn, moved["ZUUU"], noon)["remaining_warming_distribution"] == {"0.0": 2}
