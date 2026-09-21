"""Where the maker shadow book lives: its own file beside the SSOT.

User ruling 2026-09-21 (「做影子表移出 + 解绑生产」).  Same reasoning as the
paper ledger's move (charter 155) and the same shape.

What the shadow book is.  ``scripts/maker_shadow_probe.py`` writes down what
a resting bid WOULD have been -- station, bucket, limit price, expiry --
stamped at a moment with no knowledge of the future, and lets the live book
decide later whether it filled.  It places nothing and risks nothing.  Eight
price rules run side by side (``ask``, ``mid``, ``bid``, ``bid+1t``,
``bid+2t``, ``bid+1t@chase``, ``bid+1t@then_take``, ``claim_cap``); ``bid+1t``
is the rule charter 163 put the tiebreak lane on, and its fill rate is the
number that came from here.  It exists because every maker number before it
came from replaying stored snapshots, and each replay chose the placement
time after knowing the outcome -- once counting a losing bucket's decay to
zero as a fill, once comparing the filled subset against the whole
population.  Writing the order before the market moves is the one way to
make both mistakes impossible.

Why it left.  SQLite's write lock is the whole file, so writing its own two
tables made the probe an SSOT writer: the resident handed it an exclusive
writer turn every three hours and production waited it out.  On 2026-09-21
the two tables held 5,257,484 intents and 1,326,918 settlements, 1.34 GiB
with four indexes, growing 50k-195k rows a day -- all of it research, none
of it on the decision path.  Production reads the FITTED artifact
(``basket_delta_v2_fill_artifact``, via
``scripts/fit_delta_v2_fill_from_maker_shadow.py``), never these rows; the
only mentions inside ``bayes_basket_target`` are three comments.

So the tables moved to ``data/maker_shadow.db``.  A reader opens the SSOT
``mode=ro`` as ``main`` and attaches this file as ``maker_shadow``:
production tables still read under their own names, the shadow tables
resolve unqualified to the attached file, and a DDL statement is the one
thing that must name the schema (an unqualified CREATE lands in ``main``,
which is read-only).

The move applies to the production SSOT only.  A replay database or a test
fixture keeps its shadow tables in its own file, and :func:`attach_maker_shadow`
refuses to hand one of them production's book.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SSOT_DB = _ROOT / "data" / "bayes_basket_target_ssot.db"
MAKER_SHADOW_DB = _ROOT / "data" / "maker_shadow.db"
MAKER_SHADOW_SCHEMA = "maker_shadow"
#: Dropped in this order, so no row is left naming an order that is gone: a
#: settlement names its intent.
MAKER_SHADOW_TABLES = (
    "maker_shadow_settlement",
    "maker_shadow_order",
)


class MakerShadowNotMigrated(RuntimeError):
    """The SSOT still holds the shadow tables."""


def _same_file(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def main_shadow_tables(conn: sqlite3.Connection) -> list[str]:
    """The shadow tables the connection's own ``main`` file holds."""

    placeholders = ",".join("?" for _ in MAKER_SHADOW_TABLES)
    return sorted(
        str(row[0]) for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'"
            f" AND name IN ({placeholders})",
            MAKER_SHADOW_TABLES,
        )
    )


def is_attached(conn: sqlite3.Connection) -> bool:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return False
    return any(str(row[1]) == MAKER_SHADOW_SCHEMA for row in rows)


def attach_maker_shadow(
    conn: sqlite3.Connection, *,
    ssot_path: str | Path | None = None,
    shadow_path: str | Path | None = None,
) -> bool:
    """Make the shadow tables readable on a connection to the SSOT.

    True when they are readable through the attachment afterwards.  A no-op
    (False) when the connection's ``main`` is not the production SSOT -- a
    replay database or a fixture owns its own shadow tables and must never
    be handed production's -- when the SSOT still holds the tables itself
    (before the move), when the file does not exist yet, or when SQLite
    refuses the ATTACH (inside an open transaction).
    """

    if is_attached(conn):
        return True
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return False
    main = next((str(row[2] or "") for row in rows if str(row[1]) == "main"), "")
    if not main or not _same_file(main, ssot_path or PRODUCTION_SSOT_DB):
        return False
    shadow = Path(shadow_path or MAKER_SHADOW_DB)
    if not shadow.is_file():
        return False
    try:
        if main_shadow_tables(conn):
            return False
        conn.execute(f"ATTACH DATABASE ? AS {MAKER_SHADOW_SCHEMA}", (str(shadow),))
    except sqlite3.Error:
        return False
    return is_attached(conn)


def open_shadow_connection(
    *, writable: bool,
    ssot_path: str | Path | None = None,
    shadow_path: str | Path | None = None,
    timeout: float = 60.0,
) -> sqlite3.Connection:
    """The probe's own connection: SSOT read-only, shadow file attached.

    Writable opens create the file (WAL, so readers never block the probe);
    a read-only open needs it to exist.  Raises :class:`MakerShadowNotMigrated`
    while the SSOT still holds the tables.
    """

    ssot = Path(ssot_path or PRODUCTION_SSOT_DB)
    shadow = Path(shadow_path or MAKER_SHADOW_DB)
    conn = sqlite3.connect(f"file:{ssot}?mode=ro", uri=True, timeout=timeout)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        left = main_shadow_tables(conn)
        if left:
            raise MakerShadowNotMigrated(
                "the SSOT still holds " + ",".join(left)
                + "; run scripts/migrate_maker_shadow_out_of_ssot.py --apply"
            )
        if not writable and not shadow.is_file():
            raise FileNotFoundError(str(shadow))
        conn.execute(f"ATTACH DATABASE ? AS {MAKER_SHADOW_SCHEMA}", (str(shadow),))
        if writable:
            conn.execute(f"PRAGMA {MAKER_SHADOW_SCHEMA}.journal_mode=WAL")
    except BaseException:
        conn.close()
        raise
    return conn


def schema_prefix(conn: sqlite3.Connection) -> str:
    """``maker_shadow.`` when the file is attached, else empty.

    A DDL statement has to name the schema -- an unqualified CREATE lands in
    ``main``, which is read-only on a probe connection -- while a fixture
    that owns its tables in ``main`` needs no prefix at all.
    """

    return f"{MAKER_SHADOW_SCHEMA}." if is_attached(conn) else ""


__all__ = [
    "MAKER_SHADOW_DB",
    "MAKER_SHADOW_SCHEMA",
    "MAKER_SHADOW_TABLES",
    "PRODUCTION_SSOT_DB",
    "MakerShadowNotMigrated",
    "attach_maker_shadow",
    "is_attached",
    "main_shadow_tables",
    "open_shadow_connection",
    "schema_prefix",
]
