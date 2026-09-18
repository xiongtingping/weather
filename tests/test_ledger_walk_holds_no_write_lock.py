"""The ledger's walk holds no write lock (measured 2026-09-18).

The capture lanes commit beside the paper ledger by design
(``WRITER_TURN_EXEMPT_SWEEPS``), on the premise that its writes come in one
short burst at the end of the walk.  Two DELETEs before the walk committed
only when they had removed a row, and a DELETE that matches nothing still
opens a write transaction and takes the file's lock -- so a steady-state pass
held the SSOT write lock for its whole 240s lease, and
capture_noaa_conus_forward's commit waited out its 120s busy_timeout behind it
and ended ``write_blocked``.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402


def _lock_is_free(path: Path) -> bool:
    other = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def _book(path: Path, factory=sqlite3.Connection) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), factory=factory)
    conn.execute("PRAGMA journal_mode=WAL")
    ledger.ensure_schema(conn)
    conn.commit()
    return conn


def test_the_pre_walk_deletes_release_the_lock_when_they_match_nothing(tmp_path) -> None:
    path = tmp_path / "book.db"
    conn = _book(path)

    assert ledger._purge_below_floor(conn) == 0
    assert not conn.in_transaction
    assert _lock_is_free(path)

    assert ledger._drop_superseded_positions(conn, recent_days=None) == (0, 0)
    assert not conn.in_transaction
    assert _lock_is_free(path)


def test_another_writer_can_commit_while_the_walk_runs(tmp_path, monkeypatch) -> None:
    """Probed at the walk's first per-scope read, with every step before it
    real except the ones that read other lanes' tables."""

    path = tmp_path / "book.db"
    seen: list[bool] = []

    class Spy(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("SELECT stage FROM paper_trade_position") and not seen:
                seen.append(_lock_is_free(path))
            return super().execute(sql, *args)

    conn = _book(path, factory=Spy)
    day = (
        date.fromisoformat(ledger.LEDGER_EVENT_DATE_FLOOR) + timedelta(days=1)
    ).isoformat()
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES ('KMIA',?,'P0','COMMITTED')",
        (day,),
    )
    conn.execute(
        "CREATE TABLE calibration_station_market_timezone_binding("
        " station_id TEXT, settlement_timezone TEXT)"
    )
    # Seated under the served rule set, so the scope answers "already
    # recorded" right after the probed read and the walk goes no deeper.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(paper_trade_position)")
    }
    values = {
        "position_id": "p0", "station_id": "KMIA", "event_date": day,
        "stage": "P0", "decision_at_utc": f"{day}T00:00:00Z",
        "quote_observed_at_utc": f"{day}T00:00:00Z",
        "ladder_center_bucket": "30C", "window_buckets_json": '["30C"]',
        "window_size": 1, "win_probability": 0.5, "all_in_cost": 0.5,
        "edge": 0.0, "kelly_fraction": 0.25, "stake_usdc": 1.0,
        "shares": 1.0, "probabilities_json": "{}", "prices_json": "{}",
        "refused_window_count": 0, "content_sha256": "c0",
        "selection_rule_sha256": ledger.SELECTION_RULE_SHA256,
    }
    present = {k: v for k, v in values.items() if k in columns}
    conn.execute(
        "INSERT INTO paper_trade_position (%s) VALUES (%s)"
        % (",".join(present), ",".join("?" * len(present))),
        tuple(present.values()),
    )
    conn.commit()
    monkeypatch.setattr(ledger, "_truth", lambda c: {})
    monkeypatch.setattr(ledger, "_station_peak_cutoffs", lambda tz: {})
    monkeypatch.setattr(ledger, "_opening_equity", lambda c, *, earliest: 1000.0)

    counts = ledger.backfill(conn, recent_days=None, budget_sec=60.0)

    assert counts.get("already_recorded") == 1, counts
    assert seen == [True], counts
