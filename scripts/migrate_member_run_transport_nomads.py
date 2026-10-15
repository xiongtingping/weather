#!/usr/bin/env python3
"""Admit ``NCEP_NOMADS`` to the member-run transport CHECKs, in place.

Charter 270 (五) (user ruling 2026-10-15 「加」): NOAA's operational AIGEFS
is read from NCEP NOMADS and its rows carry that transport.  The table holds
8.8M rows; the earlier widening (``migrate_member_run_transport_allowlist``)
copied the whole table, which here would write several GB of WAL onto a disk
with ~16 GB free and keep the writer down for tens of minutes.

Adding a value to a CHECK's IN list only loosens it: every stored row stays
valid and the file format does not change, which is the case SQLite documents
for editing ``sqlite_schema`` directly (``PRAGMA writable_schema``, bump
``schema_version``).  The new text is the live text with ``'NCEP_NOMADS'``
appended to both lists -- the storage list and the narrower OOS-eligibility
list -- and nothing else; each anchor must occur exactly once in each list.

Verified before commit, inside the same transaction, and again on a fresh
connection: the table's DDL parses, an ``NCEP_NOMADS`` row passes both CHECKs
(inserted and rolled back under a savepoint with the append-only triggers
untouched, so a probe row can never persist), and an unknown transport is
still refused.

Run only with the resident stopped (``--confirm-weather-basket-yes-stopped``);
read-only without ``--apply``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = "calibration_native_ensemble_historical_member_run"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
NEW = "NCEP_NOMADS"
ANCHOR = "'DWD_OPENDATA_ICON_EU'"


def widened(live: str) -> str:
    if live.count(ANCHOR) != 2:
        raise SystemExit(f"expected {ANCHOR} in exactly two lists, found {live.count(ANCHOR)}")
    return live.replace(ANCHOR, f"{ANCHOR},'{NEW}'")


def probe(conn: sqlite3.Connection) -> None:
    """An NCEP_NOMADS row passes both CHECKs; an unknown transport does not."""

    row = conn.execute(f"SELECT * FROM {TABLE} WHERE offline_oos_eligible=1 LIMIT 1").fetchone()
    columns = [d[0] for d in conn.execute(f"SELECT * FROM {TABLE} LIMIT 0").description]
    base = dict(zip(columns, row))

    def attempt(transport: str) -> bool:
        values = {
            **base,
            "historical_member_run_id": "transport-probe-" + transport,
            "content_sha256": ("f" * 63 + "0") if transport == NEW else ("e" * 63 + "0"),
            "extraction_version": "transport_probe_never_kept",
            "transport_provider_id": transport,
        }
        conn.execute("SAVEPOINT transport_probe")
        try:
            conn.execute(
                f"INSERT INTO {TABLE} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                [values[c] for c in columns],
            )
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.execute("ROLLBACK TO transport_probe")
            conn.execute("RELEASE transport_probe")

    if not attempt(NEW):
        raise RuntimeError(f"{NEW} still refused")
    if attempt("NOT_A_TRANSPORT"):
        raise RuntimeError("CHECK no longer refuses an unknown transport")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-weather-basket-yes-stopped", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.apply and not args.confirm_weather_basket_yes_stopped:
        raise SystemExit("--apply needs --confirm-weather-basket-yes-stopped")
    conn = sqlite3.connect(
        str(database) if args.apply else f"file:{database}?mode=ro",
        uri=not args.apply, timeout=60.0, isolation_level=None,
    )
    conn.execute("PRAGMA busy_timeout=60000")
    live = str(conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()[0])
    if live.count(f"'{NEW}'") == 2:
        print("already widened; nothing to do")
        return 0
    target = widened(live)
    print(f"live DDL {len(live)} chars -> {len(target)} chars; {NEW} added to both lists")
    if not args.apply:
        print("(report only; --apply performs the migration)")
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema=ON")
        changed = conn.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=? AND sql=?",
            (target, TABLE, live),
        ).rowcount
        if changed != 1:
            raise RuntimeError(f"schema row not updated exactly once: {changed}")
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        probe(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    fresh = sqlite3.connect(str(database), timeout=60.0, isolation_level=None)
    try:
        fresh.execute("BEGIN IMMEDIATE")
        try:
            probe(fresh)
        finally:
            fresh.execute("ROLLBACK")
        stored = str(fresh.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()[0])
        if stored != target:
            raise RuntimeError("stored DDL is not the widened text")
        rows = fresh.execute(f"SELECT max(rowid) FROM {TABLE}").fetchone()[0]
    finally:
        fresh.close()
    print(f"widened in place; max rowid {rows}; probe passed on a fresh connection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
