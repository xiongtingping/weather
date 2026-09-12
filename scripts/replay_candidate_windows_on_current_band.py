#!/usr/bin/env python3
"""Re-derive settled candidate windows on the band table that serves today.

Charter 110 partitions the lambda fit on the band axis, because moving the
axis moves every claim and the claim is the denominator of ``a = price /
claim``.  That is right, and it left P0 with nothing: its band table moved to
``centre_distance`` on 2026-09-07 16:19, distance-axis candidate windows only
exist for event days that have not settled yet, and so the fit has had zero
seats since.  The served table is frozen at a claim-axis vintage it can no
longer refresh.

The rows are not dead, though -- they are mis-keyed.  A settled window
carries two facts that do not depend on our probabilities at all: the
all-in price the book quoted, and whether the truth landed inside it.  Only
the claim is ours.  So this recomputes the claim on the current band table
and leaves the other two exactly as they were: the question the fit asks
("at this agreement, how often does the claim arrive?") is answerable on
that pair, and it is the question lambda has to answer about the claims it
will actually see.

Measured 2026-09-08 on P0's 841 settled claim-axis windows: all 841
recompute, and 196 of them (23.3%) land in a different lambda band than the
one they are filed under -- which is why they cannot simply be admitted as
they stand, and why they are worth recovering rather than waiting out.

WHAT THIS IS NOT.  These claims were never served.  The rows go to their own
table, never to ``paper_trade_candidate_window``, whose contract is that it
holds what production's own function produced at a decision moment.  The fit
may read them (a claim's honesty is a property of the claim, and these are
the claims the code now computes); the activation guard may not, because
whether a table earns its seat is a question about the book that was really
bought.  ``fit_lambda_table(include_replayed=True)`` and ``admitted_return``
respectively.

KNOWN APPROXIMATION, stated rather than hidden: the model centre is carried
over from the original envelope.  A band table keyed on centre distance can
in principle move the peak (the 1-2 bucket cell is 1.057 against 1.014 at
0-1), which would change which windows hold the centre.  It takes two
near-tied buckets to flip, and this does not re-derive it.

Read-only against the envelope store; writes only its own table.
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

from bayes_basket_target.band_reliability_store import (  # noqa: E402
    band_table_axis,
    load_band_table,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    claim_transform_fingerprint,
    window_claim_factor,
)
from bayes_basket_target.cross_consensus import (  # noqa: E402
    apply_band_reliability,
)
from bayes_basket_target.off_centre_claim_store import (  # noqa: E402
    load_off_centre_bands,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
EPS = 1e-9
SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trade_candidate_window_replay (
  replay_id TEXT PRIMARY KEY,
  source_candidate_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  decision_at_utc TEXT NOT NULL,
  buckets_json TEXT NOT NULL,
  window_size INTEGER NOT NULL,
  -- Recomputed.  ``raw_win_probability`` is the window mass under the band
  -- table named below; ``win_probability`` is that mass through the kernel's
  -- own ``window_claim_factor``.  Nothing here re-implements a rule.
  win_probability REAL NOT NULL,
  raw_win_probability REAL NOT NULL,
  -- Carried unchanged, and the reason this replay is answerable: neither
  -- depends on our probabilities.
  all_in_price REAL NOT NULL,
  hit INTEGER,
  band_axis TEXT NOT NULL,
  band_table_sha256 TEXT NOT NULL,
  claim_transform_sha256 TEXT NOT NULL,
  source_band_axis TEXT,
  -- Charter 131: the shape rule the SOURCE window was enumerated under.
  -- A replay re-derives the claim, never the window, so the shape is the
  -- source's; the lambda fit partitions on it.
  seat_shape_rule TEXT,
  replayed_at_utc TEXT NOT NULL,
  UNIQUE(source_candidate_id, band_table_sha256)
)
"""
INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_candidate_replay_scope"
    " ON paper_trade_candidate_window_replay("
    "  stage, band_axis, claim_transform_sha256, event_date)"
)


