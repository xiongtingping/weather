from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import resource_bounds
from bayes_basket_target.database import SHARED_LIVE_DB


def test_oversized_json_is_rejected_before_parse(tmp_path: Path) -> None:
    path = tmp_path / "oversized.json"
    path.write_bytes(b"{" + b"a" * 128 + b"}")

    with pytest.raises(resource_bounds.ResourceBoundError, match="target_input_bytes_exceeded"):
        resource_bounds.load_bounded_json(path, limit=32)


def test_deep_json_is_rejected_after_bounded_read(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    value: object = 0
    for _ in range(resource_bounds.MAX_JSON_DEPTH + 2):
        value = [value]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(resource_bounds.ResourceBoundError, match="target_json_depth_exceeded"):
        resource_bounds.load_bounded_json(path)


def test_oversized_json_output_leaves_no_partial_file(tmp_path: Path) -> None:
    destination = tmp_path / "receipt.json"

    with pytest.raises(resource_bounds.ResourceBoundError, match="target_json_output_bytes_exceeded"):
        resource_bounds.write_bounded_json(destination, {"payload": "x" * 128}, limit=32)

    assert not destination.exists()
    assert not list(tmp_path.glob(".*.tmp.*"))


def test_shared_live_database_is_always_rejected() -> None:
    with pytest.raises(resource_bounds.ResourceBoundError, match="shared_live_database"):
        resource_bounds.assert_target_db_capacity(SHARED_LIVE_DB)


def test_process_memory_budget_rejects_projected_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resource_bounds, "_process_rss_bytes", lambda: 100)

    with pytest.raises(resource_bounds.ResourceBoundError, match="process_memory_budget"):
        resource_bounds.assert_process_memory_budget(projected_bytes=1, limit=100)


def test_process_memory_runtime_limit_matches_declared_512_mib_and_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert resource_bounds.MAX_TARGET_PROCESS_RSS_BYTES == 512 * resource_bounds.MIB
    limit = resource_bounds.MAX_TARGET_PROCESS_RSS_BYTES

    monkeypatch.setattr(resource_bounds, "_process_rss_bytes", lambda: limit - 1)
    assert resource_bounds.assert_process_memory_budget(projected_bytes=1) == limit - 1

    with pytest.raises(resource_bounds.ResourceBoundError, match="process_memory_budget"):
        resource_bounds.assert_process_memory_budget(projected_bytes=2)


