"""Where the production paper ledger lives: its own file beside the SSOT.

User ruling 2026-09-19 (「移出」).  The ledger's four tables sat inside the
production SSOT, and SQLite's write lock is the whole file: writing even its
own tables made the ledger an SSOT writer, so the resident gave it an
exclusive writer turn and production waited it out.  Measured over the 28.9
hours before the move: 62 production cycles waited on it, 247s each, 4.25
hours in all -- while the tables it wrote held 167 positions and 134
settlements.  Its pass is almost entirely reading and re-deriving (7,419
production cycles of the last four days per pass), and nothing on the
decision path reads what it writes: the lambda/beta lanes read production
since charter 140, and the 2026-09-16 ruling made the ledger a rule
comparator whose money readings are all live fills.

So the tables moved to ``data/paper_ledger.db``.  The ledger opens the SSOT
``mode=ro`` as ``main`` and attaches this file as ``paper_ledger``: every
production table still reads under its own name, the paper tables resolve
unqualified to the attached file, and a DDL statement is the one thing that
must name the schema (an unqualified CREATE lands in ``main``, which is
read-only).  Readers of the paper tables attach the same file.

The move applies to the production SSOT only.  A replay database or a test
fixture keeps its paper tables in its own file, as before, and
:func:`attach_paper_ledger` refuses to hand one of them production's book.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SSOT_DB = _ROOT / "data" / "bayes_basket_target_ssot.db"
PAPER_LEDGER_DB = _ROOT / "data" / "paper_ledger.db"
PAPER_LEDGER_SCHEMA = "paper_ledger"
# Dropped in this order, so no foreign key is ever left pointing at nothing
# (a settlement names its position).
PAPER_LEDGER_TABLES = (
    "paper_trade_settlement",
    "paper_trade_position",
    "paper_trade_candidate_window",
    "paper_trade_refusal",
)


class PaperLedgerNotMigrated(RuntimeError):
    """The SSOT still holds paper tables, so the split layout cannot open.

    Unqualified names resolve to ``main`` first: with the old tables still in
    the SSOT, every read would see them and every write would hit a
    read-only file.  Refused rather than guessed at.
    """


def _same_file(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def main_paper_tables(conn: sqlite3.Connection) -> list[str]:
    """The paper tables the connection's own ``main`` file holds."""

    placeholders = ",".join("?" for _ in PAPER_LEDGER_TABLES)
    return sorted(
        str(row[0]) for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'"
            f" AND name IN ({placeholders})",
            PAPER_LEDGER_TABLES,
        )
    )


def is_attached(conn: sqlite3.Connection) -> bool:
    return any(
        str(row[1]) == PAPER_LEDGER_SCHEMA
        for row in conn.execute("PRAGMA database_list")
    )


def schema_prefix(conn: sqlite3.Connection) -> str:
    """``paper_ledger.`` when the tables live in the attached file, else ``""``."""

    return f"{PAPER_LEDGER_SCHEMA}." if is_attached(conn) else ""


def attach_paper_ledger(
    conn: sqlite3.Connection,
    *,
    ssot_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
) -> bool:
    """Make the production paper tables readable on a connection to the SSOT.

    True when they are readable through the attachment afterwards.  A no-op
    (False) when the connection's ``main`` is not the production SSOT -- a
    replay database or a fixture owns its own paper tables, and must never
    be handed production's -- when the SSOT still holds the tables itself
    (before the move), when the file does not exist yet, or when SQLite
    refuses the ATTACH (inside an open transaction).
    """

    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return False
    if any(str(row[1]) == PAPER_LEDGER_SCHEMA for row in rows):
        return True
    main = next((str(row[2] or "") for row in rows if str(row[1]) == "main"), "")
    if not main or not _same_file(main, ssot_path or PRODUCTION_SSOT_DB):
        return False
    ledger = Path(ledger_path or PAPER_LEDGER_DB)
    if not ledger.is_file():
        return False
    try:
        if main_paper_tables(conn):
            return False
        conn.execute(
            f"ATTACH DATABASE ? AS {PAPER_LEDGER_SCHEMA}", (str(ledger),)
        )
    except sqlite3.Error:
        return False
    return True


def open_ledger_connection(
    *,
    writable: bool,
    ssot_path: str | Path | None = None,
    ledger_path: str | Path | None = None,
    timeout: float = 60.0,
) -> sqlite3.Connection:
    """The ledger's own connection: SSOT read-only, paper file attached.

    Writable opens create the file (WAL, so readers never block the ledger);
    a read-only open needs it to exist.  Raises
    :class:`PaperLedgerNotMigrated` while the SSOT still holds paper tables.
    """

    ssot = Path(ssot_path or PRODUCTION_SSOT_DB)
    ledger = Path(ledger_path or PAPER_LEDGER_DB)
    conn = sqlite3.connect(f"file:{ssot}?mode=ro", uri=True, timeout=timeout)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        left = main_paper_tables(conn)
        if left:
            raise PaperLedgerNotMigrated(
                "the SSOT still holds " + ",".join(left)
                + "; run scripts/migrate_paper_ledger_out_of_ssot.py"
            )
        if not writable and not ledger.is_file():
            raise FileNotFoundError(str(ledger))
        conn.execute(
            f"ATTACH DATABASE ? AS {PAPER_LEDGER_SCHEMA}", (str(ledger),)
        )
        if writable:
            conn.execute(f"PRAGMA {PAPER_LEDGER_SCHEMA}.journal_mode=WAL")
    except BaseException:
        conn.close()
        raise
    return conn


__all__ = [
    "PAPER_LEDGER_DB",
    "PAPER_LEDGER_SCHEMA",
    "PAPER_LEDGER_TABLES",
    "PRODUCTION_SSOT_DB",
    "PaperLedgerNotMigrated",
    "attach_paper_ledger",
    "is_attached",
    "main_paper_tables",
    "open_ledger_connection",
    "schema_prefix",
]
