"""A refusal is an answer, so it belongs to the rule set that gave it.

``_scope_answer_is_final`` lets a recorded refusal stand in for a walk on a
settled station-day.  The table carried no rule fingerprint until
2026-09-11, so an arm run against a database another arm had already walked
inherited that arm's refusals and never solved those scopes: the second arm
of a shared-database pair answered 783 scopes out of the first arm's table.
Two digests, two names, one strategy -- the same shape as the charter-72
switch that rehashed a book and judged it under the old law anyway.

Every fixture here builds its table from the shipped DDL rather than a hand
-written one, because a hand-written schema that drifts is how a migration
test comes to pass against a shape nothing serves.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as ledger  # noqa: E402

OTHER_SHA = "0" * 64
_REFUSAL_COLUMNS = (
    "station_id, event_date, stage, reason, detail,"
    " cycles_examined, last_decision_at_utc, recorded_at_utc,"
    " selection_rule_sha256"
)


def _refusal(
    conn: sqlite3.Connection,
    *,
    station: str,
    date: str,
    stage: str,
    sha: str,
    reason: str = "no_positive_kelly",
) -> None:
    conn.execute(
        f"INSERT INTO paper_trade_refusal ({_REFUSAL_COLUMNS})"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (station, date, stage, reason, "fixture", 1,
         f"{date}T12:00:00Z", f"{date}T23:00:00Z", sha),
    )


def test_the_shipped_table_carries_the_rule_that_answered() -> None:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_refusal)")
    }
    assert "selection_rule_sha256" in columns


def test_only_this_rule_sets_refusal_counts_as_this_rule_sets_answer() -> None:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    _refusal(conn, station="KMIA", date="2026-09-01", stage="P0",
             sha=ledger.SELECTION_RULE_SHA256)
    _refusal(conn, station="KLGA", date="2026-09-01", stage="P0",
             sha=OTHER_SHA)
    _refusal(conn, station="KSFO", date="2026-09-01", stage="P0", sha="")
    conn.commit()

    mine = ledger._recorded_refusal_stages(
        conn, station_id="KMIA", event_date="2026-09-01"
    )
    theirs = ledger._recorded_refusal_stages(
        conn, station_id="KLGA", event_date="2026-09-01"
    )
    unstamped = ledger._recorded_refusal_stages(
        conn, station_id="KSFO", event_date="2026-09-01"
    )
    assert mine == {"P0"}
    # The two scopes below are what the arm skipped.  A refusal that is not
    # this rule set's is not an answer, so the walk must re-derive them.
    assert theirs == set()
    assert unstamped == set()


def test_another_rule_sets_refusal_does_not_settle_the_scope() -> None:
    """The consequence, at the function that reads it."""

    present = {"P0", "P1"}
    assert ledger._scope_answer_is_final(
        present, set(), {"P0", "P1"}, settled=True
    ) is True
    # Same day, same stages, but the refusals belonged to another arm and
    # therefore did not reach ``refused`` at all.
    assert ledger._scope_answer_is_final(
        present, set(), set(), settled=True
    ) is False


def test_the_superseded_drop_reaches_refusals_inside_the_window() -> None:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    # A date floor the helper can compute: it reads the bridge history.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_probability_bridge_history ("
        " station_id TEXT, event_date TEXT, stage TEXT, state TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES"
        " ('KMIA','2026-09-10','P0','COMMITTED')"
    )
    _refusal(conn, station="KMIA", date="2026-09-09", stage="P0",
             sha=OTHER_SHA)
    _refusal(conn, station="KLGA", date="2026-09-09", stage="P0",
             sha=ledger.SELECTION_RULE_SHA256)
    conn.commit()

    dropped, dropped_refusals = ledger._drop_superseded_positions(
        conn, recent_days=None
    )
    assert dropped == 0
    assert dropped_refusals == 1
    survivors = conn.execute(
        "SELECT station_id, selection_rule_sha256"
        " FROM paper_trade_refusal"
    ).fetchall()
    assert survivors == [("KLGA", ledger.SELECTION_RULE_SHA256)]


def test_a_table_written_before_the_column_is_widened_not_dropped() -> None:
    """The migration path: the shipped DDL minus this one column."""

    conn = sqlite3.connect(":memory:")
    old_ddl = re.sub(
        r"\n *-- Which rule set answered.*?\n *selection_rule_sha256[^\n]*\n",
        "\n",
        ledger._REFUSAL_DDL,
        flags=re.S,
    )
    assert "selection_rule_sha256" not in old_ddl
    conn.execute(old_ddl)
    conn.execute(
        "INSERT INTO paper_trade_refusal ("
        " station_id, event_date, stage, reason, detail,"
        " cycles_examined, last_decision_at_utc, recorded_at_utc"
        ") VALUES ('KMIA','2026-08-01','P0','no_positive_kelly','old',"
        " 3,'2026-08-01T12:00:00Z','2026-08-01T23:00:00Z')"
    )
    conn.commit()

    ledger.ensure_schema(conn)
    rows = conn.execute(
        "SELECT station_id, selection_rule_sha256 FROM paper_trade_refusal"
    ).fetchall()
    # Kept, and honestly unstamped -- not back-dated into this rule set.
    assert rows == [("KMIA", "")]


def test_every_appended_refusal_is_as_wide_as_the_statement() -> None:
    """The two append sites and the write agree, read off the AST.

    Counting question marks in the source text found the migration's INSERT
    instead of the walk's and asserted 211 == 9.  The arity is a property
    of the code, so take it from the code: each ``pending_refusals.append``
    tuple, against the placeholders of the statement that writes that list.
    """

    import ast  # noqa: PLC0415

    tree = ast.parse(
        (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
            encoding="utf-8"
        )
    )
    appended: list[int] = []
    placeholders: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pending_refusals"
            and node.args
            and isinstance(node.args[0], ast.Tuple)
        ):
            appended.append(len(node.args[0].elts))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "executemany"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == "pending_refusals"
        ):
            statement = ast.literal_eval(node.args[0]) if isinstance(
                node.args[0], ast.Constant
            ) else "".join(
                ast.literal_eval(part)
                for part in ast.walk(node.args[0])
                if isinstance(part, ast.Constant)
                and isinstance(part.value, str)
            )
            values = statement[statement.index("VALUES ("):]
            placeholders.append(values[:values.index(")")].count("?"))

    assert len(appended) == 2, appended
    assert len(placeholders) == 1, placeholders
    assert set(appended) == {placeholders[0]}
    # And that width is the one the shipped table has.
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    assert placeholders[0] == len(
        list(conn.execute("PRAGMA table_info(paper_trade_refusal)"))
    )


def test_the_reset_drops_every_table_this_script_owns() -> None:
    conn = sqlite3.connect(":memory:")
    ledger.ensure_schema(conn)
    present = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name LIKE 'paper_trade_%'"
        )
    }
    # Whatever the schema creates is what the reset must be able to clear,
    # or an arm inherits the table nobody listed.
    assert present == set(ledger.PAPER_TRADE_TABLES), present
    assert ledger.reset_paper_tables(conn) == len(ledger.PAPER_TRADE_TABLES)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name LIKE 'paper_trade_%'"
    ).fetchone()[0] == 0
    # Idempotent: nothing left to drop is not an error.
    assert ledger.reset_paper_tables(conn) == 0


def test_the_reset_refuses_the_production_ssot() -> None:
    """The standing book lives in those tables; every ROI figure reads them."""

    import subprocess  # noqa: PLC0415

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "paper_trading_ledger.py"),
         "--db", str(ledger.DEFAULT_DB), "--reset-paper-tables", "--backfill"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "refuses the production SSOT" in (
        result.stderr + result.stdout
    )


def test_every_arm_runner_starts_its_walk_from_nothing() -> None:
    """Both runners that drive the ledger for arms pass the reset."""

    import importlib  # noqa: PLC0415

    slices = importlib.import_module("run_ledger_arm_slices")
    first = slices.ledger_command(
        Path("/tmp/replay.db"), bound="2026-08-30", first_slice=True
    )
    later = slices.ledger_command(
        Path("/tmp/replay.db"), bound="2026-09-03", first_slice=False
    )
    assert "--reset-paper-tables" in first
    # And only the first: a later slice must inherit this arm's own earlier
    # days, because every seat is sized off the equity they left behind.
    assert "--reset-paper-tables" not in later

    # The execution sweep rebuilds in place, so its reset is in the command
    # it spawns; read it off the source of that one function.
    import ast  # noqa: PLC0415

    sweep = (ROOT / "scripts" / "run_execution_arm_sweep.py").read_text(
        encoding="utf-8"
    )
    rebuild = next(
        node for node in ast.parse(sweep).body
        if isinstance(node, ast.FunctionDef) and node.name == "_rebuild"
    )
    assert "--reset-paper-tables" in ast.unparse(rebuild)


def test_the_report_reads_the_book_without_the_write_lock() -> None:
    """``--report`` exists to be usable while a sweep holds the lock.

    It called ``ensure_schema`` so that a database without the tables would
    not raise, and that function writes -- DDL plus the label backfill -- so
    the read-only path died on "attempt to write a readonly database".  The
    path that exists to avoid the lock cannot be the path that needs it.
    """

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "book.db"
        writable = sqlite3.connect(str(path))
        ledger.ensure_schema(writable)
        writable.commit()
        writable.close()

        readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        readonly.execute("PRAGMA query_only=ON")
        # Raises if anything on this path writes.
        assert ledger.report(readonly)["schema"]

    # And a database with none of the tables answers rather than raising,
    # which is what the ensure_schema call was there for.
    empty = sqlite3.connect(":memory:")
    assert ledger.report(empty) == {
        "schema": "paper_trading_report_v1",
        "settled_count": 0, "open_positions": 0, "stages": {},
    }


def test_the_report_says_which_rule_sets_the_book_is_made_of() -> None:
    """A reader who sees one return reads one strategy.

    The superseded sweep is bounded by the window a pass can re-derive, so
    after a rule change the standing book holds two rule sets at once: the
    scheduled pass re-derives four days and everything older keeps the
    digest it was seated under.  Three knobs moved on 2026-09-12 and the
    book became 224 settled chains of one strategy and 83 of another,
    published as a single -28.3% while the served rule set alone read
    -34.5%.
    """

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "book.db"
        conn = sqlite3.connect(str(path))
        ledger.ensure_schema(conn)
        columns = [
            row[1] for row in conn.execute(
                "PRAGMA table_info(paper_trade_position)"
            )
        ]

        def _seat(
            pid: str, sha: str, stake: float, pnl: float, day: str
        ) -> None:
            # One settlement per station-day-stage is the table's own key,
            # so the two rule sets sit on different days.
            values = {
                "position_id": pid, "station_id": "KMIA",
                "event_date": day, "stage": "P0",
                "decision_at_utc": day + "T00:00:00Z",
                "quote_observed_at_utc": day + "T00:00:00Z",
                "ladder_center_bucket": "30C",
                "window_buckets_json": '["30C"]', "window_size": 1,
                "win_probability": 0.5, "all_in_cost": 0.5, "edge": 0.0,
                "kelly_fraction": 0.25, "stake_usdc": stake, "shares": 1.0,
                "probabilities_json": "{}", "prices_json": "{}",
                "refused_window_count": 0, "content_sha256": pid,
                "selection_rule_sha256": sha,
            }
            present = {k: v for k, v in values.items() if k in columns}
            conn.execute(
                "INSERT INTO paper_trade_position (%s) VALUES (%s)"
                % (",".join(present), ",".join("?" * len(present))),
                tuple(present.values()),
            )
            conn.execute(
                "INSERT INTO paper_trade_settlement ("
                " settlement_id, position_id, station_id, event_date, stage,"
                " settled_tmax_c, settled_bucket, hit, stake_usdc,"
                " payout_usdc, pnl_usdc, claimed_win_probability,"
                " settled_at_utc, truth_content_sha256"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("s_" + pid, pid, "KMIA", day, "P0", 30.0, "30C",
                 1 if pnl > 0 else 0, stake, stake + pnl, pnl, 0.5,
                 day + "T23:00:00Z", ""),
            )

        _seat("old", "0" * 64, 100.0, -40.0, "2026-09-01")
        _seat("new", ledger.SELECTION_RULE_SHA256, 100.0, -10.0, "2026-09-02")
        conn.commit()

        rule_sets = ledger.report(conn)["rule_sets"]
        assert rule_sets["single_rule_set"] is False
        assert rule_sets["served"] == ledger.SELECTION_RULE_SHA256[:12]
        served = rule_sets["served_only"]
        # The served rule set's own return, which is not the headline.
        assert served["settled_chains"] == 1
        assert served["return_on_stake"] == -0.1

        # And a book of one rule set says so.
        conn.execute("DELETE FROM paper_trade_settlement WHERE position_id='old'")
        conn.execute("DELETE FROM paper_trade_position WHERE position_id='old'")
        conn.commit()
        assert ledger.report(conn)["rule_sets"]["single_rule_set"] is True