def test_resource_snapshot_reports_memory_guard(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    db.write_bytes(b"x")

    snapshot = resource_bounds.inspect_target_resource_budget(db)

    assert snapshot["memory_probe_available"] is True
    assert snapshot["process_rss_bytes"] >= 0
    assert snapshot["limits"]["max_process_rss_bytes"] == resource_bounds.MAX_TARGET_PROCESS_RSS_BYTES
    assert "target_process_memory_budget_exceeded" not in snapshot["violations"]


def test_file_connection_gets_hard_page_cap_without_touching_shared_live(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (value text)")
        resource_bounds.assert_connection_target_capacity(conn)
        page_size = int(conn.execute("pragma page_size").fetchone()[0])
        max_pages = int(conn.execute("pragma max_page_count").fetchone()[0])

    assert max_pages * page_size <= resource_bounds.MAX_TARGET_DB_BYTES


def test_connection_guard_does_not_recover_wal_without_explicit_writer_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        wal = _seed_oversized_test_wal(conn)
        before = wal.stat().st_size
        monkeypatch.setattr(resource_bounds, "MAX_TARGET_WAL_BYTES", 1)

        with pytest.raises(
            resource_bounds.ResourceBoundError,
            match="target_database_wal_bytes_exceeded$",
        ):
            resource_bounds.assert_connection_target_capacity(conn)

        assert wal.stat().st_size == before


def test_connection_guard_recovers_wal_in_explicit_writer_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        wal = _seed_oversized_test_wal(conn)
        monkeypatch.setattr(resource_bounds, "MAX_TARGET_WAL_BYTES", 1)

        assert resource_bounds.assert_connection_target_capacity(
            conn,
            recover_oversized_wal=True,
        ) == db.resolve()

        assert wal.stat().st_size <= 1


def _seed_oversized_test_wal(conn: sqlite3.Connection) -> Path:
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma wal_autocheckpoint=0")
    conn.execute("create table if not exists wal_payload(value blob)")
    conn.commit()
    conn.execute("insert into wal_payload(value) values (?)", (b"x" * 8192,))
    conn.commit()
    db_path = Path(str(conn.execute("pragma database_list").fetchone()[2]))
    wal = db_path.with_name(db_path.name + "-wal")
    assert wal.stat().st_size > 1
    return wal


def test_oversized_target_wal_is_checkpointed_once_and_repeat_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        wal = _seed_oversized_test_wal(conn)
        monkeypatch.setattr(resource_bounds, "MAX_TARGET_WAL_BYTES", 1)

        first = resource_bounds.checkpoint_target_wal_once_if_oversized(conn, limit=1)
        second = resource_bounds.checkpoint_target_wal_once_if_oversized(conn, limit=1)

        assert first["state"] == "checkpointed"
        assert first["attempted"] is True
        assert first["wal_bytes_before"] > 1
        assert first["wal_bytes_after"] <= 1
        assert second == {
            "state": "within_budget",
            "attempted": False,
            "wal_bytes_before": wal.stat().st_size,
            "wal_bytes_after": wal.stat().st_size,
        }


def test_oversized_target_wal_checkpoint_busy_remains_fail_closed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    writer = sqlite3.connect(db)
    reader = sqlite3.connect(db)
    try:
        _seed_oversized_test_wal(writer)
        reader.execute("begin")
        reader.execute("select count(*) from wal_payload").fetchone()
        writer.execute("insert into wal_payload(value) values (?)", (b"y" * 8192,))
        writer.commit()

        with pytest.raises(resource_bounds.ResourceBoundError, match="wal_checkpoint_busy"):
            resource_bounds.checkpoint_target_wal_once_if_oversized(writer, limit=1)
    finally:
        reader.close()
        writer.close()


def test_oversized_target_wal_with_active_transaction_is_not_checkpointed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as conn:
        wal = _seed_oversized_test_wal(conn)
        before = wal.stat().st_size
        conn.execute("begin")

        with pytest.raises(resource_bounds.ResourceBoundError, match="transaction_active"):
            resource_bounds.checkpoint_target_wal_once_if_oversized(conn, limit=1)

        assert wal.stat().st_size == before


def test_query_only_connection_reports_oversized_wal_without_checkpoint(
    tmp_path: Path,
) -> None:
    db = tmp_path / "target.db"
    with sqlite3.connect(db) as writer:
        wal = _seed_oversized_test_wal(writer)
        before = wal.stat().st_size
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as reader:
            reader.execute("pragma query_only=on")

            with pytest.raises(
                resource_bounds.ResourceBoundError,
                match="target_database_wal_bytes_exceeded$",
            ):
                resource_bounds.checkpoint_target_wal_once_if_oversized(
                    reader,
                    limit=1,
                )

        assert wal.stat().st_size == before


def test_wal_checkpoint_rejects_shared_live_before_sqlite_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SharedLiveConnection:
        in_transaction = False

        def execute(self, sql: str):
            assert sql == "PRAGMA database_list"
            return self

        def fetchone(self):
            return (0, "main", str(SHARED_LIVE_DB))

    with pytest.raises(resource_bounds.ResourceBoundError, match="shared_live_database"):
        resource_bounds.checkpoint_target_wal_once_if_oversized(SharedLiveConnection())  # type: ignore[arg-type]


def test_database_budget_rejects_growth_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "target.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(resource_bounds, "MAX_TARGET_DB_BYTES", 0)

    with pytest.raises(resource_bounds.ResourceBoundError, match="target_database_bytes_exceeded"):
        resource_bounds.assert_target_db_capacity(db)


def test_aggregate_raw_budget_covers_all_providers(tmp_path: Path) -> None:
    for provider in ("ecmwf_ifs025_official", "gfs_0p25_official"):
        raw = tmp_path / provider / "raw"
        raw.mkdir(parents=True)
        (raw / ("a" * 64 + ".bin")).write_bytes(b"x" * 8)

    assert resource_bounds.assert_target_raw_retention_budget(tmp_path, bytes_needed=1, limit=32) == 16
    with pytest.raises(resource_bounds.ResourceBoundError, match="aggregate_retention"):
        resource_bounds.assert_target_raw_retention_budget(tmp_path, bytes_needed=17, limit=32)


def test_aggregate_raw_budget_counts_deep_candidate_provider_layout(
    tmp_path: Path,
) -> None:
    raw = (
        tmp_path
        / "candidate_raw/2026-07-30/Asia_Seoul/RKPK/run"
        / "gem_gdps_official/raw/hash.bin"
    )
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"x" * 17)

    assert resource_bounds.assert_target_raw_retention_budget(
        tmp_path,
        bytes_needed=1,
        limit=32,
    ) == 17
    with pytest.raises(
        resource_bounds.ResourceBoundError,
        match="aggregate_retention",
    ):
        resource_bounds.assert_target_raw_retention_budget(
            tmp_path,
            bytes_needed=16,
            limit=32,
        )


def test_raw_budget_rejects_write_that_would_cross_free_space_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_bounds, "_filesystem_free_bytes", lambda _path: 100)

    with pytest.raises(resource_bounds.ResourceBoundError, match="raw_min_free_space_guard"):
        resource_bounds.assert_target_raw_retention_budget(
            tmp_path,
            bytes_needed=1,
            limit=32,
            min_free_bytes=100,
        )


def test_resource_snapshot_is_read_only_and_reports_target_only_usage(tmp_path: Path) -> None:
    db = tmp_path / "target.db"
    db.write_bytes(b"x" * 7)
    raw = tmp_path / "raw" / "provider" / "raw"
    raw.mkdir(parents=True)
    (raw / "object.bin").write_bytes(b"y" * 5)

    snapshot = resource_bounds.inspect_target_resource_budget(db, raw_root=tmp_path / "raw")

    assert snapshot["ok"] is True
    assert snapshot["database_bytes"] == 7
    assert snapshot["raw_bytes"] == 5
    assert snapshot["raw_file_count"] == 1
    assert db.read_bytes() == b"x" * 7
    assert (raw / "object.bin").read_bytes() == b"y" * 5
