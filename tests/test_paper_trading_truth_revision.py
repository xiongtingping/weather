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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ssot  # noqa: E402


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


# Five one-degree rungs, closed at both ends.  The seat takes the middle
# two.  The ends used to be open (``None``) and one test closed them with
# an UPDATE when it needed a value outside the book -- which the real
# schema forbids: ``basket_probability_bridge_bucket_no_update`` makes
# these rows append-only, and the fixture only got away with it while it
# hand-rolled a table without the trigger.  A ladder that is bounded from
# the start needs no mutation to have an outside.
_LADDER = [
    ("B0", 0, 19.0, 20.0),
    ("B1", 1, 20.0, 21.0),
    ("B2", 2, 21.0, 22.0),
    ("B3", 3, 22.0, 23.0),
    ("B4", 4, 23.0, 24.0),
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
    # The bridge tables come from the module that owns them, never a
    # hand-rolled subset.  This fixture used to declare five of the
    # eighteen columns, and the ledger's re-score reads a sixth: charter
    # bookkeeping added ``state`` to the history table and the copy here
    # did not follow, so every test in this file died on "no such column:
    # h.state" -- a stale fixture reading as a broken ledger.  Taking the
    # production DDL means the next column arrives for free, and the
    # append-only triggers and the CHECK constraints come with it.
    conn.executescript(ssot.PROBABILITY_BRIDGE_DDL)
    ledger.ensure_schema(conn)
    conn.execute(
        "INSERT INTO basket_probability_bridge_history ("
        " history_id, station_id, event_date, settlement_timezone, stage,"
        " checkpoint_id, decision_at_utc, fresh_until_utc,"
        " envelope_content_sha256, bridge_sha256, bucket_map_sha256,"
        " bucket_intervals_json, projection_sha256, envelope_json, state,"
        " created_at_utc, content_sha256"
        ") VALUES ('h1','KTST','2026-08-10','UTC','P0','chk-1',"
        " '2026-08-10T12:00:00Z','2026-08-10T18:00:00Z',"
        " ?,?,?,'[]',?,'{}','COMMITTED','2026-08-10T12:00:00Z',?)",
        ("e" * 64, "b" * 64, "m" * 64, "p" * 64, "c" * 64),
    )
    conn.executemany(
        "INSERT INTO basket_probability_bridge_bucket_probability ("
        " history_id, bucket, ordinal, lower_c, upper_c, probability,"
        " content_sha256) VALUES (?,?,?,?,?,?,?)",
        [("h1", b, o, lo, hi, 0.2, f"{o:064d}") for b, o, lo, hi in _LADDER],
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

        # Outside every rung.  The ladder is bounded 19-24 by construction
        # (see ``_LADDER``), so nothing here has to reach into a table the
        # production schema holds append-only.
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
    # Ordered by statement, not by substring: the assignment this used to
    # grep for grew a second target on 2026-09-11 (the drop now reports the
    # refusals it removed too) and the ordering rule went untested under a
    # passing name.
    import ast  # noqa: PLC0415

    backfill = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "backfill"
    )

    def _first_call(needle: str) -> int:
        for node in ast.walk(backfill):
            if isinstance(node, ast.Call) and needle in ast.unparse(node):
                return node.lineno
        raise AssertionError("not called in backfill: " + needle)

    walk = _first_call("_drop_superseded_positions(")
    rescore = _first_call("_rescore_settlements(")
    scopes = _first_call("SELECT DISTINCT station_id, event_date")
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


def test_the_live_books_ids_are_the_ones_this_ledger_writes() -> None:
    """Every settled row on disk carries the id the shape produces now.

    This is the check that would have caught the split, and it is the one
    that goes loud if a third shape ever appears.  What it supplies by
    hand is only the INPUTS the recorded row holds -- the chain's window
    ordered by the ladder's own ordinals, the chain stake, the chain's
    first decision -- while the shape itself comes from the ledger's
    function.  Eight rows failed this the day it was written; the
    re-score re-keys them on its next pass and they stay put after that.
    """

    ledger = _ledger()
    db = ROOT / "data" / "bayes_basket_target_ssot.db"
    if not db.is_file():
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30.0)
    try:
        rows = conn.execute(
            "SELECT s.settlement_id, s.station_id, s.event_date,"
            " s.settled_tmax_c, p.chain_shares_json, p.leg_shares_json,"
            " p.chain_stake_usdc, p.stake_usdc, p.stage, p.decision_at_utc,"
            " t.opened"
            " FROM paper_trade_settlement s"
            " JOIN paper_trade_position p ON p.position_id = s.position_id"
            " JOIN (SELECT station_id, event_date,"
            "         MIN(decision_at_utc) AS opened"
            "       FROM paper_trade_position GROUP BY 1, 2) t"
            "   ON t.station_id = s.station_id"
            "  AND t.event_date = s.event_date"
        ).fetchall()
    except sqlite3.DatabaseError:
        conn.close()
        return
    drifted = []
    for (sid, station, day, truth, chain_js, leg_js, chain_stake,
         stage_stake, stage, decided, opened) in rows:
        ladder = ledger._settlement_ladder(
            conn, str(station), str(day), str(stage), str(decided)
        )
        ordinals = {str(b): int(o) for b, o, _lo, _hi, _p in ladder}
        per_leg = json.loads(chain_js or leg_js or "{}") or {}
        if not per_leg:
            continue
        want = ledger._settlement_identity_id(
            station_id=str(station),
            event_date=str(day),
            chain_opened_at=str(opened),
            window=sorted(per_leg, key=lambda b: ordinals.get(b, 0)),
            stake_usdc=float(
                chain_stake if chain_stake is not None else stage_stake
            ),
            truth_c=truth,
        )
        if str(sid) != want:
            drifted.append(f"{station} {day}")
    conn.close()
    assert not drifted, (
        "%d settlements carry an id this ledger would not write: %s"
        % (len(drifted), ", ".join(drifted[:6]))
    )


