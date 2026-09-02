"""The replay extract must not need more scratch than the host has.

The builder had one transaction spanning the whole joined table.  Measured
2026-09-01 on a 21 GiB host: the file itself reached 6 GiB and the
uncommitted pages took roughly 11 GiB on top, which tripped the storage
guard before the join finished -- twice, once at each of two window widths.
The rows are identical either way; only the peak differs, so the commit
cadence is a resource property and is pinned as one.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

BUILDER = ROOT / "scripts" / "build_ledger_replay_db.py"


def _join_loop() -> ast.For:
    """The chunk loop that carries the joined table."""

    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        source = ast.unparse(node)
        if "JOINED_TABLE" in source and "CHUNK" in source:
            return node
    raise AssertionError("the joined-table chunk loop is gone")


def test_the_joined_table_commits_inside_the_chunk_loop() -> None:
    """A commit must be reachable without finishing every chunk first.

    Not "a commit exists somewhere": the one after the loop was always
    there, and it is the one that made the peak.  This asserts the cadence
    is inside.
    """

    loop = _join_loop()
    commits = [
        node for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    assert commits, (
        "the chunk loop holds every page uncommitted until it ends; on"
        " 2026-09-01 that peaked ~11 GiB above the file and tripped the"
        " storage guard"
    )


def test_the_commit_interval_is_small_enough_to_bound_the_peak() -> None:
    """A commit somewhere in the loop is not the property; the interval is.

    Measured 2026-09-01: committing every 50 chunks still drew 7 GiB of
    scratch in four minutes while the file itself stayed at 550 MB, and the
    guard stopped the run.  Twenty thousand envelopes of bucket rows is not
    a bound on anything.  The commit is nearly free here -- journal_mode is
    OFF, so there is no journal to flush -- which is why the interval can be
    tight without costing time.
    """

    loop = _join_loop()
    source = ast.unparse(loop)
    match = re.search(r"start % \(CHUNK \* (\d+)\) == 0", source)
    assert match, "the commit is no longer paced off a chunk multiple"
    assert int(match.group(1)) <= 10, (
        f"committing every {match.group(1)} chunks is the cadence that"
        " tripped the storage guard; keep it at 10 or below"
    )


def test_the_extract_carries_no_journal_and_says_why() -> None:
    """WAL on a throwaway extract doubled it to 7.8 GB once already."""

    source = BUILDER.read_text(encoding="utf-8")
    assert "journal_mode=OFF" in source


def test_the_ledger_floor_is_the_narrowest_useful_window() -> None:
    """A window earlier than the ledger's own floor extracts dead weight.

    The ledger refuses every event day before ``LEDGER_EVENT_DATE_FLOOR``,
    so evidence older than it can never be read -- the 2026-07-28 extract
    the arms were built from carried well over half its bytes for days no
    arm could score.
    """

    import paper_trading_ledger as ledger  # noqa: PLC0415

    assert ledger.LEDGER_EVENT_DATE_FLOOR
    # A replay narrower than the floor would silently drop scoreable days,
    # so the floor is the floor -- not a suggestion to go narrower still.
    assert ledger.LEDGER_EVENT_DATE_FLOOR >= "2026-08-15"


def test_the_extract_carries_the_lineage_bodies_the_envelopes_reference() -> None:
    """An envelope without its lineage documents is unservable, not degraded.

    Lineage bodies are stored once and referenced by hash, so they live in
    their own table with no date column.  ``rehydrate_lineage_documents``
    fails closed on a missing body -- correctly, the body is the evidence --
    and the error it raises, ``xsb_history_envelope_body_invalid``, names
    neither the table nor the reason.  On 2026-09-01 that killed the walk on
    its first station after a 3.6 GB extract had already been built.

    The table is deduplicated by hash: the whole store is ~295 rows and
    about 1 MB, so it is copied whole and costs nothing.
    """

    import build_ledger_replay_db as builder  # noqa: PLC0415

    tables = {name for name, _cut in builder.INPUT_TABLES}
    assert "basket_probability_lineage_document" in tables, (
        "the extract copies envelopes but not the documents they reference"
        " by hash; every envelope then fails closed on rehydration"
    )
    # Whole-table: it has no date column to cut on, and a partial copy
    # would fail exactly like no copy for the envelopes it missed.
    cut = dict(builder.INPUT_TABLES)["basket_probability_lineage_document"]
    assert cut is None


def test_no_table_is_read_with_fetchall() -> None:
    """The envelope bodies do not fit in memory, so nothing may be slurped.

    Measured 2026-09-01: the bodies average 18.4 KB, so ``fetchall()`` on
    the 2026-07-28 window materialises 7.2 GB of Python objects.  That was
    the "vanishing disk" -- swap, not SQLite scratch -- and it survived two
    wrong fixes (narrowing the window, then tightening the commit cadence)
    because neither touched the read.

    Asserted structurally rather than by memory profiling: a cursor and
    ``fetchmany`` is the property, and it is cheap to check.
    """

    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    # ``_schema_of`` reads sqlite_master -- a handful of DDL strings, not
    # data -- and is allowed to slurp.  Everything that touches a data
    # table must stream.
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name == "_schema_of":
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "fetchall"
            ):
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        "a fetchall() here reads a whole data table into memory; the"
        " envelope bodies alone are 7.2 GB on the widest window: "
        + ", ".join(offenders)
    )
    source = BUILDER.read_text(encoding="utf-8")
    assert source.count("fetchmany(") >= 2, "both copy loops should stream"
