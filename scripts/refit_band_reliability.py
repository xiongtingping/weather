#!/usr/bin/env python3
"""Refit every stage's band reliability table and (with --commit) activate.

Thin CLI over ``bayes_basket_target.band_reliability_store``, which holds
the admission rule, the evidence-lane loaders (including exact inversion of
post-band serving rows from their receipts), the content-addressed
persistence and the fit journal.

Every stage -- P0, P0-M, P1 -- is fitted every day from the moment it has
any settled history, long before it can qualify.  A stage short of evidence
fits to identity and records how far off it is; promotion happens by itself
the day the hurdles are cleared, with no code change.

Without ``--commit`` this reports only.  With ``--commit`` each stage's
fitted table becomes that stage's active one and the fit is journalled.
Artifacts already scored against an older version keep serving that exact
version through their pinned sha, so a refit never rewrites served math.
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
    BAND_SERVED_STAGES,
    BAND_STAGES,
    DEFAULT_INTERVAL_ALPHA,
    MIN_QUALIFYING_DAYS,
    refit_all_stages,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--axis",
        choices=("claim", "centre_distance"),
        help=(
            "refit every stage on this axis (charter 105).  Omit to refit"
            " each stage on the axis it is already serving, which is what"
            " the nightly sweep does -- moving a table's axis is a decision"
            " somebody makes on purpose, not something a refit does by"
            " defaulting."
        ),
    )
    parser.add_argument(
        "--stage-axis",
        action="append",
        default=[],
        metavar="STAGE=AXIS",
        help=(
            "move ONE stage's axis, e.g. --stage-axis P1=centre_distance."
            "  The evidence for the move is per stage -- on 2026-09-09 P1"
            " cleared all five distance cells on the forward window while"
            " P0-M tied at two either way -- so a global --axis would have"
            " moved P0-M on P1's evidence.  Repeatable; unlisted stages keep"
            " the axis they serve.  Takes precedence over --axis for the"
            " stages it names."
            "  P0-M moved on 2026-09-11 (user ruling), which retires the"
            " 2026-09-09 tie and leaves no stage on the claim axis.  The tie"
            " was read off this module's own forward diagnostic, whose"
            " window overlaps the days the factors were fitted on, and the"
            " claim axis has eleven cells against the distance axis's five:"
            " re-measured through ``until_event_date`` -- the fit blind to"
            " the days it is scored on -- the distance axis won four of six"
            " cuts and the claim axis scored WORSE THAN IDENTITY on four of"
            " them (-0.43, -1.10, -0.75, -0.25 against +3.43, +2.00, +1.91,"
            " -0.02).  A tie on an in-sample number is not a tie."
        ),
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument(
        "--interval-alpha",
        type=float,
        default=DEFAULT_INTERVAL_ALPHA,
        help=(
            "counterfactual width of the by-day bootstrap interval whose end"
            " nearest one is served.  The default is the shipped 95%% and is"
            " the only value that can be committed; a wider alpha pulls the"
            " served factor toward the point estimate, which is the question"
            " 'is the table too conservative' asked through production's own"
            " coordinate instead of a replay's.  Report-only by construction:"
            " passing it with --commit is refused."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="activate each stage's fitted table (default: report only)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="also print one plain-language SUMMARY line per stage",
    )
    args = parser.parse_args()
    axis: "str | dict[str, str] | None" = args.axis
    if args.stage_axis:
        per_stage = {} if args.axis is None else {
            stage: args.axis for stage in BAND_STAGES
        }
        for item in args.stage_axis:
            stage, _, value = str(item).partition("=")
            if stage not in BAND_STAGES or value not in (
                "claim", "centre_distance"
            ):
                parser.error(f"bad --stage-axis {item!r}")
            per_stage[stage] = value
        axis = per_stage
    database = args.db.expanduser().resolve(strict=True)
    if args.commit:
        conn = sqlite3.connect(str(database), timeout=60.0)
    else:
        conn = sqlite3.connect(
            f"file:{database}?mode=ro", uri=True, timeout=60.0
        )
    conn.execute("PRAGMA busy_timeout=60000")
    stages = refit_all_stages(
        conn,
        bootstrap_samples=args.bootstrap_samples,
        commit=args.commit,
        axis=axis,
        interval_alpha=args.interval_alpha,
    )
    activated = [
        stage
        for stage, entry in stages.items()
        if (entry.get("commit") or {}).get("state") == "ACTIVATED"
    ]
    # Stages are fitted in isolation, so a run can be part success.  It is
    # still a failed run -- the exit code says so -- but the stages that
    # landed are reported rather than thrown away with the one that did not.
    failed = sorted(
        stage for stage, entry in stages.items() if entry.get("error")
    )
    print(json.dumps({
        "schema": "band_reliability_refit_report_v4",
        "stages": stages,
        "activated_stages": activated,
        "failed_stages": failed,
        "committed": bool(args.commit),
        "interval_alpha": float(args.interval_alpha),
        # Loud in the receipt, because a counterfactual report and a real one
        # are otherwise the same shape: a reader who does not look at this
        # would take a wider-alpha table for the one being served.
        "counterfactual": bool(
            abs(float(args.interval_alpha) - DEFAULT_INTERVAL_ALPHA) > 1e-12
        ),
    }, ensure_ascii=False, sort_keys=True))
    if args.summary:
        for stage, entry in sorted(stages.items()):
            if entry.get("error"):
                # Named in the same digest the good stages land in, so the
                # cron log shows which stage was lost, not just that one was.
                error = entry["error"]
                print(f"SUMMARY {stage}: 本次未拟合 [FAILED] {error}")
                continue
            state = (entry.get("commit") or {}).get("state", "REPORT_ONLY")
            if stage not in BAND_SERVED_STAGES:
                # Charter 150: retired, not fitted, so the entry carries no
                # fit to summarise.  Read as one on 2026-09-18T20:16Z, the
                # KeyError failed the whole calibration_refit sweep after
                # every table had already been written.
                print(f"SUMMARY {stage}: 宪章150起不服务，不拟合 [{state}]")
                continue
            served = " ".join(
                f"{band[0]:.2f}-{band[1]:.2f}x{factor:.3f}"
                for band, factor in entry["fitted_factors"]
                if factor != 1.0
            )
            days = entry["serving_day_count"]
            if entry["accumulating"]:
                detail = (
                    f"攒证据中 {days}/{MIN_QUALIFYING_DAYS} 天，暂不动系数"
                )
            elif served:
                detail = f"{days} 天证据，生效 {served}"
            else:
                detail = f"{days} 天证据，尚无档位够格，全档恒等"
            print(f"SUMMARY {stage}: {detail} [{state}]")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
