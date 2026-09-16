"""A pass that holds the exclusive SSOT writer must be bounded end to end.

``maker_shadow_probe`` is declared ``("sliced", WRITER_LEASE_SEC)`` in the
resident's lease contract: it reads the 240-second lease, stops between
scopes, and asks for the next pass with the continuation marker.  Its three
loops honour that.  Everything around them did not.

On 2026-09-16T04:13Z the pass was ``timeout_killed`` at its 900s subprocess
timeout, 660 seconds past the lease, holding the writer -- which is
production not starting -- for all of it.  Measured that morning against the
live book, warm:

    _latest_books        100.1s   445,097 rows scanned to return 331
    claim_cap_reading     47.4s   two self-joins over 4,717,228 orders
    place's throttle read 16.8s   placed_at_utc >= ? over the same table

None of those three is inside the budget.  The first and third run before
their loop's first deadline check, and the reading ran after all three loops
had already stopped for time.

Two of them are fixed and this file holds both fixes.  The third is left,
and is named here so that is a decision rather than an oversight.

The scan is the ninth and tenth instance of the bug class
``test_target_quiescence_guard_is_indexed`` was written for: an unindexed
predicate over a growing append-only ledger, read on a path that holds up a
decision.  Asserted on the planner's answer, never on a clock -- a timing
test over an empty in-memory table passes while production scans 50GB.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402

PROBE = ROOT / "scripts" / "maker_shadow_probe.py"

#: The freshest committed book per scope, exactly as ``_latest_books`` asks.
LATEST_BOOKS_SQL = (
    "SELECT station_id, event_date, quote_observed_at, buckets_json"
    " FROM basket_official_ws_snapshot"
    " WHERE state='COMMITTED' AND quote_observed_at >= ?"
    " ORDER BY quote_observed_at"
)

#: Scans this pass still pays, by the statement that pays them.  A scan not
#: listed here fails the sweep below; one listed here has been priced.
DECLARED_REMAINING_SCANS = {
    # 16.8s over 4.7M orders, before the place loop's first deadline check.
    # An index leading on placed_at_utc would seek it, and costs about
    # 140MB on a database roughly 0.7GiB under its own ceiling -- so it is
    # deferred rather than taken, and it fits inside the 240s lease once
    # the 100s scan above is gone.
    "SELECT station_id, event_date, MAX(placed_at_utc)",
    # 3,923 rows, correlated max-revision subquery; milliseconds.
    "SELECT station_id, local_date, official_tmax_c",
    # The claim_cap reading, 21.0s + 26.4s over 4.7M orders.  Left as scans
    # deliberately: it is a once-a-day line, not work, and the pass now
    # reserves room for it instead of indexing the five-column pair key --
    # which would be a second index on the widest table here, bought for a
    # statement that may run once a day.
    "SELECT COUNT(*), SUM(CASE WHEN cap.limit_price",
    "SELECT COUNT(*), AVG(scap.pnl_per_share)",
}


def _probe_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_probe_under_test", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Production schema PLUS the probe's own tables.

    Without the second half, ``maker_shadow_order`` does not exist and the
    sweep below skips every statement that touches it -- which is four of
    them, including the two self-joins this file was opened for.  A guard
    that skips its own subject is worse than no guard.
    """

    connection = sqlite3.connect(":memory:")
    apply_production_ssot_schema(connection)
    _probe_module().ensure_schema(connection)
    return connection


def _plan(connection: sqlite3.Connection, sql: str, args: tuple) -> list[str]:
    return [
        str(row[3])
        for row in connection.execute("EXPLAIN QUERY PLAN " + sql, args)
    ]


