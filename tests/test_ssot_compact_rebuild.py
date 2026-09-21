"""The compact rebuild, end to end on a small store (2026-09-21).

Two passes -- ``stage`` onto a key-less staging file, ``build`` from it --
each stopped part-way by its budget and resumed, then ``verify`` against a
fingerprint of the source.  What must survive: every row with its rowid,
the AUTOINCREMENT high-water mark, the append-only triggers (live, and not
fired during the copy), the fold applied exactly once and parsed by the
kernel, and the off-grid rung the first cut of charter 168 stored.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _module():
    spec = importlib.util.spec_from_file_location(
        "rebuild_ssot_compact_under_test", ROOT / "scripts" / "rebuild_ssot_compact.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._residents_running = lambda: []
    return module


def _ladder(start: float, n: int, sign: int) -> list[dict[str, float]]:
    return [{"price": round(start + sign * 0.01 * i, 2), "shares": 5.0 + i} for i in range(n)]


def _bucket(asks, bids) -> dict:
    return {"bucket": "20-21", "ordinal": 2, "price_tick_size": 0.01,
            "ask_levels": asks, "ask_available": bool(asks),
            "bid_levels": bids, "bid_price": bids[0]["price"] if bids else 0.0}


def _source(path: Path) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE basket_official_ws_snapshot("
              "snapshot_id TEXT PRIMARY KEY, buckets_json TEXT, created_at TEXT)")
    c.execute("CREATE TABLE basket_p0_preselection_plan(plan_id TEXT PRIMARY KEY, plan_json TEXT)")
    c.execute("CREATE TABLE auto_trade_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    c.execute("CREATE TABLE basket_policy_training_fact(k TEXT PRIMARY KEY, feature_json TEXT, n NUMERIC)")
    c.execute("CREATE INDEX idx_snap_created ON basket_official_ws_snapshot(created_at)")
    c.execute("CREATE VIEW v_dec AS SELECT id FROM auto_trade_decisions")
    c.execute("CREATE TRIGGER no_upd BEFORE UPDATE ON auto_trade_decisions "
              "BEGIN SELECT RAISE(ABORT,'append_only'); END")
    for i in range(1500):
        if i % 3 == 0:     # a stored row the first cut folded: six levels, the sixth off grid
            asks = _ladder(0.30, 5, +1) + [{"price": 0.355283, "shares": 90.0}]
            bids = _ladder(0.29, 5, -1) + [{"price": 0.219444, "shares": 80.0}]
        else:              # a raw ladder the rebuild folds for the first time
            asks, bids = _ladder(0.30, 14, +1), _ladder(0.29, 9, -1)
        c.execute("INSERT INTO basket_official_ws_snapshot VALUES(?,?,?)",
                  (f"s{i}", json.dumps([_bucket(asks, bids)], sort_keys=True), f"t{i:05d}"))
    c.execute("DELETE FROM basket_official_ws_snapshot WHERE rowid % 7 = 0")
    for i in range(300):
        c.execute("INSERT INTO basket_p0_preselection_plan VALUES(?,?)", (f"p{i}", json.dumps({"x": i})))
    for i in range(30000):
        c.execute("INSERT INTO auto_trade_decisions(v) VALUES(?)", (f"v{i}",))
    c.execute("DELETE FROM auto_trade_decisions WHERE id > 29990")
    for i in range(12000):
        c.execute("INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
                  (f"k{i}", json.dumps({"i": i}), 1.0 if i % 2 else 2))
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()


class _Clock:
    """Every reading advances a second, so a small budget stops mid-table."""

    def __init__(self) -> None:
        self.t = 0.0

    def time(self) -> float:
        self.t += 1.0
        return self.t


def _run_to_completion(call, done: str) -> tuple[dict, int]:
    for attempt in range(1, 400):
        receipt = call()
        if receipt["state"] != "PARTIAL":
            assert receipt["state"] == done, receipt
            return receipt, attempt
    raise AssertionError("never finished")


def test_two_passes_resume_and_prove_the_copy(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    source, staging, target = tmp_path / "src.db", tmp_path / "stage.db", tmp_path / "out.db"
    _source(source)
    fingerprint = module.fingerprint(source)

    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    staged, stage_runs = _run_to_completion(
        lambda: module.stage(source=source, staging=staging, budget_seconds=6.0), "STAGED")
    built, build_runs = _run_to_completion(
        lambda: module.build(source=staging, target=target, budget_seconds=6.0,
                             expected=fingerprint), "BUILT")
    monkeypatch.undo()
    assert stage_runs > 2 and build_runs > 2          # both passes really resumed

    # The fold happened once, in the staging pass; assembly copied it verbatim.
    assert staged["folded_snapshots"] == fingerprint["counts"]["basket_official_ws_snapshot"]
    assert built["source_staged"] is True
    assert built["folded_snapshots"] == 0

    proof = module.verify(source=source, target=target, expected=fingerprint)
    assert proof["state"] == "VERIFIED", proof
    assert proof["fold"]["kernel_new_rejections"] == 0
    assert proof["fold"]["kernel_repaired"] > 0      # the off-grid rows now parse
    assert proof["fold"]["max_depth_after"] == 6

    out = sqlite3.connect(target)
    # rowids kept, gaps and all; staging bookkeeping did not leak into the store
    assert [r[0] for r in out.execute(
        "SELECT rowid FROM basket_official_ws_snapshot ORDER BY rowid LIMIT 8")] == [1, 2, 3, 4, 5, 6, 8, 9]
    names = {r[0] for r in out.execute("SELECT name FROM sqlite_master")}
    assert module.STAGE_SCHEMA_TABLE not in names and module.STAGE_SEQUENCE_TABLE not in names
    # values keep their storage class through the typeless staging column
    assert out.execute("SELECT typeof(n) FROM basket_policy_training_fact WHERE k='k1'").fetchone()[0] == \
        sqlite3.connect(source).execute(
            "SELECT typeof(n) FROM basket_policy_training_fact WHERE k='k1'").fetchone()[0]
    # the AUTOINCREMENT high-water mark, not the highest surviving id
    assert out.execute("SELECT seq FROM sqlite_sequence WHERE name='auto_trade_decisions'").fetchone()[0] == 30000
    assert out.execute("INSERT INTO auto_trade_decisions(v) VALUES('n') RETURNING id").fetchone()[0] == 30001
    with pytest.raises(sqlite3.Error, match="append_only"):
        out.execute("UPDATE auto_trade_decisions SET v='x' WHERE id=1")
    assert out.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_staging_file_is_not_staged_again(tmp_path: Path) -> None:
    module = _module()
    source, staging = tmp_path / "src.db", tmp_path / "stage.db"
    _source(source)
    assert module.stage(source=source, staging=staging, budget_seconds=600)["state"] == "STAGED"
    again = module.stage(source=staging, staging=tmp_path / "stage2.db", budget_seconds=600)
    assert again["state"] == "REFUSED_SOURCE_IS_STAGING"


def test_a_finished_target_is_never_resumed_into(tmp_path: Path) -> None:
    module = _module()
    source, staging = tmp_path / "src.db", tmp_path / "stage.db"
    _source(source)
    assert module.stage(source=source, staging=staging, budget_seconds=600)["state"] == "STAGED"
    assert module.stage(source=source, staging=staging, budget_seconds=600)["state"] == "REFUSED_TARGET_EXISTS"
