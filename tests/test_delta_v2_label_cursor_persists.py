"""The delta_v2 label cursor survives a restart.

Held only in memory, every restart rescanned the training facts from rowid
0: 235s on the first cycle after the 2026-09-21 05:44Z restart, and four
restarts that day.  The retry lane wraps to rowid 0 on its own, so resuming
from the persisted page skips nothing a page would not revisit.
"""

import json
import sqlite3
from pathlib import Path

from bayes_basket_target import production_worker as pw


def _worker(tmp_path: Path, *, rows: int = 100):
    worker = object.__new__(pw.TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    worker.commit_conn = sqlite3.connect(":memory:")
    worker.commit_conn.execute(
        "CREATE TABLE basket_policy_training_fact(id INTEGER PRIMARY KEY, fact_id TEXT)"
    )
    worker.commit_conn.executemany(
        "INSERT INTO basket_policy_training_fact(fact_id) VALUES(?)",
        [(f"d2fact_{i}",) for i in range(rows)],
    )
    worker._delta_v2_label_fact_cursor = 0
    worker._delta_v2_label_cursor_initialized = False
    return worker


def _write(tmp_path: Path, **fields) -> None:
    payload = {
        "schema": "delta_v2_label_cursor_v2",
        "scan_after_rowid": 50,
        "retry_after_rowid": 10,
        "next_lane": "new",
        **fields,
    }
    (tmp_path / "delta_v2_label_cursor.json").write_text(json.dumps(payload))


def test_a_persisted_cursor_seeds_the_next_process(tmp_path) -> None:
    _write(tmp_path)
    worker = _worker(tmp_path)
    worker._load_persisted_delta_v2_label_cursor()
    assert worker._delta_v2_label_cursor_initialized is True
    assert worker._delta_v2_label_fact_cursor == 50
    assert worker._delta_v2_label_cursor_state == {
        "schema": "delta_v2_label_cursor_v2",
        "scan_after_rowid": 50,
        "retry_after_rowid": 10,
        "next_lane": "new",
    }


def test_a_cursor_past_the_stores_newest_fact_is_a_cold_start(tmp_path) -> None:
    _write(tmp_path, scan_after_rowid=500, retry_after_rowid=0)
    worker = _worker(tmp_path)
    worker._load_persisted_delta_v2_label_cursor()
    assert worker._delta_v2_label_cursor_initialized is False
    assert worker._delta_v2_label_fact_cursor == 0


def test_an_inconsistent_or_malformed_file_is_ignored(tmp_path) -> None:
    for fields in (
        {"retry_after_rowid": 60},          # retry past scan
        {"next_lane": "sideways"},
        {"scan_after_rowid": "fifty"},
        {"schema": "something_else"},
    ):
        _write(tmp_path, **fields)
        worker = _worker(tmp_path)
        worker._load_persisted_delta_v2_label_cursor()
        assert worker._delta_v2_label_cursor_initialized is False, fields
    (tmp_path / "delta_v2_label_cursor.json").write_text("{not json")
    worker = _worker(tmp_path)
    worker._load_persisted_delta_v2_label_cursor()
    assert worker._delta_v2_label_cursor_initialized is False


def test_the_file_is_read_once_per_process_and_never_over_a_committed_page(tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._load_persisted_delta_v2_label_cursor()          # no file yet
    _write(tmp_path)
    worker._load_persisted_delta_v2_label_cursor()          # already looked
    assert worker._delta_v2_label_cursor_initialized is False
    other = _worker(tmp_path)
    other._delta_v2_label_cursor_initialized = True
    other._delta_v2_label_fact_cursor = 7
    other._load_persisted_delta_v2_label_cursor()           # a page won
    assert other._delta_v2_label_fact_cursor == 7


def test_a_committed_page_is_persisted_and_read_back(tmp_path) -> None:
    worker = _worker(tmp_path)
    worker._delta_v2_label_cursor_state = {
        "schema": "delta_v2_label_cursor_v2",
        "scan_after_rowid": 42,
        "retry_after_rowid": 3,
        "next_lane": "retry",
    }
    worker._persist_delta_v2_label_cursor(as_of_utc="2026-09-21T06:00:00Z")
    raw = json.loads((tmp_path / "delta_v2_label_cursor.json").read_text())
    assert raw["scan_after_rowid"] == 42 and raw["retry_after_rowid"] == 3
    assert raw["next_lane"] == "retry"
    assert raw["persisted_at_utc"] == "2026-09-21T06:00:00Z"
    fresh = _worker(tmp_path)
    fresh._load_persisted_delta_v2_label_cursor()
    assert fresh._delta_v2_label_fact_cursor == 42


def test_a_worker_without_a_runtime_root_neither_reads_nor_writes(tmp_path) -> None:
    worker = _worker(tmp_path)
    del worker.nwp_runtime_root
    worker._delta_v2_label_cursor_state = {"scan_after_rowid": 1, "retry_after_rowid": 0, "next_lane": "new"}
    worker._persist_delta_v2_label_cursor(as_of_utc="t")
    worker._load_persisted_delta_v2_label_cursor()
    assert not (tmp_path / "delta_v2_label_cursor.json").exists()
    assert worker._delta_v2_label_cursor_initialized is False