def _pre_band_vector(
    served: dict[str, float], receipt: dict[str, object]
) -> dict[str, float] | None:
    """Undo the band factors the envelope recorded applying.

    The receipt keeps ``applied_factors`` per bucket and nothing else is
    needed: the layer normalises after scaling, so dividing back out and
    re-normalising returns the vector it was handed.
    """

    applied = receipt.get("applied_factors")
    if not isinstance(applied, dict):
        return None
    pre = {
        bucket: (
            value / float(applied[bucket]) if bucket in applied else value
        )
        for bucket, value in served.items()
    }
    total = sum(pre.values())
    if total <= 0.0:
        return None
    return {bucket: value / total for bucket, value in pre.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--stage",
        default="P0",
        help=(
            "stage to replay, or 'all' for every stage.  'all' is what the"
            " sweep passes: a claim-axis stage is a no-op here (its served"
            " rows already carry the axis), so the sweep does not have to be"
            " edited again the next time a stage moves -- which is exactly"
            " what was missed when P1 moved on 2026-09-09 and this call was"
            " still hard-coded to P0 alone."
        ),
    )
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--as-of", default=None, help="stamp; required to commit")
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.commit and not args.as_of:
        print("--commit needs --as-of (scripts may not read the clock)")
        return 2
    conn = (
        sqlite3.connect(str(database), timeout=180.0)
        if args.commit
        else sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=180.0)
    )
    conn.execute("PRAGMA busy_timeout=180000")
    if str(args.stage) == "all":
        reports = []
        worst = 0
        for stage in ("P0", "P0-M", "P1"):
            code = replay_stage(conn, stage=stage, args=args, reports=reports)
            worst = max(worst, code)
        print(json.dumps({"stage": "all", "stages": reports}, indent=1))
        conn.close()
        return worst
    code = replay_stage(conn, stage=str(args.stage), args=args, reports=None)
    conn.close()
    return code


