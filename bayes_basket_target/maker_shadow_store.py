"""Where the maker shadow book lives: frozen, on the cold disk.

User ruling 2026-10-02 (「去掉挂单探针采集」): the probe is retired and the
book no longer grows.  It stopped writing on 2026-09-28 and left the machine
on 09-30 (``weather_cold/maker_shadow_retired_20260930``, with a receipt of
its bytes, sha256 and last rowids).  What is left here is the read path the
offline tools use against that copy -- ``scripts/fit_delta_v2_fill_from_
maker_shadow.py`` (the lineage of the served fill batch, which the daily
authority sweep renews without refitting) and
``scripts/measure_venue_price_calibration.py``.  Nothing in production reads
it, and with the cold disk absent the attach is simply refused.

What the shadow book was.  The probe wrote down what
a resting bid WOULD have been -- station, bucket, limit price, expiry --
stamped at a moment with no knowledge of the future, and let the live book
decide later whether it filled.  It placed nothing and risked nothing.  Eight
price rules ran side by side (``ask``, ``mid``, ``bid``, ``bid+1t``,
``bid+2t``, ``bid+1t@chase``, ``bid+1t@then_take``, ``claim_cap``); ``bid+1t``
is the rule charter 163 put the tiebreak lane on, and its fill rate is the
number that came from here.  It existed because every maker number before it
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

So the tables moved to ``data/maker_shadow.db`` (2026-09-21), and from
there to the cold disk.  A reader opens the SSOT
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

from .offloaded_paths import DEFAULT_COLD_TIER_ROOT

_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SSOT_DB = _ROOT / "data" / "bayes_basket_target_ssot.db"
MAKER_SHADOW_DB = (
    DEFAULT_COLD_TIER_ROOT / "maker_shadow_retired_20260930" / "maker_shadow.db"
)
MAKER_SHADOW_SCHEMA = "maker_shadow"
#: The book's two tables (a settlement names its intent).
MAKER_SHADOW_TABLES = (
    "maker_shadow_settlement",
    "maker_shadow_order",
)


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


__all__ = [
    "MAKER_SHADOW_DB",
    "MAKER_SHADOW_SCHEMA",
    "MAKER_SHADOW_TABLES",
    "PRODUCTION_SSOT_DB",
    "attach_maker_shadow",
    "is_attached",
    "main_shadow_tables",
]