def test_both_writers_build_the_settlement_id_the_same_way() -> None:
    """Two writers, one identity, and it is a function they both call.

    ``paper_trade_settlement`` is written from two places -- the entry walk
    when a day resolves, and the re-score when a truth revision moves it --
    and they hashed different dictionaries for three weeks.  The walk keyed
    the CHAIN (``chain_opened_at``, the accumulated window, no stage); the
    re-score keyed a STAGE (``stage`` and its own ``decision_at_utc``).  So
    a corrected settlement got an id the walk would never produce.

    This test used to try to catch that and could not: it rebuilt the
    re-score's dictionary by hand and compared it to
    ``paper_trade_position.content_sha256`` -- a THIRD hash, the economic
    decision id (``paper_trade_economic_decision_v2``), which has neither
    shape.  A hand-copied rule audits the copy.  Measured on the live book
    2026-09-10 the real split was 237 ids from the walk against 7 from the
    re-score, and this test was red on none of them and red on all 162
    positions instead.

    So the shape lives in one function and this asks the source whether
    both writers call it.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # Exactly one place decides the shape.
    assert source.count('"paper-set-" + content_sha256(') == 1
    assert source.count("def _settlement_identity_id(") == 1
    # And both writers reach it through that function.
    assert source.count("_settlement_identity_id(\n") == 3, (
        "expected the definition plus one call per writer"
    )
    # The stage is deliberately not part of it: a chain settles once.
    body = source.split("def _settlement_identity_id(")[1].split(
        "\ndef "
    )[0]
    assert '"chain_opened_at"' in body
    assert '"stage"' not in body


def test_a_rescore_writes_the_id_the_entry_walk_would_have_written(
    tmp_path,
) -> None:
    """The same chain, the same truth, the same id -- whoever wrote it.

    Seeds a settlement under the retired stage-keyed shape, lets the
    re-score run with the truth unchanged, and requires the row to come
    back under the chain-keyed id.  Before the fix the re-score left it
    alone (its truth was current) and the book kept an id no writer
    produces; now it is re-keyed once and is stable from then on.
    """

    ledger = _ledger()
    conn = _seed(tmp_path / "ledger.db", ledger)
    try:
        _append_truth(conn, 0, 22.4)
        ledger._rescore_settlements(conn, ledger._truth(conn))
        settled = _settlement(conn)
        expected = ledger._settlement_identity_id(
            station_id="KTST",
            event_date="2026-08-10",
            chain_opened_at="2026-08-10T12:00:00Z",
            window=_WINDOW,
            stake_usdc=_STAKE,
            truth_c=22.4,
        )
        assert settled["settlement_id"] == expected

        # Now put the retired shape back under it and re-score again.  The
        # truth has not moved, so the only thing that can bring the row
        # back is the identity check.
        stale = "paper-set-" + ledger.content_sha256({
            "station_id": "KTST", "event_date": "2026-08-10",
            "stage": "P0", "decision_at_utc": "2026-08-10T12:00:00Z",
            "window": list(_WINDOW), "stake_usdc": round(_STAKE, 6),
            "truth": 22.4,
        })[:24]
        assert stale != expected, "the two shapes must actually differ"
        conn.execute(
            "UPDATE paper_trade_settlement SET settlement_id=?"
            " WHERE settlement_id=?",
            (stale, expected),
        )
        conn.commit()
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_rekeyed") == 1
        assert _settlement(conn)["settlement_id"] == expected
        # And it is idempotent: a second pass finds nothing to do.
        counts = ledger._rescore_settlements(conn, ledger._truth(conn))
        assert counts.get("settlement_rekeyed") is None
        assert counts.get("settlement_current") == 1
    finally:
        conn.close()
