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

Since 2026-09-17 this runs on a clock (``delta_v2_authority`` hosted sweep,
weekly through ``--if-older-than-days``) and mints a year of validity.  The
batch it commits starts HANDOFF_LEAD_MINUTES ahead and the policies are
re-pinned to start at that same instant, so the new numbers and the pin that
accepts them arrive together.  The rules live in
``bayes_basket_target.delta_v2_authority_clock``.
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
from bayes_basket_target.delta_v2_artifact import (  # noqa: E402
    ACTIVATION_SCHEMA,
    NO_LIVE_ACTIVATION_DECISION,
    PROMOTION_SCHEMA,
    commit_delta_v2_authority,
)
from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    MAKER_LADDER_STEPS,
)
from bayes_basket_target.delta_v2_fill_artifact import (  # noqa: E402
    REQUIRED_BASELINES,
)
from bayes_basket_target.delta_v2_authority_clock import (  # noqa: E402
    HANDOFF_LEAD_MINUTES,
    VALIDITY_DAYS,
    days_between,
    newest_fill_batch_start,
    repin_policies,
    utc_text,
    write_fill_batch,
)
from scripts.maker_shadow_store import attach_maker_shadow  # noqa: E402

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
# The shadow book has no retention and grows by ~160k resolved intents a day,
# so a fit over its whole history gets slower every week and weights August's
# books like today's.  Four weeks is enough days for the 20% holdout to keep
# its two forward windows many times over.
FIT_WINDOW_DAYS = 28
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