def _probe_selects() -> list[tuple[str, int, str]]:
    """Every literal SELECT the probe executes, with where it is written.

    Taken off the AST rather than by regex over the source: several of these
    statements are built from adjacent string literals inside a call, and a
    pattern anchored on ``execute("SELECT`` reads none of them.
    """

    found: list[tuple[str, int, str]] = []
    tree = ast.parse(PROBE.read_text(encoding="utf-8"))

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.fn = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef):  # noqa: N802
            previous, self.fn = self.fn, node.name
            self.generic_visit(node)
            self.fn = previous

        def visit_Call(self, node: ast.Call):  # noqa: N802
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"execute", "executemany"}
                and node.args
            ):
                try:
                    sql = ast.literal_eval(node.args[0])
                except Exception:  # noqa: BLE001 -- a built string, not ours
                    sql = None
                if isinstance(sql, str) and re.match(r"\s*SELECT", sql, re.I):
                    found.append((self.fn, node.lineno, " ".join(sql.split())))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def test_the_freshest_book_read_is_a_seek_not_a_scan(conn) -> None:
    """The 100-second statement, by the planner's own answer.

    445,097 rows to return 331, and half the cost is the column order:
    ``state`` is declared after ``buckets_json`` and ``cash_phase_input_json``
    (7.99 GB between them), so a scan that needs it follows their overflow
    pages.  The same scan reading only ``quote_observed_at`` took 51.4s.
    Leading the index on that column alone seeks the range and reads
    ``state`` from the few hundred rows that survive.
    """

    steps = _plan(conn, LATEST_BOOKS_SQL, ("",))
    assert steps, "the planner returned no steps"
    assert any(
        "basket_official_ws_snapshot_quote_observed_idx" in step
        for step in steps
    ), steps
    assert not any(step.startswith("SCAN") for step in steps), steps


def test_the_index_is_declared_where_a_migration_will_carry_it(conn) -> None:
    """Declared in ssot.py is not enough; a RENAME must carry it too.

    The legacy upgrades rename this table to an archive, and a RENAME takes
    its indexes with it under their own names -- so an index left out of
    those inventories survives on the archive and makes the
    ``CREATE INDEX IF NOT EXISTS`` in the DDL fail on a name that exists.
    ``day_expiry_idx`` was already in that position when it was added.
    """

    source = (ROOT / "bayes_basket_target" / "ssot.py").read_text(
        encoding="utf-8"
    )
    name = "basket_official_ws_snapshot_quote_observed_idx"
    # Once in the DDL, and once in each of the two migration inventories.
    assert source.count(name) >= 3, source.count(name)
    live = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='basket_official_ws_snapshot'"
        )
    }
    assert name in live, sorted(live)


@pytest.mark.parametrize(
    "where,lineno,sql",
    [pytest.param(*item, id=f"{item[0]}:L{item[1]}") for item in _probe_selects()],
)
def test_no_statement_in_the_pass_scans_a_base_table(
    conn, where: str, lineno: int, sql: str
) -> None:
    """Every SELECT the probe runs, not only the two that were caught.

    A statement added later inherits the shape that caused this -- an
    unindexed predicate over a ledger that only grows -- and this pass holds
    the exclusive writer while it runs, so the cost lands on production
    rather than on the probe.
    """

    if any(sql.startswith(declared) for declared in DECLARED_REMAINING_SCANS):
        pytest.skip(f"declared and priced: {sql[:60]}")
    try:
        steps = _plan(conn, sql, tuple([""] * sql.count("?")))
    except sqlite3.Error as exc:  # a table this schema does not create
        pytest.skip(f"not on the production schema: {exc}")
    scans = [
        step for step in steps
        if step.startswith("SCAN") and "USING COVERING INDEX" not in step
    ]
    assert not scans, f"{where}:L{lineno} {sql[:90]}\n  {steps}"


