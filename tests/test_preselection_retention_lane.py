"""Cold archive planning stays read-only; the writer applies only verified rows."""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import preselection_retention_lane as lane  # noqa: E402

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _store(path: Path, *, extra_free: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE auto_trade_decisions (
          decision_id TEXT,station_id TEXT,event_date TEXT,details_json TEXT);
        CREATE TABLE basket_p0_preselection_plan (
          preselection_id TEXT,station_id TEXT,event_date TEXT,created_at TEXT,
          plan_json TEXT);
        CREATE TRIGGER plan_no_delete BEFORE DELETE ON basket_p0_preselection_plan
          BEGIN SELECT RAISE(ABORT,'append_only'); END;
    """)
    rows = [
        ("free", "2026-09-01", "sha_free"),
        ("hash_named", "2026-09-01", "sha_named"),
        ("id_named", "2026-09-01", "sha_id"),
    ]
    if extra_free:
        rows.append(("free2", "2026-09-01", "sha_free2"))
    rows.append(("latest", "2026-09-29", "sha_latest"))
    for plan_id, day, sha in rows:
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?,?)",
            (plan_id, "KLGA", day, day + "T10:00:00Z",
             json.dumps({"compiled_plan": {"production_plan_sha256": sha}})),
        )
    conn.execute("INSERT INTO auto_trade_decisions VALUES (?,?,?,?)",
                 ("d1", "KLGA", "2026-09-01", '{"production_plan_sha256":"sha_named"}'))
    conn.execute("INSERT INTO auto_trade_decisions VALUES (?,?,?,?)",
                 ("d2", "KLGA", "2026-09-01", '{"preselection_id":"id_named"}'))
    conn.commit()
    conn.close()


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch):
    db = tmp_path / "ssot.db"
    _store(db)
    monkeypatch.setattr(lane, "_external_available", lambda *_: True)
    monkeypatch.setattr(lane.legacy, "_require_external_export", lambda *_: None)
    return db, tmp_path / "cursor.json", tmp_path / "pending.json", tmp_path / "cold" / "archive"


def _archive(args):
    db, state, pending, external = args
    external.parent.mkdir(exist_ok=True)
    return lane.archive_once(db, state, pending, external, now=NOW)


def test_archive_then_apply_preserves_exact_references_and_trigger(setup) -> None:
    db, _state, pending, external = setup
    result = _archive(setup)
    assert result["state"] == "archived" and result["rows"] == 1
    assert pending.exists()
    archive = next(external.glob("*.jsonl.gz"))
    with gzip.open(archive, "rt") as stream:
        archived = json.loads(stream.readline())
    assert archived["row"]["preselection_id"] == "free"
    # A missing archive must postpone deletion, even with a local receipt.
    detached = external / "detached.gz"
    archive.rename(detached)
    assert lane.apply_once(db, pending)["state"] == "external_unavailable"
    assert pending.exists()
    detached.rename(archive)
    assert lane.apply_once(db, pending)["deleted_rows"] == 1
    conn = sqlite3.connect(db)
    assert {r[0] for r in conn.execute(
        f"SELECT preselection_id FROM {lane.TABLE}"
    )} == {"hash_named", "id_named", "latest"}
    with pytest.raises(sqlite3.DatabaseError, match="append_only"):
        conn.execute(f"DELETE FROM {lane.TABLE} WHERE preselection_id='latest'")
    conn.rollback()
    applied = next(pending.parent.glob("preselection_retention_applied_*.json"))
    assert lane.restore_archived_batch(db, applied)["rows"] == 1
    assert conn.execute(f"SELECT COUNT(*) FROM {lane.TABLE}").fetchone()[0] == 4
    assert _archive(setup)["state"] == "no_candidates"


def test_new_reference_after_archive_blocks_delete(setup) -> None:
    db, _state, pending, _external = setup
    assert _archive(setup)["state"] == "archived"
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO auto_trade_decisions VALUES (?,?,?,?)",
                 ("late", "KLGA", "2026-09-01", '{"preselection_id":"free"}'))
    conn.commit()
    with pytest.raises(RuntimeError, match="preselection_retention_new_reference"):
        lane.apply_once(db, pending)
    assert pending.exists()
    assert conn.execute(f"SELECT COUNT(*) FROM {lane.TABLE}").fetchone()[0] == 4


def test_absent_external_is_zero_ssot_work(setup, monkeypatch) -> None:
    db, state, pending, external = setup
    monkeypatch.setattr(lane, "_external_available", lambda *_: False)
    assert lane.archive_once(db, state, pending, external, now=NOW) == {
        "state": "external_unavailable"
    }
    assert not state.exists() and not pending.exists()
    assert lane.apply_once(db, pending)["state"] == "nothing_pending"


def test_external_unplug_during_apply_verification_defers_without_write(
    setup, monkeypatch,
) -> None:
    db, _state, pending, _external = setup
    assert _archive(setup)["state"] == "archived"
    before = db.stat().st_size
    def unplug(*_args):
        raise OSError("volume detached")
    monkeypatch.setattr(lane.legacy, "_verify_export_member", unplug)
    assert lane.apply_once(db, pending)["state"] == "external_unavailable"
    assert pending.exists() and db.stat().st_size == before


def test_read_only_archive_never_enters_hosted_writer_rotation() -> None:
    import weather_basket_yes_daemon as daemon

    hosted = {name for name, *_rest in daemon.HOSTED_SWEEPS}
    assert "preselection_archive" not in hosted
    assert "preselection_retention_apply" in hosted
    assert daemon.HOSTED_SWEEP_WRITE_DOMAIN["preselection_retention_apply"] == "ssot"


def test_pending_retention_priority_does_not_change_other_sweeps() -> None:
    import weather_basket_yes_daemon as daemon

    sweeps = (("preselection_retention_apply", "retention.sh", 21600.0, 120.0),
              ("other", "other.sh", 600.0, 120.0))
    due = daemon.due_hosted_sweeps(
        now_mono=1000.0,
        next_due={"preselection_retention_apply": 400.0, "other": 900.0},
        sweeps=sweeps,
        priority_intervals={"preselection_retention_apply": 600.0},
    )
    assert [item[2] for item in sorted(due, reverse=True)] == [
        "preselection_retention_apply", "other"
    ]


def test_apply_turn_is_bounded_and_resumes_batches(monkeypatch) -> None:
    calls = []

    def one_batch(*_args):
        calls.append(1)
        return {"state": "applied", "deleted_rows": 7}

    monkeypatch.setattr(lane, "apply_once", one_batch)
    monkeypatch.setattr(lane, "_pending_paths", lambda *_: [Path("still_pending")])
    result = lane.apply_turn(Path("db"), Path("pending"))
    assert result == {"state": "applied", "batches": 4, "deleted_rows": 28,
                      "continue": True}
    assert len(calls) == 4


def test_apply_turn_stops_after_external_disconnect(monkeypatch) -> None:
    outcomes = iter(({"state": "applied", "deleted_rows": 7},
                     {"state": "external_unavailable"}))
    monkeypatch.setattr(lane, "apply_once", lambda *_: next(outcomes))
    result = lane.apply_turn(Path("db"), Path("pending"))
    assert result == {"state": "applied", "batches": 1, "deleted_rows": 7,
                      "continue": False}


def test_apply_turn_stops_at_time_budget(monkeypatch) -> None:
    ticks = iter((0.0, lane.MAX_APPLY_TURN_SECONDS))
    monkeypatch.setattr(lane.time, "monotonic", lambda: next(ticks))
    calls = []
    def one_batch(*_args):
        calls.append(1)
        return {"state": "applied", "deleted_rows": 7}
    monkeypatch.setattr(lane, "apply_once", one_batch)
    monkeypatch.setattr(lane, "_pending_paths", lambda *_: [Path("pending")])
    assert lane.apply_turn()["batches"] == 1
    assert len(calls) == 1


def test_apply_turn_queue_drained_does_not_request_another_slot(monkeypatch) -> None:
    outcomes = iter(({"state": "applied", "deleted_rows": 7},
                     {"state": "nothing_pending"}))
    monkeypatch.setattr(lane, "apply_once", lambda *_: next(outcomes))
    assert lane.apply_turn() == {
        "state": "applied", "batches": 1, "deleted_rows": 7, "continue": False,
    }


def test_oversized_receipt_rejected_before_archive_read(setup, monkeypatch) -> None:
    db, _state, pending, _external = setup
    _archive(setup)
    receipt = json.loads(pending.read_text())
    receipt["payload_bytes"] = lane.MAX_BATCH_BYTES + 1
    pending.write_text(json.dumps(receipt))
    def must_not_read(*_args):
        pytest.fail("oversized receipt reached archive decompression")
    monkeypatch.setattr(lane.legacy, "_verify_export_member", must_not_read)
    with pytest.raises(RuntimeError, match="payload_out_of_bounds"):
        lane.apply_once(db, pending)
    assert pending.exists()


def test_memory_probe_failure_prevents_ssot_access(monkeypatch) -> None:
    def unavailable(**_kwargs):
        raise RuntimeError("memory_probe_unavailable")
    def must_not_open(*_args, **_kwargs):
        pytest.fail("SSOT opened after memory guard failed")
    monkeypatch.setattr(lane, "assert_process_memory_budget", unavailable)
    monkeypatch.setattr(lane.sqlite3, "connect", must_not_open)
    with pytest.raises(RuntimeError, match="memory_probe_unavailable"):
        lane.archive_once()
    with pytest.raises(RuntimeError, match="memory_probe_unavailable"):
        lane.apply_once()


def test_crash_before_pending_receipt_reuses_verified_archive(setup) -> None:
    _db, state, pending, external = setup
    assert _archive(setup)["state"] == "archived"
    pending.unlink()
    state.unlink()
    assert _archive(setup)["state"] == "archived"
    assert len(list(external.glob("*.jsonl.gz"))) == 1


def test_two_batches_resume_without_skipping_or_duplicate_archive(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "two.db"
    _store(db, extra_free=True)
    monkeypatch.setattr(lane, "_external_available", lambda *_: True)
    monkeypatch.setattr(lane.legacy, "_require_external_export", lambda *_: None)
    conn = sqlite3.connect(db)
    columns = lane.legacy._columns(conn, lane.TABLE)
    sizes = [len(lane._row_payload(columns, row)) for row in conn.execute(
        f"SELECT rowid,* FROM {lane.TABLE} WHERE preselection_id IN ('free','free2')"
    )]
    monkeypatch.setattr(lane, "MAX_BATCH_BYTES", max(sizes) + 1)
    conn.close()
    args = (db, tmp_path / "cursor.json", tmp_path / "pending.json",
            tmp_path / "cold" / "archive")
    assert _archive(args)["rows"] == 1
    assert lane.apply_once(db, args[2])["deleted_rows"] == 1
    assert _archive(args)["rows"] == 1
    assert lane.apply_once(db, args[2])["deleted_rows"] == 1
    assert _archive(args)["state"] == "no_candidates"
    assert len(list(args[3].glob("*.jsonl.gz"))) == 2
    conn = sqlite3.connect(db)
    assert {r[0] for r in conn.execute(f"SELECT preselection_id FROM {lane.TABLE}")} == {
        "hash_named", "id_named", "latest"
    }


def test_prearchive_queue_drains_in_rowid_order(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "queue.db"
    _store(db, extra_free=True)
    monkeypatch.setattr(lane, "_external_available", lambda *_: True)
    monkeypatch.setattr(lane.legacy, "_require_external_export", lambda *_: None)
    conn = sqlite3.connect(db)
    columns = lane.legacy._columns(conn, lane.TABLE)
    sizes = [len(lane._row_payload(columns, row)) for row in conn.execute(
        f"SELECT rowid,* FROM {lane.TABLE} WHERE preselection_id IN ('free','free2')"
    )]
    monkeypatch.setattr(lane, "MAX_BATCH_BYTES", max(sizes) + 1)
    conn.close()
    args = (db, tmp_path / "cursor.json", tmp_path / "pending.json",
            tmp_path / "cold" / "archive")
    assert _archive(args)["rows"] == 1
    assert _archive(args)["rows"] == 1
    assert len(lane._pending_paths(args[2])) == 2
    assert lane.apply_once(db, args[2])["deleted_rows"] == 1
    assert lane.apply_once(db, args[2])["deleted_rows"] == 1
    assert lane.apply_once(db, args[2])["state"] == "nothing_pending"
    assert not lane._pending_paths(args[2])


def test_corrupt_receipt_identity_cannot_authorize_delete(setup) -> None:
    db, _state, pending, _external = setup
    assert _archive(setup)["state"] == "archived"
    receipt = json.loads(pending.read_text())
    receipt["plan_scopes"][0][2] = "wrong_sha"
    pending.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="preselection_retention_receipt_identity_changed"):
        lane.apply_once(db, pending)
    conn = sqlite3.connect(db)
    assert conn.execute(f"SELECT COUNT(*) FROM {lane.TABLE}").fetchone()[0] == 4


def test_external_disappears_mid_archive_without_advancing_cursor(
    setup, monkeypatch
) -> None:
    _db, state, pending, external = setup

    def _lost_volume(*_args, **_kwargs):
        raise OSError("volume disappeared")

    monkeypatch.setattr(lane.gzip, "GzipFile", _lost_volume)
    assert _archive(setup)["state"] == "external_unavailable"
    assert not pending.exists() and not state.exists()
    assert not list(external.glob("*.partial"))
