#!/usr/bin/env python3
"""Fit how much of today's observed forecast error carries to today's maximum.

This is the one layer allowed to read the model's level, and it reads it once.
Within a single day a forecast error persists: a model running two degrees cold
at nine in the morning is usually still running cold at three in the afternoon,
because the error is mostly in the air mass it started from rather than in the
diurnal shape it applies to it.  That persistence is information about the
maximum, and nothing else in the four-layer chain is looking at it.

The quantity fitted is a single slope per hours-left bin:

    delta = beta(h) * D,   D = mean(observed - curve) over the last three hours

and the shift is applied to the root by translating it along the temperature
axis, not by multiplying a likelihood into it.  That distinction is the whole
reason this layer is separable.  A likelihood built from D would be a second
use of the same model output that the root already carries -- the posterior
would sharpen on one piece of evidence counted twice, and the ablation could
not be run without refitting.  As a translation of the prior it is used once,
and setting beta to zero recovers the unshifted root exactly, which is the arm
the acceptance run scores against.

No intercept.  A day on which the reports agree with the curve is a day with
no evidence of bias, and a fitted constant would move every root on every such
day in whatever direction the window happened to lean.

    python3 scripts/fit_metar_reanchor_artifact.py \
        --samples data/runtime/bayes_basket_target_nwp/metar_phase_hazard_samples.db \
        --through 2026-09-01 \
        --out data/runtime/bayes_basket_target_nwp/metar_reanchor_artifact.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402

SCHEMA = "bayes_basket_target_metar_reanchor_v1"
# Coarser than the hazard chain's bins: a slope needs more rows than a rate,
# and the thing being estimated changes slowly through the day.
HOURS_EDGES = (0.0, 3.0, 6.0, 9.0, 12.0, 16.0, 20.0, 1e9)
MIN_BIN_ROWS = 300
# Three degrees is already twice the fleet's daily forecast error.  Past it the
# reports and the curve disagree about something other than the air mass -- a
# station whose curve came from the wrong run, a report that has not been
# quality-flagged yet -- and translating a whole prior on that is how one bad
# report becomes one bad day's book.
MAX_SHIFT_C = 3.0
# A bias read off one report is mostly that report's own rounding.
MIN_BIAS_REPORTS = 2


def hours_bin(hours: float) -> int:
    for index in range(len(HOURS_EDGES) - 1):
        if HOURS_EDGES[index] <= hours < HOURS_EDGES[index + 1]:
            return index
    return len(HOURS_EDGES) - 2


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def load_rows(
    path: Path, *, through: str, since: str | None
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    clause = (
        "WHERE event_date<=? AND root_bias_recent_c IS NOT NULL"
        " AND root_center_c IS NOT NULL AND root_bias_report_count>=?"
    )
    params: list[Any] = [through, MIN_BIAS_REPORTS]
    if since:
        clause += " AND event_date>=?"
        params.append(since)
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM metar_phase_hazard_sample {clause}", params
        )
    ]


def fit_slope(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Weighted least squares through the origin, plus what it is worth.

    ``residual_reduction`` is the honest headline: the share of the root's
    squared error this slope removes on its own rows.  A slope with a
    confident-looking value and no reduction behind it is a slope that should
    not ship, and the number is on the artifact so that is visible without
    rerunning anything.
    """

    numerator = 0.0
    denominator = 0.0
    weight_total = 0.0
    for row in rows:
        bias = float(row["root_bias_recent_c"])
        error = float(row["truth_tmax_c"]) - float(row["root_center_c"])
        weight = float(row["station_day_weight"])
        numerator += weight * bias * error
        denominator += weight * bias * bias
        weight_total += weight
    slope = numerator / denominator if denominator > 1e-9 else 0.0
    before = 0.0
    after = 0.0
    for row in rows:
        bias = float(row["root_bias_recent_c"])
        error = float(row["truth_tmax_c"]) - float(row["root_center_c"])
        weight = float(row["station_day_weight"])
        shift = max(-MAX_SHIFT_C, min(MAX_SHIFT_C, slope * bias))
        before += weight * error * error
        after += weight * (error - shift) ** 2
    return {
        "slope": slope,
        "row_n": len(rows),
        "weight": weight_total,
        "root_rmse_before_c": (before / weight_total) ** 0.5 if weight_total else 0.0,
        "root_rmse_after_c": (after / weight_total) ** 0.5 if weight_total else 0.0,
        "residual_reduction": (
            1.0 - after / before if before > 1e-12 else 0.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", required=True)
    parser.add_argument("--through", required=True)
    parser.add_argument("--since", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fitted-at-utc", default=None)
    args = parser.parse_args()

    rows = load_rows(
        Path(args.samples), through=args.through, since=args.since
    )
    if len(rows) < 1000:
        raise SystemExit(f"only {len(rows)} usable rows; refusing to fit")
    by_bin: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_bin.setdefault(
            hours_bin(float(row["remaining_hours"])), []
        ).append(row)
    pooled = fit_slope(rows)
    bins: dict[str, Any] = {}
    for index in range(len(HOURS_EDGES) - 1):
        bucket = by_bin.get(index, [])
        if len(bucket) < MIN_BIN_ROWS:
            # Too thin to carry its own slope.  It takes the pooled one and
            # says so, rather than publishing a number three hundred rows
            # could not support.
            bins[str(index)] = {
                **pooled, "backed_off": True, "row_n": len(bucket),
            }
            continue
        bins[str(index)] = {**fit_slope(bucket), "backed_off": False}

    body = {
        "schema": SCHEMA,
        "fitted_at_utc": args.fitted_at_utc or _iso(datetime.now(timezone.utc)),
        "trained_from": min(row["event_date"] for row in rows),
        "trained_through": max(row["event_date"] for row in rows),
        "hours_edges": list(HOURS_EDGES),
        "max_shift_c": MAX_SHIFT_C,
        "minimum_bias_reports": MIN_BIAS_REPORTS,
        "minimum_bin_rows": MIN_BIN_ROWS,
        "station_n": len({row["station_id"] for row in rows}),
        "station_day_n": len({
            (row["station_id"], row["event_date"]) for row in rows
        }),
        "row_n": len(rows),
        "pooled": pooled,
        "bins": bins,
    }
    body["content_sha256"] = content_sha256(body)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(body, indent=2, sort_keys=True))
    print(json.dumps({
        "schema": SCHEMA,
        "row_n": body["row_n"],
        "pooled_slope": round(pooled["slope"], 4),
        "pooled_residual_reduction": round(pooled["residual_reduction"], 4),
        "out": str(out_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
