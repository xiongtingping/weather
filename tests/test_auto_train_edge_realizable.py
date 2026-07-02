import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
CALIBRATE_SPEC = importlib.util.spec_from_file_location(
    "edge_realizable_calibrate", ROOT / "scripts" / "edge_realizable_calibrate.py"
)
edge_realizable_calibrate = importlib.util.module_from_spec(CALIBRATE_SPEC)
assert CALIBRATE_SPEC.loader is not None
CALIBRATE_SPEC.loader.exec_module(edge_realizable_calibrate)

LEGACY_SPEC = importlib.util.spec_from_file_location(
    "train_edge_realizable_calibration", ROOT / "scripts" / "train_edge_realizable_calibration.py"
)
train_edge_realizable_calibration = importlib.util.module_from_spec(LEGACY_SPEC)
assert LEGACY_SPEC.loader is not None
LEGACY_SPEC.loader.exec_module(train_edge_realizable_calibration)


def _write_fact(conn: sqlite3.Connection, idx: int) -> None:
    fact_as_of = f"2026-05-05T12:{idx:02d}:00+00:00"
    row = {
        "outcome": "80F or higher",
        "q_entry": 0.70,
        "best_bid": 0.50,
        "best_ask": 0.52,
        "spread": 0.02,
        "ask_depth_topN": 100.0,
        "entry_size": 10.0,
        "exec_edge_entry": 0.18,
        "exec_entry_price": 0.52,
        "exec_exit_price": 0.56,
        "exec_edge_exit": 0.05,
        "q_entry_no": 0.30,
        "best_bid_no": 0.48,
        "best_ask_no": 0.50,
        "spread_no": 0.02,
        "ask_depth_topN_no": 100.0,
        "entry_size_no": 10.0,
        "exec_edge_entry_no": 0.10,
        "exec_entry_price_no": 0.50,
        "exec_exit_price_no": 0.53,
        "exec_edge_exit_no": 0.04,
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version,
            fact_json, created_at
        )
        VALUES ('2026-05-05', 'test-market', '2026-05-06', ?, 'test', ?, ?)
        """,
        (fact_as_of, json.dumps({"distribution": [row]}), fact_as_of),
    )


def test_auto_train_generates_candidate_without_touching_production(tmp_path: Path) -> None:
    work = tmp_path / "work"
    data_dir = work / "data"
    scripts_dir = work / "scripts"
    data_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    (scripts_dir / "lib").mkdir()

    db_path = data_dir / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec INTEGER,
            model_cache_age_sec INTEGER,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            fact_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        )
        """
    )
    for idx in range(3):
        _write_fact(conn, idx)
    for idx, bid in enumerate((0.50, 0.55, 0.59)):
        ts = f"2026-05-05T12:{idx:02d}:30+00:00"
        conn.execute(
            """
            INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
            VALUES ('test-market', '2026-05-06', '80F or higher', 'YES', ?, 0.60, ?)
            """,
            (bid, ts),
        )
        conn.execute(
            """
            INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
            VALUES ('test-market', '2026-05-06', '80F or higher', 'NO', ?, 0.52, ?)
            """,
            (0.54, ts),
        )
    conn.commit()
    conn.close()

    production = data_dir / "edge_realizable_calibration.json"
    production.write_text('{"version":"production"}\n', encoding="utf-8")
    before = production.read_text(encoding="utf-8")

    for rel in (
        "scripts/auto_train_edge_realizable.sh",
        "scripts/edge_realizable_calibrate.py",
        "scripts/lib/resolve_project_python.sh",
        "edge_realizable.py",
    ):
        src = ROOT / rel
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        if rel.endswith(".sh"):
            dst.chmod(0o755)

    out_path = tmp_path / "candidate.json"
    result = subprocess.run(
        [
            "bash",
            str(work / "scripts" / "auto_train_edge_realizable.sh"),
            "--since-days",
            "1",
            "--max-facts",
            "10",
            "--output",
            str(out_path),
        ],
        cwd=str(work),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert production.read_text(encoding="utf-8") == before
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["fill_mode"] == "ticks"
    assert len(payload["p_converge_buckets"]) > 0
    assert len(payload["p_fill_entry_buckets"]) > 0
    assert len(payload["p_fill_exit_buckets"]) > 0


def test_calibrate_default_output_is_candidate_path() -> None:
    default_out = str(
        ROOT / "data" / "edge_realizable_calibration.candidate.json"
    )
    source = (ROOT / "scripts" / "edge_realizable_calibrate.py").read_text(encoding="utf-8")
    assert f'"data" / "edge_realizable_calibration.candidate.json"' in source
    assert default_out.endswith("edge_realizable_calibration.candidate.json")


def test_tick_window_cache_reuses_bid_ask_lookup() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
        VALUES ('test-market', '2026-05-06', '80F or higher', 'YES', 0.57, 0.49, '2026-05-05T12:15:00+00:00')
        """
    )
    conn.commit()

    queries = []
    conn.set_trace_callback(
        lambda sql: queries.append(sql) if "event_market_ticks" in sql else None
    )
    cache = edge_realizable_calibrate._TickWindowCache(conn)
    t0 = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)

    hit_entry = edge_realizable_calibrate._touches_entry_limit(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.50,
        window_cache=cache,
    )
    hit_exit = edge_realizable_calibrate._touches_target(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.56,
        window_cache=cache,
    )

    assert hit_entry is True
    assert hit_exit is True
    assert len(queries) == 1


def test_tick_window_cache_preloads_market_window() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
        VALUES ('test-market', '2026-05-06', ?, ?, ?, ?, ?)
        """,
        [
            ("80F or higher", "YES", 0.57, 0.49, "2026-05-05T12:15:00+00:00"),
            ("80F or higher", "NO", 0.51, 0.44, "2026-05-05T12:15:00+00:00"),
        ],
    )
    conn.commit()

    queries = []
    conn.set_trace_callback(
        lambda sql: queries.append(sql) if "event_market_ticks" in sql else None
    )
    cache = edge_realizable_calibrate._TickWindowCache(conn)
    t0 = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)

    cache.preload_market("test-market", "2026-05-06", t0, t1)
    assert edge_realizable_calibrate._touches_target(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.56,
        window_cache=cache,
    ) is True
    assert edge_realizable_calibrate._touches_entry_limit(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "NO",
        t0,
        t1,
        0.45,
        window_cache=cache,
    ) is True

    assert len(queries) == 1


