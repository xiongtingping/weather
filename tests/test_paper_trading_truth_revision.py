"""The paper ledger must follow a truth correction without a rebuild.

Settled truth is revisable by design.  ``calibration_station_daily_high_truth``
is append-only under ``revision_no``, held that way by the
``calibration_truth_no_update`` and ``calibration_truth_no_delete`` triggers,
so a corrected value can only ever arrive as a *new* revision beside the old
one.  On 2026-08-18 the sub-degree Fahrenheit re-derivation appended 192 of
them in one go -- 156 changing the value, 35 moving the winning bucket.

The ledger's incremental pass could not follow any of it.  A settlement was
written by the same branch of the entry walk that created the position, under
``INSERT OR IGNORE`` on ``UNIQUE(station_id,event_date,stage)``, and the walk
skips a stage it has already seated.  So a recorded settlement was frozen
against whatever truth stood when it was written, and the only thing that
deleted settlements keyed on the *selection rule*, which a truth correction
does not touch.  Only ``--rebuild`` -- which discards the whole derived book --
could move it, and ``--rebuild`` is not what the quarter-hourly sweep runs.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ledger():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "paper_trading_ledger_under_test",
        ROOT / "scripts" / "paper_trading_ledger.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# Five one-degree rungs.  The seat takes the middle two.
_LADDER = [
    ("B0", 0, None, 20.0),
    ("B1", 1, 20.0, 21.0),
    ("B2", 2, 21.0, 22.0),
    ("B3", 3, 22.0, 23.0),
    ("B4", 4, 23.0, None),
]
_WINDOW = ["B2", "B3"]
_STAKE = 12.0
_SHARES = 30.0
_LEG_SHARES = {"B2": 30.0, "B3": 30.0}


def _seed(path: Path, ledger) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth ("
        " truth_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " local_date TEXT NOT NULL, official_tmax_c REAL NOT NULL,"
        " revision_no INTEGER NOT NULL, content_sha256 TEXT NOT NULL,"
        " UNIQUE(station_id, local_date, revision_no))"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history ("
        " history_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL, stage TEXT NOT NULL,"
        " decision_at_utc TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_bucket_probability ("
        " history_id TEXT NOT NULL, bucket TEXT NOT NULL,"
        " ordinal INTEGER NOT NULL, lower_c REAL, upper_c REAL,"
        " probability REAL NOT NULL, PRIMARY KEY(history_id, bucket))"
    )
    ledger.ensure_schema(conn)
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES"
        " ('h1','KTST','2026-08-10','P0','2026-08-10T12:00:00Z')"
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_bucket_probability VALUES"
        " (?,?,?,?,?,?)",
        [("h1", b, o, lo, hi, 0.2) for b, o, lo, hi in _LADDER],
    )
    conn.execute(
        "INSERT INTO paper_trade_position ("
        " position_id, station_id, event_date, stage, decision_at_utc,"
        " quote_observed_at_utc, ladder_center_bucket, window_buckets_json,"
        " window_size, win_probability, all_in_cost, edge, curve_rank,"
        " kelly_fraction, stake_usdc, shares, probabilities_json,"
        " prices_json, center_c, refused_window_count, content_sha256,"
        " selection_rule_sha256, selection_rule_label, leg_shares_json,"
        " resting_fill_json, posted_buckets_json"
        ") VALUES ('pos-1','KTST','2026-08-10','P0',"
        " '2026-08-10T12:00:00Z','2026-08-10T11:59:00Z','B3',?,2,0.55,"
        " 0.40,0.15,1.0,0.05,?,?,'{}','{}',22.4,0,'sha-pos-1',"
        " ?,'label',?,'{}','[]')",
        (
            json.dumps(_WINDOW),
            _STAKE,
            _SHARES,
            ledger.SELECTION_RULE_SHA256,
            json.dumps(_LEG_SHARES, sort_keys=True),
        ),
    )
    conn.commit()
    return conn


def _append_truth(
    conn: sqlite3.Connection, revision: int, value: float
) -> None:
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES"
        " (?,'KTST','2026-08-10',?,?,?)",
        (
            "truth-%d" % revision,
            value,
            revision,
            ("%064d" % revision)[:64],
        ),
    )
    conn.commit()


def _settlement(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT settled_tmax_c, settled_bucket, hit, payout_usdc, pnl_usdc,"
        " bucket_distance, center_error_c, settled_at_utc,"
        " truth_revision_no, truth_content_sha256, settlement_id"
        " FROM paper_trade_settlement"
    ).fetchone()
    if row is None:
        return {}
    keys = (
        "settled_tmax_c", "settled_bucket", "hit", "payout_usdc", "pnl_usdc",
        "bucket_distance", "center_error_c", "settled_at_utc",
        "truth_revision_no", "truth_content_sha256", "settlement_id",
    )
    return dict(zip(keys, row))


def test_a_corrected_truth_rescores_the_settlement(tmp_path) -> None:
    """The revision that moves the winning bucket must move the score.

    35 of 2026-08-18's revisions did exactly this, and every one of them was
    a settlement still claiming the bucket the old value had landed in.
    """

    ledger = _ledger()
    conn = _seed(tmp_path / "ledger.db", ledger)
    try:
        # The seat opens before the day resolves.  Nothing to score yet, and
        # the entry walk that created it has no reason to look again.
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("open_unsettled") == 1
        assert _settlement(conn) == {}

        # Truth lands inside the seated window.  This alone is a fix: before
        # it, a seat opened ahead of its truth stayed open until a rebuild.
        _append_truth(conn, 0, 22.4)
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_settled_late") == 1
        first = _settlement(conn)
        assert first["settled_bucket"] == "B3"
        assert first["hit"] == 1
        assert first["payout_usdc"] == _LEG_SHARES["B3"]
        assert first["pnl_usdc"] == _LEG_SHARES["B3"] - _STAKE
        assert first["bucket_distance"] == 0
        assert first["truth_revision_no"] == 0

        # A correction two rungs down.  The seat is untouched -- it was chosen
        # before the day resolved -- but everything the value decides moves.
        _append_truth(conn, 1, 20.4)
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_rescored") == 1
        second = _settlement(conn)
        assert second["settled_tmax_c"] == 20.4
        assert second["settled_bucket"] == "B1"
        assert second["hit"] == 0
        assert second["payout_usdc"] == 0.0
        assert second["pnl_usdc"] == -_STAKE
        assert second["bucket_distance"] == 1
        assert second["center_error_c"] == 22.4 - 20.4
        # The id hashes the truth, so a re-scored row is a different row.
        assert second["settlement_id"] != first["settlement_id"]
        # And exactly one settlement survives the swap: the delete-then-insert
        # has to respect ``UNIQUE(station_id,event_date,stage)``.
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_trade_settlement"
        ).fetchone()[0] == 1

        # Idempotent: the binding now stands, so a second pass does nothing.
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_current") == 1
        assert _settlement(conn) == second
    finally:
        conn.close()


def test_a_restating_revision_rebinds_without_moving_the_page(
    tmp_path,
) -> None:
    """156 of the 192 changed the value.  The rest restated it.

    A revision that lands on the same value cannot change anything derived
    from it, so it must not rewrite the row -- otherwise every restatement
    churns ``settled_at_utc`` and the page reports motion that did not happen.
    But the binding still has to advance, or the pass re-derives the same
    answer every quarter hour forever.
    """

    ledger = _ledger()
    conn = _seed(tmp_path / "ledger.db", ledger)
    try:
        _append_truth(conn, 0, 22.4)
        ledger._rescore_settlements(conn, ledger._truth(conn))
        before = _settlement(conn)

        _append_truth(conn, 1, 22.4)
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_rebound") == 1
        after = _settlement(conn)
        assert after["truth_revision_no"] == 1
        assert after["truth_content_sha256"] != before["truth_content_sha256"]
        # Nothing the page reads may have moved.
        assert after["settled_at_utc"] == before["settled_at_utc"]
        assert after["settlement_id"] == before["settlement_id"]
        assert after["pnl_usdc"] == before["pnl_usdc"]
        assert after["hit"] == before["hit"]

        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_current") == 1
    finally:
        conn.close()


def test_a_correction_off_the_ladder_retires_the_settlement(tmp_path) -> None:
    """A value the seat was never priced against scores nothing.

    The entry walk records no settlement when truth falls outside the ladder,
    so a re-score cannot leave one standing either -- a row scored against a
    value that is no longer the truth must not survive as if it were.
    """

    ledger = _ledger()
    conn = _seed(tmp_path / "ledger.db", ledger)
    try:
        _append_truth(conn, 0, 22.4)
        ledger._rescore_settlements(conn, ledger._truth(conn))
        assert _settlement(conn)["hit"] == 1

        # Outside every rung: the open-ended ends of the ladder are the only
        # reason this needs an explicit sentinel value at all.
        conn.execute(
            "UPDATE basket_probability_bridge_bucket_probability"
            " SET lower_c=20.0 WHERE bucket='B0'"
        )
        conn.execute(
            "UPDATE basket_probability_bridge_bucket_probability"
            " SET upper_c=24.0 WHERE bucket='B4'"
        )
        conn.commit()
        _append_truth(conn, 1, 31.0)
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_dropped") == 1
        assert counts.get("truth_outside_book") == 1
        assert _settlement(conn) == {}
    finally:
        conn.close()


def test_the_incremental_pass_is_the_one_that_corrects(tmp_path) -> None:
    """``--backfill`` is what runs every quarter hour; ``--rebuild`` is not.

    The launchd job ``com.weather.paper_trading_ledger_sweep`` runs
    ``--backfill`` and the sweep script says in as many words that
    ``--rebuild`` is never used there.  So the correction has to live on the
    incremental path, ahead of the entry walk -- the equity curve reads each
    day's realised P&L back out of the committed settlements, and sizing the
    following days off superseded numbers is the failure this is fixing.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    walk = source.index("superseded = _drop_superseded_positions")
    rescore = source.index("rescored = _rescore_settlements(conn, truth)")
    scopes = source.index("scopes = conn.execute(")
    assert walk < rescore < scopes

    sweep = (ROOT / "scripts" / "paper_trading_ledger_sweep.sh").read_text(
        encoding="utf-8"
    )
    assert "--backfill" in sweep
    assert "--rebuild" not in sweep.split("set -euo pipefail")[1]

    # And it is deliberately unbounded, unlike the rule-change check beside
    # it: a correction that reaches back a month has to reach that month's
    # rows, and re-scoring costs one indexed ladder lookup rather than a
    # replay of the day's whole decision history.
    assert "def _rescore_settlements(\n    conn: sqlite3.Connection," \
        " truth: dict[tuple[str, str], _Truth]\n) -> dict[str, int]:" in source