def fit(conn: sqlite3.Connection, *, since_utc: str | None = None) -> dict:
    # Streamed and counted per (day, cell) rather than fetched and sorted:
    # 4.78M resolved intents on 2026-09-17, which as a list of tuples is
    # gigabytes in the resident's machine.  The fit only ever needed the
    # per-cell counts either side of a day cut, so the counts are all that is
    # kept.  ``since_utc`` rides the (state, expires_at_utc) index.
    window = "" if since_utc is None else " AND expires_at_utc>=?"
    cursor = conn.execute(
        "SELECT limit_price, horizon_minutes, state, substr(placed_at_utc,1,10),"
        " price_rule, reference_bid, reference_tick"
        " FROM maker_shadow_order"
        f" WHERE state IN ('FILLED','EXPIRED'){window}",
        () if since_utc is None else (since_utc,),
    )
    resolved = 0
    every_day: set[str] = set()
    counts: dict[tuple[str, tuple], list[int]] = defaultdict(lambda: [0, 0])
    while True:
        chunk = cursor.fetchmany(50_000)
        if not chunk:
            break
        for price, horizon, state, day, rule, ref_bid, ref_tick in chunk:
            resolved += 1
            every_day.add(str(day))
            band = _band(price)
            if band is None:
                continue
            rung = _rung_band(rule, price, ref_bid, ref_tick)
            if rung is None:
                continue
            cell = counts[(str(day), (band, int(horizon), rung))]
            cell[0] += 1
            if str(state) == "FILLED":
                cell[1] += 1
    if not resolved:
        raise SystemExit("maker shadow book has no resolved intents")
    # Cut on a day boundary, not on a row index.  A mid-day cut puts the
    # same date in both windows, and the artifact contract requires
    # ``train_end < holdout_start`` strictly -- correctly, since a day split
    # across the two is not a holdout of anything.
    all_days = sorted(every_day)
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
    # [placed, filled] per cell either side of the cut.
    train: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    holdout: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    holdout_days: set[str] = set()
    train_days: set[str] = set()
    for (day, key), (placed, filled) in counts.items():
        side = train if day < cut_at else holdout
        (train_days if day < cut_at else holdout_days).add(day)
        side[key][0] += placed
        side[key][1] += filled
    fitted: dict[tuple, dict] = {}
    for key, (placed, filled) in train.items():
        if not placed or key not in holdout or not holdout[key][0]:
            continue
        rate = filled / placed
        held_n, held_filled = holdout[key]
        observed = held_filled / held_n
        errors = {
            "v1": abs(INCUMBENT_FLAT_RATE - observed),
            "EV_ONLY": abs(INCUMBENT_FLAT_RATE - observed),
            "ZERO_WEIGHT": abs(0.0 - observed),
            "HOLD": abs(0.0 - observed),
        }
        fitted[key] = {
            "rate": rate,
            "sample_n": placed,
            "holdout_n": held_n,
            "holdout_rate": observed,
            "absolute_error": abs(rate - observed),
            "baseline_absolute_error_v1": errors["v1"],
            "beats_every_baseline": all(
                abs(rate - observed) <= value + 1e-12
                for value in errors.values()
            ),
        }
    return {
        "resolved_intents": resolved,
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


def mint(
    fitobj: dict, *, now: datetime, starts_at: datetime | None = None
) -> list[dict]:
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
    trained_through = utc_text(now)
    valid_from = utc_text(starts_at or now + timedelta(minutes=1))
    valid_until = utc_text(now + timedelta(days=VALIDITY_DAYS))
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
# The candidate ceiling has to be derived from the grids above, not chosen.
# It was 16 -- a number from when the clip grid held two entries, one timeout
# and one rung, so a solve produced two candidates.  The grids have grown
# since: eleven clips, four timeouts, eight ladder rungs, across a window the
# ledger has never seen wider than four buckets.  That is 11 x 4 x 8 x 4 =
# 1,408 MAKER candidates plus one FOK family per clip per bucket, and a
# ceiling of 16 refuses every one of them.
#
# The refusal has never been seen in production because production has never
# reached this code -- it blocks upstream first -- so a stale ceiling looked
# like a working one.  It is not: it is a trap laid for the first cycle that
# gets far enough to price a basket.
#
# Derived, so growing a grid moves it and nobody has to remember.
POLICY_MAX_SUPPORT_BUCKETS = 4
POLICY_LADDER_RUNGS = MAKER_LADDER_STEPS
POLICY_MAXIMUM_CANDIDATES = POLICY_MAX_SUPPORT_BUCKETS * len(
    POLICY_CASH_CLIP_GRID
) * (POLICY_LADDER_RUNGS * len(POLICY_TIMEOUT_GRID_SECONDS) + 1)


def _remint_policy(db: Path, *, now: datetime) -> int:
    """Re-mint the policy artifacts onto the measured execution grids.

    Artifact, promotion and activation together.  Minting an artifact alone
    changes nothing: the resolver reaches a policy through
    ``basket_delta_v2_policy_activation``, which names an artifact_id, so an
    unbound artifact is a file nobody opens.  Verified on 2026-08-22 -- the
    grids re-minted that morning were still resolving to the old two-clip,
    one-timeout policy hours later, because no activation had moved.

    Append-only throughout, at generation + 1, which is what the resolver
    orders on.  The previous generation stays as the record of what served
    before, and its validity window is left alone: the newer generation wins
    the ORDER BY, so nothing has to be retracted.
    """

    conn = sqlite3.connect(str(db), timeout=60.0)
    attach_maker_shadow(conn, ssot_path=db)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_from = (
        now + timedelta(minutes=1)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    valid_until = utc_text(now + timedelta(days=VALIDITY_DAYS))
    written = 0
    # One row per scope, the newest generation of it.  Iterating every
    # activation instead meant a scope already on generation 2 produced
    # generation 2 again and collided with itself on the uniqueness
    # constraint over (scope, generation).
    for activation in conn.execute(
        "SELECT * FROM basket_delta_v2_policy_activation AS outer"
        " WHERE generation = ("
        "   SELECT MAX(generation) FROM basket_delta_v2_policy_activation"
        "   WHERE station_id=outer.station_id AND stage=outer.stage"
        "     AND season_bin=outer.season_bin"
        "     AND maturity_bin=outer.maturity_bin"
        "     AND liquidity_bin=outer.liquidity_bin"
        "     AND strategy_version=outer.strategy_version"
        "     AND authority_scope=outer.authority_scope)"
        " ORDER BY station_id,maturity_bin,strategy_version"
    ).fetchall():
        artifact_row = conn.execute(
            "SELECT * FROM basket_delta_v2_policy_artifact WHERE artifact_id=?",
            (activation["artifact_id"],),
        ).fetchone()
        if artifact_row is None:
            continue
        payload = json.loads(artifact_row["artifact_json"])
        weights = dict(payload.get("execution_weights") or {})
        if (
            payload.get("cash_clip_grid") == POLICY_CASH_CLIP_GRID
            and payload.get("timeout_grid_seconds")
            == POLICY_TIMEOUT_GRID_SECONDS
            and int(weights.get("maximum_candidates", 0))
            == POLICY_MAXIMUM_CANDIDATES
        ):
            continue
        updated = dict(payload)
        updated["cash_clip_grid"] = POLICY_CASH_CLIP_GRID
        updated["timeout_grid_seconds"] = POLICY_TIMEOUT_GRID_SECONDS
        updated["execution_weights"] = {
            **weights,
            "maximum_candidates": POLICY_MAXIMUM_CANDIDATES,
        }
        updated["trained_through_utc"] = stamp
        updated["valid_from_utc"] = valid_from
        updated["valid_until_utc"] = valid_until
        updated.pop("content_sha256", None)
        base = str(activation["artifact_id"]).split("-shadow")[0]
        # The id has to move when the content does.  A fixed "-shadowexec"
        # suffix made the second re-mint of a scope collide with the first on
        # the primary key, where INSERT OR IGNORE dropped it silently while
        # the counter still claimed it -- a re-mint that reported twelve
        # artifacts and changed nothing.  Naming the id after the content
        # makes a genuinely new policy a new row and an unchanged one a no-op.
        identity = content_sha256({
            key: value
            for key, value in updated.items()
            if key not in {
                "artifact_id", "valid_from_utc", "trained_through_utc",
            }
        })
        updated["artifact_id"] = base + "-shadowexec" + identity[:12]
        updated["content_sha256"] = content_sha256(updated)
        promotion_id = "delta-v2-policy-no-live-" + updated["artifact_id"]
        promotion = {
            "schema": PROMOTION_SCHEMA,
            "promotion_id": promotion_id,
            "artifact_id": updated["artifact_id"],
            "decision_seq": 1,
            "decision": NO_LIVE_ACTIVATION_DECISION,
            "gate_version": updated["oos_gate_version"],
            "decided_at_utc": stamp,
            "receipt_sha256": content_sha256({
                "artifact_id": updated["artifact_id"],
                "artifact_content_sha256": updated["content_sha256"],
                "decision": NO_LIVE_ACTIVATION_DECISION,
                "gate_version": updated["oos_gate_version"],
                "decided_at_utc": stamp,
            }),
        }
        # Both rows carry the hash of themselves, and the validator recomputes
        # it over exactly the fields it just parsed -- so the hash is taken
        # after the payload is complete and before content_sha256 is added to
        # it, which is what the validator sees.
        promotion["content_sha256"] = content_sha256(promotion)
        generation = int(activation["generation"]) + 1
        activation_payload = {
            "schema": ACTIVATION_SCHEMA,
            "activation_id": (
                str(activation["activation_id"]).split("-shadow")[0]
                + "-shadowexec" + identity[:12]
            ),
            "artifact_id": updated["artifact_id"],
            "station_id": activation["station_id"],
            "stage": activation["stage"],
            "season_bin": activation["season_bin"],
            "maturity_bin": activation["maturity_bin"],
            "liquidity_bin": activation["liquidity_bin"],
            "strategy_version": activation["strategy_version"],
            "generation": generation,
            "valid_from_utc": valid_from,
            "valid_until_utc": valid_until,
            "activated_at_utc": stamp,
            "authority_scope": activation["authority_scope"],
        }
        activation_payload["content_sha256"] = content_sha256(
            activation_payload
        )
        receipt = commit_delta_v2_authority(
            conn,
            artifact=updated,
            promotion=promotion,
            activation=activation_payload,
        )
        if receipt.get("state") == "COMMITTED":
            written += 1
    conn.commit()
    conn.close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--commit", action="store_true",
        help=(
            "write the batch, starting HANDOFF_LEAD_MINUTES from now, and"
            " re-pin every policy to start at that same instant"
        ),
    )
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
    parser.add_argument(
        "--window-days", type=float, default=FIT_WINDOW_DAYS,
        help="fit on intents resolved inside this many days (0: whole book)",
    )
    parser.add_argument(
        "--if-older-than-days", type=float, default=None,
        help=(
            "do nothing unless the newest fill batch started at least this"
            " many days ago -- the weekly clock the hosted sweep runs on"
        ),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60.0)
    attach_maker_shadow(conn, ssot_path=database)
    now = datetime.now(timezone.utc)
    if args.if_older_than_days is not None:
        newest = newest_fill_batch_start(conn, now=now)
        if newest is not None and (
            -days_between(now, newest) < args.if_older_than_days
        ):
            print(json.dumps({
                "schema": "delta_v2_fill_shadow_fit_v1",
                "skipped": "newest batch is younger than the refit clock",
                "newest_batch_start_utc": newest,
                "refit_every_days": args.if_older_than_days,
            }, ensure_ascii=False, sort_keys=True))
            return 0
    since = (
        utc_text(now - timedelta(days=args.window_days))
        if args.window_days else None
    )
    fitobj = fit(conn, since_utc=since)
    starts_at = now + timedelta(minutes=HANDOFF_LEAD_MINUTES)
    artifacts = mint(fitobj, now=now, starts_at=starts_at)
    by_basis = defaultdict(int)
    for row in artifacts:
        by_basis[str(row["timeout_seconds"])] += 1
    rates = [
        row["maker_distribution"]["fill_probability_full"] for row in artifacts
    ]
    report = {
        "schema": "delta_v2_fill_shadow_fit_v1",
        "resolved_intents": fitobj["resolved_intents"],
        "window_since_utc": since,
        "train_days": [fitobj["train_days"][0], fitobj["train_days"][-1]],
        "holdout_days": [
            fitobj["holdout_days"][0], fitobj["holdout_days"][-1]
        ],
        "price_rule": PRODUCTION_PRICE_RULE,
        "artifact_count": len(artifacts),
        "valid_from_utc": utc_text(starts_at),
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
    rc = 0
    if args.commit:
        conn.close()
        writer = sqlite3.connect(str(database), timeout=60.0)
        writer.execute("PRAGMA busy_timeout=60000")
        written = write_fill_batch(
            writer, artifacts,
            promoted_at=utc_text(datetime.now(timezone.utc)),
        )
        report["committed"] = True
        report["rows_written"] = written["rows_written"]
        if datetime.now(timezone.utc) >= starts_at:
            # The rows went in after their own start, so a reader may have
            # resolved the batch half-written.  Loud, and still re-pinned:
            # a batch that has started with no policy pinning it refuses
            # every candidate, which is worse.
            report["handoff_late"] = True
            rc = 1
        report["policies"] = repin_policies(
            writer, now=now, commit=True, handoff_at=starts_at,
            log=lambda line: print(line, file=sys.stderr),
        )
        writer.close()
    # Outside --commit, deliberately.  The policy artifacts are a handful of
    # rows and the fill artifacts are tens of thousands, so nesting the
    # re-mint inside the fit meant changing one number in the policy cost a
    # full re-mint of every fill artifact.  On 2026-08-22 that is exactly
    # what happened: thirteen passes to get past the artifact contract took
    # the table from 7,291 rows to 521,865, pushed the store past its size
    # bound, and production wrote no decision for 6.8 hours.  The two are now
    # independent, so --remint-policy alone re-mints only the policy.
    if args.remint_policy:
        report["policy_artifacts_reminted"] = _remint_policy(
            database, now=now
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
