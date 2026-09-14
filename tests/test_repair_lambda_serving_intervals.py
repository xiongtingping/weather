"""The one-off repair for serving intervals a table of the other scope closed."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from bayes_basket_target import effective_edge_store as store  # noqa: E402
from repair_lambda_serving_intervals import plan  # noqa: E402


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    tables = (
        ("w1", "window", 0), ("b1", "bucket", 0), ("w2", "window", 1),
        ("b2", "bucket", 1), ("w0", "window", 0),
    )
    for sha, scope, active in tables:
        conn.execute(
            "INSERT INTO calibration_effective_edge_lambda(lambda_table_sha256,"
            " bands_json, evidence_json, fitted_at_utc, window_days, seat_count,"
            " activated, stage_scope, claim_scope) VALUES(?,?,?,?,?,?,?,?,?)",
            (sha, "{}", "{}", "t", 28, 0, active, "P0", scope),
        )
    # What the old close produced: each activation ended the other scope's
    # interval.  w0 -> w1 is an ordinary same-scope handover.
    intervals = (
        ("w0", "01", "02"), ("w1", "02", "03"), ("b1", "03", "04"),
        ("w2", "04", "05"), ("b2", "05", None),
    )
    for sha, since, until in intervals:
        conn.execute(
            "INSERT INTO calibration_effective_edge_serving_interval VALUES(?,?,?,?)",
            ("P0", sha, since, until),
        )
    return conn


def test_only_intervals_closed_by_the_other_scope_are_repaired() -> None:
    conn = _db()
    repairs = {r["lambda_table_sha256"]: r["repaired_until_utc"] for r in plan(conn)}
    # w1 served until w2 took over; b1 until b2; w2 is still serving (w2's
    # recorded end is b2's start, and w2 is activated with no successor).
    assert repairs == {"w1": "04", "b1": "05", "w2": None}
    for sha, until in repairs.items():
        conn.execute(
            "UPDATE calibration_effective_edge_serving_interval SET served_until_utc=?"
            " WHERE lambda_table_sha256=?", (until, sha),
        )
    assert plan(conn) == []
