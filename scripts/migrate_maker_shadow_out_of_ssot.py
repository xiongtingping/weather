#!/usr/bin/env python3
"""Move the maker shadow book out of the SSOT into its own file.

User ruling 2026-09-21 (「做影子表移出 + 解绑生产」); see
``bayes_basket_target.maker_shadow_store`` for why.  Adapted from the paper
ledger's move (charter 155), one pass, in this order:

1. Take the shadow probe's own single-instance lock, so no probe pass can
   write a row while the book is between files (a running pass is waited out).
2. Copy the two ``maker_shadow_*`` tables into ``data/maker_shadow.db`` --
   the DDL exactly as the SSOT stores it, every index, every row with its
   rowid -- and prove the copy: per table, row count and a digest of every
   row in rowid order must match.
3. Only then drop the two tables from the SSOT, in one transaction.

Dry run by default: it prints what it would do and touches nothing.  A
second ``--apply`` after success is a no-op.  If both files already hold
shadow tables it refuses: that is two books, and which one is the book is a
decision, not something to guess.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.maker_shadow_store import (  # noqa: E402
    MAKER_SHADOW_DB,
    MAKER_SHADOW_SCHEMA,
    MAKER_SHADOW_TABLES,
    PRODUCTION_SSOT_DB,
    main_shadow_tables,
)

SCHEMA = "maker_shadow_migration_receipt_v1"
# The name ``scripts/maker_shadow_probe_sweep.sh`` passes to
# ``single_instance``; same directory, same protocol.
PROBE_LOCK_NAME = "maker_shadow_probe_sweep"
LOCK_WAIT_SEC = 900.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _lock_path() -> Path:
    base = Path(os.environ.get("TMPDIR") or "/tmp")
    return base / "weather-sweep-locks" / f"{PROBE_LOCK_NAME}.lock"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_probe_lock(*, wait_sec: float = LOCK_WAIT_SEC) -> Path:
    """Hold the sweep's lock the way ``single_instance`` does: pid, noclobber."""

    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_sec
    while True:
        try:
            fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            try:
                holder = int("".join(ch for ch in lock.read_text() if ch.isdigit()) or 0)
            except OSError:
                holder = 0
            if holder and _pid_alive(holder):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"shadow probe pass still running (pid {holder})")
                time.sleep(5.0)
                continue
            lock.unlink(missing_ok=True)  # a lock left by a dead pid
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{os.getpid()}\n")
        return lock


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _digest(conn: sqlite3.Connection, schema: str, table: str, columns: list[str]) -> str:
    digest = hashlib.sha256()
    cols = ",".join(columns)
    for row in conn.execute(f"SELECT rowid,{cols} FROM {schema}.{table} ORDER BY rowid"):
        digest.update(json.dumps(list(row), default=str, ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def migrate(
    *,
    ssot_path: Path,
    shadow_path: Path,
    apply: bool,
) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "started_at_utc": _utc_now(),
        "ssot": str(ssot_path),
        "maker_shadow": str(shadow_path),
        "applied": False,
    }
    conn = sqlite3.connect(str(ssot_path), timeout=120.0)
    try:
        conn.execute("PRAGMA busy_timeout=120000")
        in_ssot = main_shadow_tables(conn)
        in_shadow: list[str] = []
        if shadow_path.is_file():
            probe = sqlite3.connect(f"file:{shadow_path}?mode=ro", uri=True)
            try:
                in_shadow = main_shadow_tables(probe)
            finally:
                probe.close()
        receipt["tables_in_ssot"] = in_ssot
        receipt["tables_in_maker_shadow"] = in_shadow
        if not in_ssot:
            receipt["state"] = "ALREADY_MOVED" if in_shadow else "NOTHING_TO_MOVE"
            return receipt
        if in_shadow:
            receipt["state"] = "REFUSED_TWO_BOOKS"
            return receipt
        ddl = [
            (str(kind), str(name), str(sql))
            for kind, name, sql in conn.execute(
                "SELECT type, name, sql FROM main.sqlite_master"
                " WHERE sql IS NOT NULL AND tbl_name IN (%s)"
                " ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
                % ",".join("?" for _ in MAKER_SHADOW_TABLES),
                MAKER_SHADOW_TABLES,
            )
        ]
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])
            for table in in_ssot
        }
        receipt["ddl"] = [[kind, name] for kind, name, _ in ddl]
        receipt["rows"] = counts
        if not apply:
            receipt["state"] = "DRY_RUN"
            return receipt

        lock = acquire_probe_lock()
        try:
            # Re-read under the lock: a pass that finished while we waited
            # may have moved the counts.
            counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])
                for table in in_ssot
            }
            receipt["rows"] = counts
            shadow_path.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(str(shadow_path))
            try:
                target.execute("PRAGMA journal_mode=WAL")
                for _kind, _name, sql in ddl:
                    target.execute(sql)
                target.commit()
            finally:
                target.close()
            conn.execute(f"ATTACH DATABASE ? AS {MAKER_SHADOW_SCHEMA}", (str(shadow_path),))
            digests: dict[str, dict[str, object]] = {}
            conn.execute("BEGIN")
            for table in in_ssot:
                cols = _columns(conn, "main", table)
                listed = ",".join(cols)
                conn.execute(
                    f"INSERT INTO {MAKER_SHADOW_SCHEMA}.{table}(rowid,{listed})"
                    f" SELECT rowid,{listed} FROM main.{table} ORDER BY rowid"
                )
            conn.commit()
            for table in in_ssot:
                cols = _columns(conn, "main", table)
                if cols != _columns(conn, MAKER_SHADOW_SCHEMA, table):
                    raise RuntimeError(f"{table}: column lists differ after copy")
                moved = int(conn.execute(
                    f"SELECT COUNT(*) FROM {MAKER_SHADOW_SCHEMA}.{table}"
                ).fetchone()[0])
                source_digest = _digest(conn, "main", table, cols)
                copy_digest = _digest(conn, MAKER_SHADOW_SCHEMA, table, cols)
                digests[table] = {
                    "rows": moved, "sha256": copy_digest,
                    "matches": moved == counts[table] and source_digest == copy_digest,
                }
                if not digests[table]["matches"]:
                    raise RuntimeError(f"{table}: copy does not match the SSOT")
            receipt["copies"] = digests
            conn.execute(f"DETACH DATABASE {MAKER_SHADOW_SCHEMA}")
            # Only now does the SSOT let go.  One transaction: the four go
            # together or not at all.
            conn.execute("BEGIN IMMEDIATE")
            for table in MAKER_SHADOW_TABLES:
                if table in in_ssot:
                    conn.execute(f"DROP TABLE main.{table}")
            conn.commit()
            receipt["dropped_from_ssot"] = [t for t in MAKER_SHADOW_TABLES if t in in_ssot]
            receipt["state"] = "MOVED"
            receipt["applied"] = True
        finally:
            lock.unlink(missing_ok=True)
    finally:
        conn.close()
    receipt["finished_at_utc"] = _utc_now()
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssot", type=Path, default=PRODUCTION_SSOT_DB)
    parser.add_argument("--maker-shadow", type=Path, default=MAKER_SHADOW_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, default=None)
    args = parser.parse_args()
    receipt = migrate(
        ssot_path=args.ssot.expanduser().resolve(strict=True),
        shadow_path=args.maker_shadow.expanduser().resolve(),
        apply=bool(args.apply),
    )
    text = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
    print(text)
    if args.receipt is not None:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(text + "\n", encoding="utf-8")
    return 2 if receipt.get("state") == "REFUSED_TWO_BOOKS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