def replay_stage(conn, *, stage: str, args, reports: list | None) -> int:
    def emit(payload: dict) -> None:
        if reports is None:
            print(json.dumps(payload, sort_keys=True))
        else:
            reports.append(payload)

    loaded = load_band_table(conn, stage=stage)
    if loaded is None:
        emit({"stage": stage, "error": "no_activated_band_table"})
        return 1
    factors, band_sha = loaded
    axis = band_table_axis(conn, stage=stage)
    # The off-centre correction the enumeration would apply today.  Before
    # 2026-09-09 this script used the kernel bootstrap while production read
    # the store, so a replayed claim could differ from the served one by the
    # very table this lane exists to keep current.
    off_centre = load_off_centre_bands(conn, stage=stage)
    off_centre_bands = off_centre[0] if off_centre else None
    off_centre_sha = off_centre[1] if off_centre else None
    if axis != "centre_distance":
        emit({
            "stage": stage, "axis": axis,
            "note": "claim-axis table: the served rows already carry it",
        })
        return 0

    source_has_shape = any(
        str(row[1]) == "seat_shape_rule"
        for row in conn.execute("PRAGMA table_info(paper_trade_candidate_window)")
    )
    rows = conn.execute(
        "SELECT candidate_id, station_id, event_date, decision_at_utc,"
        " buckets_json, all_in_price, hit, band_axis"
        + (", seat_shape_rule" if source_has_shape else ", NULL")
        + " FROM paper_trade_candidate_window"
        " WHERE stage=? AND hit IS NOT NULL",
        (stage,),
    ).fetchall()

    envelopes: dict[tuple[str, str, str], tuple] = {}
    pending: list[tuple] = []
    counted = {"rows": len(rows), "replayed": 0, "no_envelope": 0,
               "no_band_receipt": 0, "bucket_missing": 0, "degenerate": 0}
    for cid, st, ed, at, bj, price, hit, src_axis, src_shape in rows:
        key = (str(st), str(ed), str(at))
        if key not in envelopes:
            envelopes[key] = conn.execute(
                "SELECT probabilities_json, bucket_ordinals_json, lineage_json"
                " FROM basket_probability_envelope"
                " WHERE stage=? AND state='COMMITTED' AND station_id=?"
                "   AND event_date=? AND decision_at_utc=? LIMIT 1",
                (stage, st, ed, at),
            ).fetchone()
        env = envelopes[key]
        if env is None:
            counted["no_envelope"] += 1
            continue
        served = {k: float(v) for k, v in json.loads(env[0]).items()}
        ordinals = {k: int(v) for k, v in json.loads(env[1]).items()}
        lineage = json.loads(env[2])
        try:
            receipt = json.loads(lineage.get("band_reliability_json") or "{}")
        except (TypeError, ValueError):
            receipt = {}
        pre = _pre_band_vector(served, receipt)
        if pre is None:
            counted["no_band_receipt"] += 1
            continue
        rebuilt, _ = apply_band_reliability(
            pre, factors=factors, ordinals=ordinals
        )
        window = [str(b) for b in json.loads(bj)]
        if not all(b in rebuilt and b in ordinals for b in window):
            counted["bucket_missing"] += 1
            continue
        raw_win = sum(rebuilt[b] for b in window)
        centre = str(lineage.get("model_center_bucket") or "")
        if centre in window:
            gap = 0.0
        elif centre in ordinals:
            gap = float(min(
                abs(ordinals[b] - ordinals[centre]) for b in window
            ))
        else:
            gap = 1.0
        claim = min(1.0 - EPS, raw_win * window_claim_factor(
            raw_win, center_gap=gap, off_centre_bands=off_centre_bands
        ))
        if raw_win <= 0.0 or claim <= 0.0 or float(price) <= 0.0:
            counted["degenerate"] += 1
            continue
        pending.append((
            f"replay-{band_sha[:12]}-{cid}", cid, st, ed, stage, at,
            json.dumps(window, ensure_ascii=False), len(window),
            claim, raw_win, float(price),
            None if hit is None else int(hit),
            axis, band_sha,
            claim_transform_fingerprint(
                holds_centre=(gap <= 0.0),
                off_centre_table_sha=off_centre_sha,
            ),
            src_axis, src_shape, str(args.as_of or ""),
        ))
        counted["replayed"] += 1

    if args.commit:
        conn.execute(SCHEMA)
        conn.execute(INDEX)
        if not any(
            str(row[1]) == "seat_shape_rule"
            for row in conn.execute(
                "PRAGMA table_info(paper_trade_candidate_window_replay)"
            )
        ):
            conn.execute(
                "ALTER TABLE paper_trade_candidate_window_replay"
                " ADD COLUMN seat_shape_rule TEXT"
            )
        conn.executemany(
            "INSERT INTO paper_trade_candidate_window_replay("
            " replay_id, source_candidate_id, station_id, event_date, stage,"
            " decision_at_utc, buckets_json, window_size, win_probability,"
            " raw_win_probability, all_in_price, hit, band_axis,"
            " band_table_sha256, claim_transform_sha256, source_band_axis,"
            " seat_shape_rule, replayed_at_utc)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_candidate_id, band_table_sha256) DO UPDATE SET"
            "  win_probability=excluded.win_probability,"
            "  raw_win_probability=excluded.raw_win_probability,"
            "  claim_transform_sha256=excluded.claim_transform_sha256,"
            "  seat_shape_rule=excluded.seat_shape_rule,"
            "  replayed_at_utc=excluded.replayed_at_utc",
            pending,
        )
        conn.commit()
    emit({
        "schema": "candidate_window_band_replay_v1",
        "stage": stage, "band_axis": axis, "band_table_sha256": band_sha,
        "committed": bool(args.commit), **counted,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