def test_tick_window_cache_preloads_windows_in_one_batch() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
        VALUES ('test-market', '2026-05-06', ?, ?, ?, ?, ?)
        """,
        [
            ("80F or higher", "YES", 0.40, 0.58, "2026-05-05T12:05:00+00:00"),
            ("80F or higher", "YES", 0.57, 0.49, "2026-05-05T12:15:00+00:00"),
            ("80F or higher", "NO", 0.51, 0.44, "2026-05-05T12:20:00+00:00"),
            ("80F or higher", "YES", 0.99, 0.01, "2026-05-05T14:00:00+00:00"),
        ],
    )
    conn.commit()

    queries = []
    conn.set_trace_callback(
        lambda sql: queries.append(sql) if "event_market_ticks" in sql else None
    )
    t0 = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    cache = edge_realizable_calibrate._TickWindowCache(conn)

    cache.preload_windows([("test-market", "2026-05-06", t0, t1)])
    assert edge_realizable_calibrate._touches_target(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.56,
        window_cache=cache,
    ) is True
    assert edge_realizable_calibrate._touches_entry_limit(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.02,
        window_cache=cache,
    ) is False

    assert len(queries) == 1
    assert "_edge_calib_tick_windows" in queries[0]
    assert "GROUP BY" in queries[0]


def test_tick_window_cache_preloads_windows_in_configured_batches() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
        VALUES ('test-market', '2026-05-06', '80F or higher', 'YES', ?, ?, ?)
        """,
        [
            (0.51, 0.49, "2026-05-05T12:15:00+00:00"),
            (0.61, 0.39, "2026-05-05T13:15:00+00:00"),
        ],
    )
    conn.commit()

    queries = []
    conn.set_trace_callback(
        lambda sql: queries.append(sql) if "event_market_ticks" in sql else None
    )
    cache = edge_realizable_calibrate._TickWindowCache(conn)
    t0 = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 5, 14, 0, tzinfo=timezone.utc)

    cache.preload_windows(
        [
            ("test-market", "2026-05-06", t0, t1),
            ("test-market", "2026-05-06", t1, t2),
        ],
        batch_size=1,
    )

    assert edge_realizable_calibrate._touches_target(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.50,
        window_cache=cache,
    ) is True
    assert edge_realizable_calibrate._touches_target(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t1,
        t2,
        0.60,
        window_cache=cache,
    ) is True

    assert len(queries) == 2


def test_tick_window_cache_marks_timed_out_batch_preloaded(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event_market_ticks (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            best_bid REAL,
            best_ask REAL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_market_ticks(slug, event_date, outcome, side, best_bid, best_ask, created_at)
        VALUES ('test-market', '2026-05-06', '80F or higher', 'YES', 0.51, 0.49, '2026-05-05T12:15:00+00:00')
        """
    )
    conn.commit()

    times = iter([100.0, 102.0, 102.0])
    monkeypatch.setattr(edge_realizable_calibrate.time, "monotonic", lambda: next(times, 101.0))

    cache = edge_realizable_calibrate._TickWindowCache(conn)
    t0 = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 5, 5, 13, 0, tzinfo=timezone.utc)
    cache.preload_windows(
        [("test-market", "2026-05-06", t0, t1)],
        batch_size=1,
        timeout_sec=1.0,
    )

    assert cache.stats["tick_preload_timeouts"] == 1
    assert cache.stats["tick_windows_skipped"] == 1
    assert edge_realizable_calibrate._touches_target(
        conn,
        "test-market",
        "2026-05-06",
        "80F or higher",
        "YES",
        t0,
        t1,
        0.50,
        window_cache=cache,
    ) is None


def test_legacy_training_does_not_fabricate_exit_bucket_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE edge_trades (
            slug TEXT,
            entry_at TEXT,
            state TEXT,
            entry_price REAL,
            position_size REAL,
            realized_pnl REAL,
            last_fill_at TEXT,
            updated_at TEXT,
            side TEXT,
            outcome TEXT,
            event_date TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            slug TEXT,
            event_date TEXT,
            details_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    calibration = train_edge_realizable_calibration.train_calibration(
        db_path=str(db_path),
        days=90,
        min_samples=20,
        verbose=False,
    )

    assert calibration["p_fill_exit_default"] is None
    assert calibration["p_fill_exit_buckets"] == {}
    assert calibration["legacy_na_reason"] == "insufficient_closed_trade_samples"