def test_the_expensive_reading_is_behind_the_pass_clock(tmp_path, monkeypatch) -> None:
    """A reading is not work, so a pass out of clock does not pay for it.

    ``claim_cap_reading`` is two self-joins over 4.7M orders -- 47.4s
    measured -- and it ran after the three loops had already stopped for
    time, outside the lease entirely.  Behavioural rather than structural:
    what matters is that a spent clock does not reach it, not where the
    branch is written.
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location("_probe_under_test", PROBE)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    database = tmp_path / "ssot.db"
    setup = sqlite3.connect(database)
    apply_production_ssot_schema(setup)
    probe.ensure_schema(setup)
    setup.commit()
    setup.close()

    called: list[str] = []
    monkeypatch.setattr(
        probe, "claim_cap_reading",
        lambda conn: called.append("read") or "  claim_cap: ...",
    )
    monkeypatch.setattr(sys, "argv", ["probe", "--db", str(database)])

    # A pass with clock reads it.
    monkeypatch.setattr(probe, "PASS_BUDGET_SEC", 600.0)
    assert probe.main() == 0
    assert called == ["read"], called

    # A pass whose clock is already spent does not.
    called.clear()
    monkeypatch.setattr(probe, "PASS_BUDGET_SEC", -1.0)
    assert probe.main() == 0
    assert called == [], called


def test_a_pass_with_a_sliver_of_clock_does_not_start_the_reading(
    tmp_path, monkeypatch
) -> None:
    """"Not spent" was the wrong test, and it is the same bug one level up.

    The reading is one uninterruptible statement pair.  A pass holding a
    second of budget passes ``_spent`` and then runs for forty-seven --
    exactly the "a gate between units cannot bound one unit" shape that got
    this pass killed.  It has to reserve room, not check for a pulse.
    """

    probe = _probe_module()
    database = tmp_path / "ssot.db"
    setup = sqlite3.connect(database)
    apply_production_ssot_schema(setup)
    probe.ensure_schema(setup)
    setup.commit()
    setup.close()

    called: list[str] = []
    monkeypatch.setattr(
        probe, "claim_cap_reading",
        lambda conn: called.append("read") or "  claim_cap: ...",
    )
    monkeypatch.setattr(sys, "argv", ["probe", "--db", str(database)])
    # Alive, but with far less clock than the reading needs.
    monkeypatch.setattr(
        probe, "PASS_BUDGET_SEC", probe.CLAIM_CAP_READING_RESERVE_SEC / 2.0
    )
    assert probe.main() == 0
    assert called == [], called

    # The reserve is real slack over the measurement, not a token.
    assert probe.CLAIM_CAP_READING_RESERVE_SEC >= 90.0


def _seed_open_orders(conn, probe, *, scopes: int, per_scope: int) -> int:
    """OPEN orders whose horizon has passed and that no book ever reached."""

    rows = []
    for s_index in range(scopes):
        for o_index in range(per_scope):
            rows.append((
                f"o-{s_index}-{o_index}", f"ST{s_index:02d}", "2026-09-10",
                f"b{o_index}", "bid", 15,
                "2026-09-10T00:00:00Z", "2026-09-10T00:00:00Z",
                "2026-09-10T00:15:00Z", 12.0, 0.30, 0.29, 0.31, 0.01,
                None, 0, 0.3, "OPEN", None, None, "0" * 64,
            ))
    conn.executemany(probe.PLACE_INSERT_SQL, rows)
    conn.commit()
    return len(rows)


def test_resolve_writes_in_chunks_and_loses_no_row(tmp_path, monkeypatch) -> None:
    """Chunking the write must not change what is written.

    The fix bounds what a spent clock can still owe to one chunk, which only
    helps if a chunked pass is the same pass: every row resolved, every
    count the sum across chunks, and ``still_open`` computed from those sums
    rather than from a buffer that has been emptied along the way.
    """

    probe = _probe_module()
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    probe.ensure_schema(conn)
    seeded = _seed_open_orders(conn, probe, scopes=5, per_scope=7)

    flushes: list[int] = []
    real_flush = probe._flush

    def counting_flush(connection, sql, rows):
        flushes.append(len(rows))
        return real_flush(connection, sql, rows)

    monkeypatch.setattr(probe, "FLUSH_ROWS", 3)
    monkeypatch.setattr(probe, "_flush", counting_flush)
    receipt = probe.resolve(conn, deadline=None)

    assert receipt.get("expired") == seeded, receipt
    assert receipt["still_open"] == 0, receipt
    assert conn.execute(
        "SELECT COUNT(*) FROM maker_shadow_order WHERE state='OPEN'"
    ).fetchone()[0] == 0
    # It really did write in more than one piece, and no piece was large.
    non_empty = [n for n in flushes if n]
    assert len(non_empty) > 1, flushes
    # A scope's rows can top a chunk up past the threshold before the next
    # scope boundary flushes it, so the bound is threshold + one scope.
    assert max(non_empty) <= 3 + 7, flushes


def test_a_spent_clock_keeps_what_was_already_flushed(tmp_path, monkeypatch) -> None:
    """Stopping between chunks leaves committed work committed.

    The clock runs out part way; what was flushed before it stays written and
    the rest stays OPEN for the next pass to select -- which is the whole
    reason stopping early is safe.
    """

    probe = _probe_module()
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    probe.ensure_schema(conn)
    seeded = _seed_open_orders(conn, probe, scopes=6, per_scope=4)

    monkeypatch.setattr(probe, "FLUSH_ROWS", 2)
    # Spent after the third scope boundary has been checked.
    checks = {"n": 0}

    def spent_after_three(_deadline):
        checks["n"] += 1
        return checks["n"] > 3

    monkeypatch.setattr(probe, "_spent", spent_after_three)
    receipt = probe.resolve(conn, deadline=0.0)

    assert receipt.get("stopped_for_time") == 1, receipt
    done = receipt.get("expired", 0)
    assert 0 < done < seeded, receipt
    still = conn.execute(
        "SELECT COUNT(*) FROM maker_shadow_order WHERE state='OPEN'"
    ).fetchone()[0]
    # Committed and left-open partition the seeded rows exactly.
    assert done + still == seeded, (done, still, seeded)
    assert receipt["still_open"] == still, receipt


def test_every_write_in_the_pass_goes_through_the_chunked_flush() -> None:
    """One place writes, so the bound cannot be skipped by a new phase.

    Read off the AST: the only ``executemany`` in the probe is the one inside
    ``_flush``.  A phase that writes its own batch after its loop reopens
    exactly the gap this closed.
    """

    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    sites: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.fn = "<module>"

        def visit_FunctionDef(self, node):  # noqa: N802
            previous, self.fn = self.fn, node.name
            self.generic_visit(node)
            self.fn = previous

        def visit_Call(self, node):  # noqa: N802
            if isinstance(node.func, ast.Attribute) and node.func.attr == "executemany":
                sites.append(self.fn)
            self.generic_visit(node)

    Visitor().visit(tree)
    assert sites == ["_flush"], sites


def test_the_lane_still_declares_itself_sliced_against_the_lease() -> None:
    """The contract this pass kept breaking, so a change to it is loud.

    ``sliced`` is a promise the lane reads the lease and stops inside it.
    If this lane is ever redeclared ``one_shot``, the interval rule beside
    it applies and the bound has to be argued for, not inherited.
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_lease_daemon_under_test",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    daemon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(daemon)

    kind, bound = daemon.HOSTED_SWEEP_LEASE_CONTRACT["maker_shadow_probe"]
    assert kind == "sliced"
    assert bound == daemon.WRITER_LEASE_SEC
    # And it is not writer-turn exempt, which is why the overrun is expensive:
    # every second past the lease is a second production cannot start.
    assert "maker_shadow_probe" not in daemon.WRITER_TURN_EXEMPT_SWEEPS
    assert daemon.hosted_sweep_write_domain("maker_shadow_probe") == "ssot"
