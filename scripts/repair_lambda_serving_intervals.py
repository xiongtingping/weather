#!/usr/bin/env python3
"""Reopen lambda serving intervals that a table of the other claim scope closed.

Until 2026-09-14 activating a lambda table closed every open serving interval
of its stage, whatever the claim scope of the table serving there.  P0 carries
a window table (production) and a bucket table (research), so each activation
of one cut the other's interval short: on 2026-09-10..13 the window tables
43689953 and 2a3e79a8 were recorded as serving for 5 and 49 seconds when they
served for 20 and 56 hours, and the bucket table 18e20edc was recorded as
stopped while it is still activated.  The forward serving score and the
incumbent veto read those intervals.

The corruption has one signature, so the repair needs no judgement: an
interval whose recorded end is exactly the start of an interval of the OTHER
claim scope, with no interval of its own scope starting at that instant.  Its
true end is the start of the next interval of its own scope; with none, it is
still open if its table is still activated, and is left alone otherwise (a
retirement would also have closed it, and that end cannot be told apart).

Dry run by default; ``--commit`` applies every repair in one short
transaction.  Idempotent: a repaired interval ends where its own scope's next
interval starts, which no longer matches the signature.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.band_reliability_store import _begin_immediate  # noqa: E402

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def plan(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT i.stage_scope, COALESCE(t.claim_scope,'window'),"
        " i.lambda_table_sha256, i.served_from_utc, i.served_until_utc,"
        " COALESCE(t.activated, 0)"
        " FROM calibration_effective_edge_serving_interval i"
        " LEFT JOIN calibration_effective_edge_lambda t"
        "   ON t.lambda_table_sha256 = i.lambda_table_sha256"
        " ORDER BY i.stage_scope, i.served_from_utc"
    ).fetchall()
    starts: dict[tuple[str, str], set[str]] = {}
    for stage, scope, _sha, since, _until, _active in rows:
        starts.setdefault((stage, scope), set()).add(str(since))
    repairs = []
    for stage, scope, sha, since, until, active in rows:
        if until is None:
            continue
        others = {
            other for (s, other), values in starts.items()
            if s == stage and other != scope and str(until) in values
        }
        if not others or str(until) in starts[(stage, scope)]:
            continue
        later = sorted(v for v in starts[(stage, scope)] if v > str(since))
        if later:
            true_end = later[0]
        elif int(active) == 1:
            true_end = None
        else:
            repairs.append({
                "stage_scope": stage, "claim_scope": scope, "lambda_table_sha256": sha,
                "served_from_utc": since, "recorded_until_utc": until,
                "repaired_until_utc": until, "action": "left_alone_end_unknowable",
            })
            continue
        repairs.append({
            "stage_scope": stage, "claim_scope": scope, "lambda_table_sha256": sha,
            "served_from_utc": since, "recorded_until_utc": until,
            "repaired_until_utc": true_end, "action": "repair",
            "closed_by_claim_scope": sorted(others),
        })
    return repairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if not args.commit:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
        print(json.dumps(plan(conn), indent=1))
        conn.close()
        return 0
    conn = sqlite3.connect(str(database), timeout=60.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    _begin_immediate(conn)
    try:
        repairs = plan(conn)
        for item in repairs:
            if item["action"] != "repair":
                continue
            changed = conn.execute(
                "UPDATE calibration_effective_edge_serving_interval"
                " SET served_until_utc=?"
                " WHERE stage_scope=? AND served_from_utc=? AND lambda_table_sha256=?"
                "   AND served_until_utc=?",
                (item["repaired_until_utc"], item["stage_scope"], item["served_from_utc"],
                 item["lambda_table_sha256"], item["recorded_until_utc"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"interval_repair_row_count:{changed}:{item}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(json.dumps({"committed": True, "repairs": repairs}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
