#!/usr/bin/env python3
"""Give the trajectory candidates already in evidence an activation row.

The OOS gate used to emit an activation only for a winner, so a refused
artifact could not be resolved at any authority and P1 ran with no
trajectory evidence at all -- 301 of 350 station-days at zero applied
strength, against a layer measured at 30 points of bucket accuracy on the
days it did run.  The gate is a label now, not the switch (user ruling,
2026-08-18), but that only changes what future training writes: 2631
candidates are already sitting in evidence, in date, with no activation.

This backfills them, at ``PRODUCTION_NO_LIVE_CANDIDATE``.  It promotes
nothing: every promotion receipt stays exactly as the gate decided it, and
the artifacts keep their own ``oos_gate_passed=0``.

One row per ``activation_id``, because that column is the activation table's
primary key and the old training packet minted one id per scope rather than
one per fit.  Where several artifacts share an id the newest wins, which is
the one a resolver would have chosen anyway.  Generation is 1 so that any
later fit -- which counts generations off the artifact table and is well past
1 by now -- supersedes these rather than tying with them; a tie is what the
resolver refuses as ambiguous.

Read-only without ``--commit``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.probability_evidence_store import (  # noqa: E402
    insert_probability_evidence_activation,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
ARTIFACT_KIND = "METAR_TRAJECTORY_LIKELIHOOD"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_CANDIDATE"


def _candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Newest un-activated candidate per activation id."""

    rows = conn.execute(
        "SELECT artifact_id, station_id, scope_key, valid_from_utc,"
        " valid_until_utc, trained_through_utc, artifact_json"
        " FROM basket_probability_evidence_artifact"
        " WHERE artifact_kind=? AND oos_gate_passed=0"
        "   AND artifact_id NOT IN ("
        "     SELECT artifact_id FROM basket_probability_evidence_activation)"
        " ORDER BY trained_through_utc",
        (ARTIFACT_KIND,),
    ).fetchall()
    newest: dict[str, dict[str, Any]] = {}
    for (
        artifact_id, station_id, scope_key, valid_from, valid_until,
        trained_through, payload,
    ) in rows:
        try:
            activation_id = str(
                (json.loads(payload) or {}).get("activation_id") or ""
            )
        except (TypeError, ValueError):
            continue
        if not activation_id:
            continue
        # Ordered by ``trained_through_utc`` above, so the last write wins.
        newest[activation_id] = {
            "activation_id": activation_id,
            "artifact_kind": ARTIFACT_KIND,
            "artifact_id": str(artifact_id),
            "station_id": str(station_id),
            "scope_key": str(scope_key),
            "generation": 1,
            "valid_from_utc": str(valid_from),
            "valid_until_utc": str(valid_until),
            "activated_at_utc": str(trained_through),
            "authority_scope": AUTHORITY_SCOPE,
        }
    return [
        {**row, "content_sha256": content_sha256(row)}
        for row in newest.values()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)

    database = args.db.expanduser().resolve(strict=True)
    conn = (
        sqlite3.connect(str(database), timeout=120.0)
        if args.commit
        else sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    )
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        rows = _candidates(conn)
        wrote = 0
        skipped: list[str] = []
        if args.commit and rows:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    existing = conn.execute(
                        "SELECT 1 FROM"
                        " basket_probability_evidence_activation"
                        " WHERE activation_id=?",
                        (row["activation_id"],),
                    ).fetchone()
                    if existing:
                        skipped.append(row["activation_id"])
                        continue
                    insert_probability_evidence_activation(conn, dict(row))
                    wrote += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        print(json.dumps({
            "schema": "metar_trajectory_candidate_activation_backfill_v1",
            "eligible": len(rows),
            "stations": len({row["station_id"] for row in rows}),
            "scopes": len({row["scope_key"] for row in rows}),
            "wrote": wrote,
            "skipped_existing": len(skipped),
            "committed": bool(args.commit),
            "promotes_anything": False,
        }, ensure_ascii=False, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