def test_recorded_settlements_say_which_truth_they_read() -> None:
    """The invariant on the live book: every score names its revision.

    An unbound settlement is one that cannot know it has gone stale, which is
    the entire defect.  Checked against what is actually recorded rather than
    against the source, because the migration that binds the rows written
    before the column existed is itself part of the fix.
    """

    db = ROOT / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(paper_trade_settlement)")
        }
        if "truth_content_sha256" not in columns:
            return
        unbound = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_settlement"
            " WHERE truth_content_sha256='' OR truth_content_sha256 IS NULL"
        ).fetchone()[0]
        assert unbound == 0, "%d settlements name no truth revision" % unbound
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()


def test_a_rescore_rebuilds_the_id_the_entry_walk_would_have_written() -> None:
    """Both paths must agree on identity, or a re-score forks the row.

    ``_rescore_settlements`` reconstructs the seat's identity from the
    recorded columns; the entry walk builds it from the seat it just chose and
    stores its hash in ``paper_trade_position.content_sha256``.  If those ever
    drift, a corrected settlement gets an id the walk would never produce and
    the two paths stop being the same ledger.
    """

    ledger = _ledger()
    db = ROOT / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT station_id, event_date, stage, decision_at_utc,"
            " window_buckets_json, stake_usdc, content_sha256"
            " FROM paper_trade_position"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    finally:
        conn.close()
    for station, day, stage, decided, window_json, stake, recorded in rows:
        rebuilt = ledger.content_sha256({
            "station_id": str(station),
            "event_date": str(day),
            "stage": str(stage),
            "decision_at_utc": str(decided),
            "window": list(json.loads(window_json)),
            "stake_usdc": round(float(stake), 6),
        })
        assert rebuilt == str(recorded), (
            "identity drift on %s %s %s" % (station, day, stage)
        )
