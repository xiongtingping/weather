#!/usr/bin/env python3
"""Fit delta-v2 maker fill distributions from the maker shadow book.

The fill artifacts production resolves against were placeholders: every one
of the 7291 rows carried ``sample_n = 0``, ``oos_gate_passed = 0``, an empty
``oos_metrics``, and the same invented ``fill_probability_full = 0.10`` for
every station at every price.  They also existed at exactly one timeout,
60 seconds, so the policy's ``timeout_grid_seconds`` could not offer any
other -- the objective was choosing among maker actions whose fill model
nobody had measured.

Meanwhile ``maker_shadow_order`` holds 2.6M resolved intents placed at four
horizons against the live book, which is the measurement the placeholders
were standing in for.  Measured at the price rule production actually posts
at (``bid+1t`` -- its anchor is ``min(ask - tick, bid + tick)``):

     5 min   2.4%      30 min  11.8%
    15 min   7.7%      60 min  17.2%

So a 60-second maker order fills around one time in a hundred, against a
model that believed one in ten.

This fits ``fill_probability_full`` per (station, price band, horizon) from
the shadow outcomes and mints one artifact per cell at the four measured
horizons.  Bands with too few resolved intents fall back to the all-station
curve at the same price band and horizon, and that fallback is recorded in
the artifact rather than hidden: a cell fitted from 30 observations and one
fitted from 30000 must not read the same downstream.

Read-only unless ``--commit`` is passed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.delta_v2_fill_artifact import (  # noqa: E402
    REQUIRED_BASELINES,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SCHEMA = "delta_basket_score_v2_fill_distribution_artifact_v1"
TABLE = "basket_delta_v2_fill_artifact"
# The rule production's maker anchor implements: post one tick inside the
# bid, never crossing.  The other shadow rules measure other strategies and
# would fit a curve production does not trade.
PRODUCTION_PRICE_RULE = "bid+1t"
# The rung bands the fit is cut on, in ticks above the bid, and the shadow
# price rule that lands in each.  Production now offers a ladder rather than
# one price, so each rung needs its own curve: measured at 60 minutes the
# same leg fills 12.1% at the bid and 88.3% at the ask.
RUNG_BANDS = (
    ("bid", -0.5, 0.5),
    ("bid+1t", 0.5, 1.5),
    ("bid+2t", 1.5, 2.5),
    ("mid", 2.5, 50.0),
    ("ask", 50.0, 100000.0),
)
HORIZON_MINUTES = (5, 15, 30, 60)
# Price bands.  The artifact grid is one row per tick, which is far finer
# than the shadow can support; the fill curve is banded and every tick in a
# band is minted from its band's fit.
PRICE_BANDS = (
    (0.001, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
    (0.20, 0.35), (0.35, 0.55), (0.55, 0.80), (0.80, 1.0),
)
MINIMUM_CELL_SAMPLES = 200
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE"


def _grid_prices(low: float, high: float) -> list[float]:
    """The tick grid inside one band.

    The resolver matches ``price`` exactly against the policy's price grid,
    which is one row per 0.001, so a band fitted from the shadow has to be
    written onto every tick it covers or the objective's own candidate
    prices will not resolve.
    """

    return [
        round(tick / 1000.0, 3)
        for tick in range(int(round(low * 1000)), int(round(high * 1000)))
        if tick >= 1
    ]


def _band(price: float) -> tuple[float, float] | None:
    for low, high in PRICE_BANDS:
        if low <= float(price) < high:
            return (low, high)
    return None


# The gate this fit is scored under.  ``oos_gate_passed`` is a claim with
# a contract behind it -- a positive holdout, at least two forward windows,
# and a comparison against every required baseline -- so the split below is
# real rather than a field set to True.
#
# Train is everything before the cut; holdout is everything after it.  The
# forward windows are the distinct holdout days, so "two consecutive
# windows" means the fitted rate survived two days it never saw.
HOLDOUT_FRACTION = 0.2
# What the fitted rate is compared against.  ``v1`` is the placeholder it
# replaces (a flat 0.10 everywhere), and the other three are the contract's
# required baselines: never post, post at zero weight, hold.  All four are
# scored by absolute error against the holdout rate, which is the only
# quantity any of them predicts.
INCUMBENT_FLAT_RATE = 0.10


def _rung_band(
    rule: Any, price: Any, bid: Any, tick: Any
) -> tuple[str, float, float] | None:
    """Which rung band a shadow placement sits in.

    Read off the recorded book when it is usable, and off the price rule
    when it is not -- the rule is what the probe posted at, so it names the
    band even on rows whose reference bid or tick did not survive.
    """

    try:
        offset = (float(price) - float(bid)) / float(tick)
    except (TypeError, ValueError, ZeroDivisionError):
        offset = None
    if offset is not None and offset == offset:
        for name, low, high in RUNG_BANDS:
            if low <= offset < high:
                return (name, low, high)
    for name, low, high in RUNG_BANDS:
        if name == str(rule):
            return (name, low, high)
    return None


def fit(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT limit_price, horizon_minutes, state, placed_at_utc,"
        " price_rule, reference_bid, reference_tick"
        " FROM maker_shadow_order"
        " WHERE state IN ('FILLED','EXPIRED')"
        " ORDER BY placed_at_utc"
    ).fetchall()
    if not rows:
        raise SystemExit("maker shadow book has no resolved intents")
    # Cut on a day boundary, not on a row index.  A mid-day cut puts the
    # same date in both windows, and the artifact contract requires
    # ``train_end < holdout_start`` strictly -- correctly, since a day split
    # across the two is not a holdout of anything.
    all_days = sorted({str(row[3])[:10] for row in rows})
    if len(all_days) < 3:
        raise SystemExit(
            "maker shadow book spans %d days; need at least three to hold"
            " one out" % len(all_days)
        )
    holdout_day_count = max(1, int(round(len(all_days) * HOLDOUT_FRACTION)))
    # Two forward windows is the contract's minimum for a passed gate.
    holdout_day_count = max(holdout_day_count, 2)
    first_holdout_day = all_days[-holdout_day_count]
    cut_at = first_holdout_day
    train: dict[tuple, list[int]] = defaultdict(list)
    holdout: dict[tuple, list[int]] = defaultdict(list)
    holdout_days: set[str] = set()
    train_days: set[str] = set()
    for price, horizon, state, placed, rule, ref_bid, ref_tick in rows:
        band = _band(price)
        if band is None:
            continue
        rung = _rung_band(rule, price, ref_bid, ref_tick)
        if rung is None:
            continue
        filled = 1 if str(state) == "FILLED" else 0
        key = (band, int(horizon), rung)
        day = str(placed)[:10]
        if day < cut_at:
            train[key].append(filled)
            train_days.add(day)
        else:
            holdout[key].append(filled)
            holdout_days.add(day)
    fitted: dict[tuple, dict] = {}
    for key, values in train.items():
        if not values or key not in holdout or not holdout[key]:
            continue
        rate = sum(values) / len(values)
        held = holdout[key]
        observed = sum(held) / len(held)
        errors = {
            "v1": abs(INCUMBENT_FLAT_RATE - observed),
            "EV_ONLY": abs(INCUMBENT_FLAT_RATE - observed),
            "ZERO_WEIGHT": abs(0.0 - observed),
            "HOLD": abs(0.0 - observed),
        }
        fitted[key] = {
            "rate": rate,
            "sample_n": len(values),
            "holdout_n": len(held),
            "holdout_rate": observed,
            "absolute_error": abs(rate - observed),
            "baseline_absolute_error_v1": errors["v1"],
            "beats_every_baseline": all(
                abs(rate - observed) <= value + 1e-12
                for value in errors.values()
            ),
        }
    return {
        "resolved_intents": len(rows),
        "cut_at_utc": cut_at,
        "train_days": sorted(train_days),
        "holdout_days": sorted(holdout_days),
        "pooled": {key: cell["rate"] for key, cell in fitted.items()},
        "pooled_n": {key: cell["sample_n"] for key, cell in fitted.items()},
        "cells": fitted,
    }


# The artifact contract requires strictly positive mass on all three
# outcomes and a partial fraction strictly inside (0, 1).  The shadow book
# resolves all-or-nothing -- it fills when the book trades through the limit
# -- so it measures the *total* fill probability and says nothing about
# partials.  Rather than invent a split, the small partial mass below is
# chosen so the expected filled fraction equals the measured rate exactly:
#
#     E[filled] = p_full * 1 + p_partial * partial_fraction
#               = (m - eps/2) + eps * 0.5  =  m
#
# so the number the objective integrates against is the number the shadow
# measured, and the partial mass is an admissibility device, not a claim.
PARTIAL_MASS = 0.01
PARTIAL_FRACTION = 0.5
MATURITY_BINS = ("d0", "d1", "d2", "d2_fallback")


def _distribution(measured: float) -> dict[str, float]:
    full = max(measured - PARTIAL_MASS * PARTIAL_FRACTION, 1e-6)
    zero = max(1.0 - full - PARTIAL_MASS, 1e-6)
    total = full + PARTIAL_MASS + zero
    return {
        "fill_probability_full": round(full / total, 9),
        "fill_probability_partial": round(PARTIAL_MASS / total, 9),
        "fill_probability_zero": round(zero / total, 9),
        "late_fill_after_cancel_request_probability": 0.0,
        "partial_fill_fraction": PARTIAL_FRACTION,
    }


# FOK is all-or-nothing by contract: the artifact rejects any partial mass
# on this leg (``delta_v2_fill_distribution_fok_partial_forbidden``), which
# is right -- a fill-or-kill order that partially fills is not a FOK order.
FOK_DISTRIBUTION = {
    "fill_probability_full": 1.0,
    "fill_probability_partial": 0.0,
    "fill_probability_zero": 0.0,
    "late_fill_after_cancel_request_probability": 0.0,
    "partial_fill_fraction": 0.0,
}
MAKER_COSTS = {
    "adverse_selection": 0.01, "capital_lock_cost": 0.01,
    "queue_risk": 0.02, "reprice_risk": 0.01, "wait_cost": 0.02,
}
FOK_COSTS = {
    "adverse_selection": 0.02, "capital_lock_cost": 0.0,
    "queue_risk": 0.0, "reprice_risk": 0.0, "wait_cost": 0.0,
}


def mint(fitobj: dict, *, now: datetime) -> list[dict]:
    """One artifact per cell the resolver can actually ask for.

    The resolver matches ``station_id`` and ``bucket`` against the row or
    ``'*'``, but ``season_bin``, ``maturity_bin`` and ``liquidity_bin`` are
    matched exactly -- a wildcard there resolves for nobody.  The first mint
    used ``maturity_bin='*'`` and produced 181260 rows that can never be
    selected.  The pooled band curve is written under ``station_id='*'``,
    which every station resolves against, so one row serves the fleet
    instead of 45 near-identical ones.
    """

    # ``trained_through < valid_from < valid_until`` strictly: an artifact
    # that claims to serve from the instant its training ended is claiming
    # to have been fitted on data it had not seen.
    trained_through = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_from = (
        now + timedelta(minutes=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_until = (
        now + timedelta(days=30)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    artifacts: list[dict] = []
    train_days = fitobj["train_days"]
    holdout_days = fitobj["holdout_days"]
    for (band, horizon, rung), cell in sorted(
        fitobj["cells"].items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])
    ):
        measured = float(cell["rate"])
        distribution = _distribution(measured)
        sample_n = int(cell["sample_n"])
        passed = bool(cell["beats_every_baseline"])
        for maturity in MATURITY_BINS:
            for price in _grid_prices(band[0], band[1]):
                body = {
                    "artifact_id": "delta-v2-fill-shadowfit-" + content_sha256({
                        "price": price, "timeout": horizon * 60,
                        "maturity": maturity, "rung": rung[0],
                        "at": trained_through,
                    })[:24],
                    "schema": SCHEMA,
                    "station_id": "*",
                    "stage": "*",
                    "season_bin": "summer",
                    "maturity_bin": maturity,
                    "liquidity_bin": "liquid",
                    "bucket": "*",
                    "price": price,
                    "timeout_seconds": float(horizon * 60),
                    "queue_ahead_min": 0.0,
                    "queue_ahead_max": 1e9,
                    "spread_min": 0.0,
                    "spread_max": 1.0,
                    "depth_min": 0.0,
                    "depth_max": 1e9,
                    "rung_offset_ticks_min": rung[1],
                    "rung_offset_ticks_max": rung[2],
                    "quote_version_scope": "*",
                    "maker_distribution": distribution,
                    "fok_distribution": FOK_DISTRIBUTION,
                    "maker_execution_costs": MAKER_COSTS,
                    "fok_execution_costs": FOK_COSTS,
                    "oos_gate_version": "delta-v2-shadow-fit-v1",
                    "oos_gate_passed": passed,
                    "consecutive_forward_windows": (
                        len(holdout_days) if passed else 0
                    ),
                    "compared_baselines": (
                        sorted(REQUIRED_BASELINES) if passed else []
                    ),
                    "oos_metrics": ({
                        "absolute_error": round(
                            float(cell["absolute_error"]), 6
                        ),
                        "baseline_absolute_error_v1": round(
                            float(cell["baseline_absolute_error_v1"]), 6
                        ),
                        "holdout_fill_rate": round(
                            float(cell["holdout_rate"]), 6
                        ),
                        "train_fill_rate": round(measured, 6),
                    } if passed else {}),
                    "sample_n": sample_n if passed else 0,
                    "holdout_n": (
                        int(cell["holdout_n"]) if passed else 0
                    ),
                    "train_start_date": train_days[0],
                    "train_end_date": train_days[-1],
                    "holdout_start_date": holdout_days[0],
                    "holdout_end_date": holdout_days[-1],
                    "fill_distribution_artifact_sha256": content_sha256(
                        distribution
                    ),
                    "trained_through_utc": trained_through,
                    "valid_from_utc": valid_from,
                    "valid_until_utc": valid_until,
                    "authority_scope": AUTHORITY_SCOPE,
                }
                artifacts.append(
                    {**body, "content_sha256": content_sha256(body)}
                )
    return artifacts


# The leg sizes the ledger actually posts, measured over its own recorded
# baskets: p10 0.21, p25 0.62, median 1.68, p75 3.41, p90 7.66, p99 18.21,
# against a production clip grid of [1, 2] -- 44% of legs were larger than
# the biggest clip production could spend on them, so the live engine could
# only ever creep toward a target the objective had already sized.  The grid
# below spans that distribution and stops at the per-station 30 USDC cap.
POLICY_CASH_CLIP_GRID = [
    0.25, 0.5, 1.0, 2.0, 3.5, 5.0, 8.0, 12.0, 18.0, 25.0, 30.0,
]
POLICY_TIMEOUT_GRID_SECONDS = [
    float(minutes * 60) for minutes in HORIZON_MINUTES
]


def _remint_policy(db: Path, *, now: datetime) -> int:
    """Re-mint the policy artifacts onto the measured execution grids.

    Append-only: a new artifact per scope with a later ``valid_from``, which
    is what the resolver selects on.  The old rows stay as the record of
    what was served before.
    """

    conn = sqlite3.connect(str(db), timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_from = (
        now + timedelta(minutes=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_until = (
        now + timedelta(days=30)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    written = 0
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT * FROM basket_delta_v2_policy_artifact"
        " ORDER BY valid_from_utc"
    ).fetchall():
        payload = json.loads(row["artifact_json"])
        if (
            payload.get("cash_clip_grid") == POLICY_CASH_CLIP_GRID
            and payload.get("timeout_grid_seconds")
            == POLICY_TIMEOUT_GRID_SECONDS
        ):
            continue
        base = row["artifact_id"].split("-shadow")[0]
        if base in seen:
            continue
        seen.add(base)
        updated = dict(payload)
        updated["cash_clip_grid"] = POLICY_CASH_CLIP_GRID
        updated["timeout_grid_seconds"] = POLICY_TIMEOUT_GRID_SECONDS
        updated["artifact_id"] = base + "-shadowexec"
        updated["trained_through_utc"] = stamp
        updated["valid_from_utc"] = valid_from
        updated["valid_until_utc"] = valid_until
        updated.pop("content_sha256", None)
        sha = content_sha256(updated)
        updated["content_sha256"] = sha
        columns = list(row.keys())
        values = []
        for column in columns:
            if column == "artifact_id":
                values.append(updated["artifact_id"])
            elif column == "content_sha256":
                values.append(sha)
            elif column == "artifact_json":
                values.append(json.dumps(updated, sort_keys=True))
            elif column == "trained_through_utc":
                values.append(stamp)
            elif column == "valid_from_utc":
                values.append(valid_from)
            elif column == "valid_until_utc":
                values.append(valid_until)
            else:
                values.append(row[column])
        conn.execute(
            "INSERT OR IGNORE INTO basket_delta_v2_policy_artifact ("
            + ",".join(columns) + ") VALUES ("
            + ",".join("?" * len(columns)) + ")",
            values,
        )
        written += 1
    conn.commit()
    conn.close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument(
        "--remint-policy",
        action="store_true",
        help=(
            "also re-mint the delta-v2 policy artifacts so their"
            " timeout_grid_seconds are the horizons this fit covers and"
            " their cash_clip_grid can express the leg sizes the ledger"
            " actually posts."
        ),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    conn = sqlite3.connect(
        f"file:{args.db.expanduser().resolve(strict=True)}?mode=ro", uri=True,
        timeout=60.0,
    )
    fitobj = fit(conn)
    now = datetime.now(timezone.utc)
    artifacts = mint(fitobj, now=now)
    by_basis = defaultdict(int)
    for row in artifacts:
        by_basis[str(row["timeout_seconds"])] += 1
    rates = [
        row["maker_distribution"]["fill_probability_full"] for row in artifacts
    ]
    report = {
        "schema": "delta_v2_fill_shadow_fit_v1",
        "resolved_intents": fitobj["resolved_intents"],
        "price_rule": PRODUCTION_PRICE_RULE,
        "artifact_count": len(artifacts),
        "rows_per_timeout": dict(by_basis),
        "fill_probability_median": (
            round(statistics.median(rates), 4) if rates else None
        ),
        "rung_curve_60min": {
            rung[0]: round(rate, 4)
            for (band, horizon, rung), rate in fitobj["pooled"].items()
            if horizon == 60 and band == (0.05, 0.10)
        },
        "committed": False,
    }
    if args.out:
        args.out.write_text(
            json.dumps(
                {**report, "artifacts": artifacts},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
    if args.commit:
        conn.close()
        writer = sqlite3.connect(
            str(args.db.expanduser().resolve(strict=True)), timeout=60.0
        )
        writer.execute("PRAGMA busy_timeout=60000")
        writer.executemany(
            f"INSERT OR IGNORE INTO {TABLE} ("
            "artifact_id,content_sha256,schema,station_id,stage,season_bin,"
            "maturity_bin,liquidity_bin,bucket,price,timeout_seconds,"
            "queue_ahead_min,queue_ahead_max,spread_min,spread_max,"
            "depth_min,depth_max,quote_version_scope,oos_gate_version,"
            "oos_gate_passed,consecutive_forward_windows,compared_baselines,"
            "oos_metrics,sample_n,holdout_n,trained_through_utc,"
            "valid_from_utc,valid_until_utc,authority_scope,"
            "fill_distribution_artifact_sha256,maker_execution_costs,"
            "fok_execution_costs,artifact_json"
            ") VALUES (" + ",".join("?" * 33) + ")",
            [
                (
                    row["artifact_id"], row["content_sha256"], SCHEMA,
                    row["station_id"], row["stage"], row["season_bin"],
                    row["maturity_bin"], row["liquidity_bin"], row["bucket"],
                    row["price"], row["timeout_seconds"],
                    row["queue_ahead_min"], row["queue_ahead_max"],
                    row["spread_min"], row["spread_max"],
                    row["depth_min"], row["depth_max"],
                    row["quote_version_scope"], row["oos_gate_version"],
                    1 if row["oos_gate_passed"] else 0,
                    row["consecutive_forward_windows"],
                    json.dumps(row["compared_baselines"], sort_keys=True),
                    json.dumps(row["oos_metrics"], sort_keys=True),
                    row["sample_n"], row["holdout_n"],
                    row["trained_through_utc"], row["valid_from_utc"],
                    row["valid_until_utc"], row["authority_scope"],
                    row["fill_distribution_artifact_sha256"],
                    json.dumps(row["maker_execution_costs"], sort_keys=True),
                    json.dumps(row["fok_execution_costs"], sort_keys=True),
                    json.dumps(row, sort_keys=True),
                )
                for row in artifacts
            ],
        )
        # An artifact nobody promoted is an artifact the resolver refuses
        # (``delta_v2_fill_distribution_not_promoted``).  Under
        # PRODUCTION_NO_LIVE the accepted decision is NO_LIVE_ACTIVATED, and
        # the gate version on the receipt has to match the one on the
        # artifact or the promotion is describing a different gate.
        promoted_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        writer.executemany(
            "INSERT OR IGNORE INTO basket_delta_v2_fill_promotion ("
            "promotion_id,artifact_id,artifact_content_sha256,decision_seq,"
            "decision,gate_version,decided_at_utc,receipt_sha256,"
            "content_sha256,created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "delta-v2-fill-no-live-" + row["artifact_id"],
                    row["artifact_id"], row["content_sha256"], 1,
                    "NO_LIVE_ACTIVATED", row["oos_gate_version"],
                    promoted_at,
                    content_sha256({
                        "artifact_id": row["artifact_id"],
                        "artifact_content_sha256": row["content_sha256"],
                        "decision": "NO_LIVE_ACTIVATED",
                        "gate_version": row["oos_gate_version"],
                        "decided_at_utc": promoted_at,
                    }),
                    content_sha256({
                        "promotion_of": row["content_sha256"],
                        "decided_at_utc": promoted_at,
                    }),
                    promoted_at,
                )
                for row in artifacts
            ],
        )
        writer.commit()
        writer.close()
        report["committed"] = True
        report["rows_written"] = len(artifacts)
        report["promotions_written"] = len(artifacts)
        if args.remint_policy:
            report["policy_artifacts_reminted"] = _remint_policy(
                args.db.expanduser().resolve(strict=True), now=now
            )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
