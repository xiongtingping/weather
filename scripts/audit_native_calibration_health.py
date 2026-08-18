#!/usr/bin/env python3
"""Health check for the two tables P0 serves from, and their pointers.

Serving is a lookup: the correction table holds per-system bias/spread/
reliability, the weight table holds the blend, and the activation table
decides which generation of each is live.  Nothing in those rows says
whether the stored number still deserves trust, so the failures found on
2026-08-15 were all invisible until someone asked by hand:

* a spread factor pinned at a bound that had since been raised, on 46 live
  scopes, because only systems that won a seat were ever refitted;
* a weight row six days older than the correction rows beside it, because
  the cohort selector never offers the cycle the packet actually carries;
* a bias whose stability nobody had checked -- it turned out steady to
  0.06C across halves, but that was luck, not evidence.

Each check reports what is served against what the evidence supports.
Read-only: opens the database in immutable mode and writes nothing.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
LIVE_LEAD_BUCKETS = ("h00_12", "h12_24", "h24_48")
# The forecaster's TX and the METAR physical path keep their own per-system
# lanes but never contest a P0 seat, so they are not part of what the cross
# weights weigh.
POINT_FORECAST_SYSTEM_IDS = (
    "OFFICIAL_AIRPORT_TAF_TX",
    "METAR_PHYSICAL_PATH_TMAX",
)
SPREAD_BOUND = 5.0
# A split-half bias that moves by more than this is not one number any more.
# Scaled to the station's own residual spread rather than a fixed degree so a
# volatile station is not flagged for volatility it always had.
BIAS_DRIFT_SIGMA = 1.0
MIN_SPLIT_SAMPLES = 16
# Two tables, two refresh paths.  A gap this wide means one of them stopped.
STALE_TABLE_GAP_DAYS = 3
# Mirrors native_ensemble_oos; duplicated rather than imported so a read-only
# audit never pulls the serving module's import graph into a cron shell.  If
# they drift, the rates below describe a gate nobody runs -- pinned by test.
BRIER_TOLERANCE_MAX_LOSS = 0.02
BRIER_TOLERANCE_MIN_GAIN = 0.05
# Below this the rates are a fortnight of weather, not a property of the gate.
GATE_RATE_MIN_SAMPLE = 100
# Measured 2026-08-18: NLL is the binding condition and only 6.7% of
# contested candidates are admitted, so Brier alone blocks a small minority.
# Should that minority become the majority, Brier is deciding this path by
# itself and its thresholds are worth revisiting.
GATE_BRIER_ONLY_ALARM_SHARE = 0.50
# How far back the rates look.  Long enough to be a property of the gate,
# short enough to notice a change in it.
GATE_RATE_LOOKBACK_DAYS = 14
# Murphy decomposition of the served Brier.
MURPHY_BIN_COUNT = 12
MURPHY_MIN_PAIRS = 1000
# Measured 2026-08-18: reliability is 0.6% of uncertainty, so the calibration
# layers are close to exhausted and anything approaching this share means one
# of them has come undone rather than that there is work left to do.
MURPHY_RELIABILITY_ALARM_SHARE = 0.03


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _live_activations(conn: sqlite3.Connection, now: str) -> dict:
    """Newest generation per (station, system, season, lead, cycle)."""

    top: dict[tuple, tuple] = {}
    for row in conn.execute(
        "SELECT v.station_id,v.ensemble_system_id_or_star,v.season_bin,"
        "v.lead_bucket,v.run_cycle,v.generation,a.spread_parameters_json,"
        "a.bias_parameters_json,a.train_end_date "
        "FROM calibration_native_ensemble_activation v "
        "JOIN calibration_native_ensemble_system_artifact a "
        "ON a.system_artifact_id=v.artifact_id "
        "WHERE v.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
        "AND v.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
        "AND v.valid_from_utc<=? AND v.valid_until_utc>?",
        (now, now),
    ):
        key = tuple(str(value) for value in row[:5])
        if key not in top or int(row[5]) > int(top[key][5]):
            top[key] = row
    return top


def check_spread_bound(live: dict) -> list[dict]:
    """A spread factor sitting exactly on a bound the evidence exceeds."""

    findings = []
    for key, row in live.items():
        station, system, _season, lead, cycle = key
        if lead not in LIVE_LEAD_BUCKETS:
            continue
        spread = json.loads(str(row[6]))
        served = float(spread.get("spread_factor") or 0.0)
        # The scale the fit actually sizes width by: the residual sigma about
        # the corrected centre, not the RMSE about zero.  Reading the RMSE
        # here after the fit moved to sigma made this check demand the width
        # the bias correction already removed -- 127 of 510 live scopes
        # reported as too narrow on the day the fit changed, every one of
        # them a false alarm.  Artifacts fitted before the change carry no
        # sigma and were genuinely sized by RMSE, so they fall back to it and
        # are still judged by the rule they were built under.
        scale = float(
            spread.get("train_residual_sigma_c")
            or spread.get("train_rmse_c")
            or 0.0
        )
        mean_spread = float(spread.get("train_mean_spread_c") or 0.0)
        if mean_spread <= 0.0:
            continue
        wanted = min(SPREAD_BOUND, max(0.5, scale / mean_spread))
        if wanted - served > 0.05:
            findings.append({
                "check": "spread_below_evidence",
                "station_id": station,
                "ensemble_system_id": system,
                "lead_bucket": lead,
                "run_cycle": cycle,
                "served_spread_factor": round(served, 3),
                "evidence_spread_factor": round(wanted, 3),
                "narrower_by": round(wanted / served, 2) if served else None,
                "train_end_date": str(row[8]),
            })
    return findings


def check_table_freshness(conn: sqlite3.Connection, live: dict) -> list[dict]:
    """The weight row aging while the correction rows beside it advance.

    Three things this must not mistake for staleness, each of which it did:

    Point forecasts advance on their own cadence and, by the 2026-08-15 stage
    ruling, never contest a P0 seat.  Taking them into the correction-side
    maximum compared the blend against systems that are not in it -- 17 of 32
    findings.

    A cohort that seats members from different run cycles is labelled
    ``MIXED_NATIVE_CYCLES``, while the per-system activations underneath it
    keep their own concrete cycles.  Keying the comparison on cycle therefore
    matches a live correction against a *retired* cycle label's last evidence
    and reports a scope that is being evaluated hourly as six days idle.  The
    weight side is read across every label at the lead for that reason.

    A scope whose latest evaluation was rejected is frozen by design -- the
    weights only turn over when a candidate passes the gate.  That is worth
    reporting and is not the same finding, so it is reported as its own.
    """

    correction_end: dict[tuple, str] = {}
    for key, row in live.items():
        station, system, season, lead, _cycle = key
        if lead not in LIVE_LEAD_BUCKETS:
            continue
        if system in POINT_FORECAST_SYSTEM_IDS:
            continue
        scope = (station, season, lead)
        end = str(row[8] or "")
        if end > correction_end.get(scope, ""):
            correction_end[scope] = end
    weight_end: dict[tuple, str] = {}
    for station, season, lead, end in conn.execute(
        "SELECT station_id,season_bin,lead_bucket,MAX(train_end_date) FROM "
        "calibration_cross_native_ensemble_selection_evidence "
        "GROUP BY station_id,season_bin,lead_bucket"
    ):
        weight_end[(str(station), str(season), str(lead))] = str(end or "")
    evaluated_end: dict[tuple, str] = {}
    latest_state: dict[tuple, tuple[str, str]] = {}
    for station, season, lead, end in conn.execute(
        "SELECT station_id,season_bin,lead_bucket,MAX(train_end_date) FROM "
        "calibration_cross_native_ensemble_oos_evaluation "
        "GROUP BY station_id,season_bin,lead_bucket"
    ):
        evaluated_end[(str(station), str(season), str(lead))] = str(end or "")
    for station, season, lead, state, at in conn.execute(
        "SELECT station_id,season_bin,lead_bucket,oos_state,"
        "MAX(evaluated_at_utc) FROM "
        "calibration_cross_native_ensemble_oos_evaluation "
        "GROUP BY station_id,season_bin,lead_bucket"
    ):
        latest_state[(str(station), str(season), str(lead))] = (
            str(state or ""), str(at or "")
        )
    findings = []
    for scope, corrected in sorted(correction_end.items()):
        weighted = weight_end.get(scope)
        evaluated = evaluated_end.get(scope)
        if not weighted or not evaluated:
            continue
        # Like for like.  A cross evaluation reserves a holdout shared
        # exactly across its seated members, so its train necessarily ends
        # earlier than any single member's -- comparing the two measured that
        # offset and called it staleness on every healthy scope.  What the
        # check is actually for is weights failing to turn over while
        # evaluations keep arriving, and that is evidence against evaluation.
        gap = (
            datetime.fromisoformat(evaluated)
            - datetime.fromisoformat(weighted)
        ).days
        if gap < STALE_TABLE_GAP_DAYS:
            continue
        state, evaluated_at = latest_state.get(scope, ("", ""))
        findings.append({
            "check": (
                "weight_table_frozen_by_rejection"
                if state == "OOS_REJECTED"
                else "weight_table_behind_correction_table"
            ),
            "station_id": scope[0],
            "lead_bucket": scope[2],
            "correction_train_end": corrected,
            "evaluated_train_end": evaluated,
            "weight_train_end": weighted,
            "gap_days": gap,
            "latest_cross_oos_state": state or None,
            "latest_cross_evaluated_at_utc": evaluated_at or None,
        })
    return findings


def check_bias_stability(conn: sqlite3.Connection, live: dict) -> list[dict]:
    """Whether a served bias is one number or two glued together.

    Recomputed from the cases rather than read from the row, because the
    row stores only the fit, never its stability.  Split at the median
    date; a bias that moves by more than its own residual sigma between
    halves is not describing a standing offset.
    """

    truth: dict[tuple[str, str], float] = {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT station_id,local_date,official_tmax_c "
            "FROM calibration_station_daily_high_truth"
        )
    }
    served_systems = {
        (key[0], key[1]) for key in live if key[3] in LIVE_LEAD_BUCKETS
    }
    errors: dict[tuple[str, str], list[tuple[str, float]]] = (
        collections.defaultdict(list)
    )
    for station, system, day, mean in conn.execute(
        "SELECT station_id,ensemble_system_id,target_local_date,"
        "AVG(raw_tmax_c) FROM calibration_native_ensemble_member_run "
        "GROUP BY station_id,ensemble_system_id,target_local_date,run_cycle"
    ):
        pair = (str(station), str(system))
        if pair not in served_systems:
            continue
        observed = truth.get((str(station), str(day)))
        if observed is None:
            continue
        errors[pair].append((str(day), observed - float(mean)))
    findings = []
    for pair, rows in sorted(errors.items()):
        if len(rows) < MIN_SPLIT_SAMPLES:
            continue
        rows.sort()
        half = len(rows) // 2
        early = [value for _day, value in rows[:half]]
        late = [value for _day, value in rows[half:]]
        sigma = statistics.pstdev([value for _day, value in rows])
        drift = statistics.fmean(late) - statistics.fmean(early)
        if sigma > 0.0 and abs(drift) > BIAS_DRIFT_SIGMA * sigma:
            findings.append({
                "check": "bias_drifted_between_halves",
                "station_id": pair[0],
                "ensemble_system_id": pair[1],
                "samples": len(rows),
                "early_bias_c": round(statistics.fmean(early), 2),
                "late_bias_c": round(statistics.fmean(late), 2),
                "drift_c": round(drift, 2),
                "residual_sigma_c": round(sigma, 2),
                "split_at": rows[half][0],
            })
    return findings


def gate_condition_rates(
    conn: sqlite3.Connection, *, since: str
) -> dict:
    """Which of the tolerance channel's three conditions is doing the work.

    A candidate that loses Brier to climatology may still pass if its Brier
    loss is small and CRPS *and* NLL both win by a margin.  Reading only
    Brier and CRPS makes that channel look punitive -- it rejects candidates
    whose CRPS beats climatology by 9% over a 3% Brier loss -- and reading
    NLL too shows why most of those rejections are right: half the rejected
    candidates lose NLL to climatology outright, which is a distribution
    confidently wrong where it matters, not a scoring quirk.

    So this is a diagnostic, not an alarm, with one exception.  NLL is the
    binding condition today.  If it stops being one -- if the share blocked
    by Brier *alone* grows -- then Brier has become the gate's sole
    gatekeeper on this path, and that is the moment the tolerance thresholds
    are worth revisiting.  That, and only that, is reported as a finding.
    """

    rows = conn.execute(
        "SELECT oos_metrics_json, climatology_baseline_metrics_json"
        " FROM calibration_cross_native_ensemble_oos_evaluation"
        " WHERE evaluated_at_utc>=?",
        (since,),
    ).fetchall()
    contested = []
    for metrics_json, baseline_json in rows:
        try:
            model = json.loads(str(metrics_json))
            climate = json.loads(str(baseline_json))
        except (TypeError, ValueError):
            continue
        if not model.get("crps") or not climate.get("brier"):
            continue
        if model["crps"] > climate["crps"] or model["brier"] <= climate["brier"]:
            continue
        contested.append((
            (model["brier"] - climate["brier"]) / climate["brier"],
            (climate["crps"] - model["crps"]) / climate["crps"],
            (
                (climate["nll"] - model["nll"]) / climate["nll"]
                if climate.get("nll") else 0.0
            ),
        ))
    total = len(contested)
    if not total:
        return {"contested_count": 0}
    within_brier = [c for c in contested if c[0] <= BRIER_TOLERANCE_MAX_LOSS]
    passes_crps = [c for c in contested if c[1] >= BRIER_TOLERANCE_MIN_GAIN]
    passes_nll = [c for c in contested if c[2] >= BRIER_TOLERANCE_MIN_GAIN]
    admitted = [
        c for c in contested
        if c[0] <= BRIER_TOLERANCE_MAX_LOSS
        and c[1] >= BRIER_TOLERANCE_MIN_GAIN
        and c[2] >= BRIER_TOLERANCE_MIN_GAIN
    ]
    # Blocked by Brier and nothing else: the other two conditions are met and
    # only the Brier loss keeps it out.
    brier_only = [
        c for c in contested
        if c[0] > BRIER_TOLERANCE_MAX_LOSS
        and c[1] >= BRIER_TOLERANCE_MIN_GAIN
        and c[2] >= BRIER_TOLERANCE_MIN_GAIN
    ]
    return {
        "contested_count": total,
        "brier_loss_within_tolerance_share": len(within_brier) / total,
        "crps_gain_sufficient_share": len(passes_crps) / total,
        "nll_gain_sufficient_share": len(passes_nll) / total,
        "admitted_share": len(admitted) / total,
        "blocked_by_brier_alone_share": len(brier_only) / total,
        "median_brier_loss": statistics.median(c[0] for c in contested),
        "median_crps_gain": statistics.median(c[1] for c in contested),
        "median_nll_gain": statistics.median(c[2] for c in contested),
        "brier_tolerance_max_loss": BRIER_TOLERANCE_MAX_LOSS,
        "brier_tolerance_min_gain": BRIER_TOLERANCE_MIN_GAIN,
    }


def murphy_decomposition(
    conn: sqlite3.Connection, *, since: str, stage: str = "P0"
) -> dict:
    """Split served Brier into calibration, discrimination and the irreducible.

    ``BS = REL - RES + UNC``.  Reliability is what the calibration layers can
    still win; resolution is whether the model can tell days apart at all;
    uncertainty is a property of the market's bucket ladder and nothing any
    layer can move.  The split matters because the two fixable terms need
    opposite work -- a bad REL is a calibration bug, a low RES is a shortage
    of information no amount of calibration reaches.

    Climatology has no resolution and is calibrated by construction, so its
    Brier *is* UNC; the gate's Brier comparison therefore reduces to asking
    whether RES exceeds REL.  Measured 2026-08-18: REL is 0.6% of UNC and RES
    is 9.1%, which is why that comparison is decided almost entirely by
    information and hardly at all by calibration.
    """

    rows = conn.execute(
        "SELECT e.bucket_ordinals_json, e.probabilities_json,"
        " t.official_tmax_c"
        " FROM basket_probability_envelope e"
        " JOIN calibration_station_daily_high_truth t"
        "  ON t.station_id=e.station_id AND t.local_date=e.event_date"
        "  AND t.qc_status='ACCEPTED'"
        " WHERE e.stage=? AND e.state='COMMITTED' AND e.event_date>=?"
        " GROUP BY e.station_id, e.event_date",
        (stage, since[:10]),
    ).fetchall()
    pairs: list[tuple[float, float]] = []
    bucket_counts: set[int] = set()
    for ordinals_json, probabilities_json, actual in rows:
        try:
            ordinals = json.loads(str(ordinals_json))
            probabilities = json.loads(str(probabilities_json))
        except (TypeError, ValueError):
            continue
        keys = sorted(ordinals, key=lambda key: ordinals[key])
        fahrenheit = any("\u00b0F" in key for key in keys)
        uppers = []
        for key in keys[:-1]:
            digits = [float(v) for v in re.findall(r"-?\d+", key)]
            if not digits:
                uppers = []
                break
            top = max(digits) + 0.5
            uppers.append((top - 32.0) * 5.0 / 9.0 if fahrenheit else top)
        if len(uppers) != len(keys) - 1:
            continue
        total = sum(float(probabilities.get(key, 0.0)) for key in keys)
        if total <= 0.0:
            continue
        landed = len(uppers)
        for index, upper in enumerate(uppers):
            if float(actual) <= upper:
                landed = index
                break
        bucket_counts.add(len(keys))
        for index, key in enumerate(keys):
            pairs.append((
                float(probabilities.get(key, 0.0)) / total,
                1.0 if index == landed else 0.0,
            ))
    if len(pairs) < MURPHY_MIN_PAIRS or len(bucket_counts) != 1:
        return {"pair_count": len(pairs), "estimable": False}
    buckets = next(iter(bucket_counts))
    count = len(pairs)
    base_rate = sum(outcome for _p, outcome in pairs) / count
    binned: dict[int, list[tuple[float, float]]] = collections.defaultdict(
        list
    )
    for probability, outcome in pairs:
        binned[
            min(MURPHY_BIN_COUNT - 1, int(probability * MURPHY_BIN_COUNT))
        ].append((probability, outcome))
    reliability = resolution = 0.0
    for group in binned.values():
        size = len(group)
        mean_p = sum(p for p, _o in group) / size
        mean_o = sum(o for _p, o in group) / size
        reliability += size / count * (mean_p - mean_o) ** 2
        resolution += size / count * (mean_o - base_rate) ** 2
    uncertainty = base_rate * (1.0 - base_rate)
    brier = sum((p - o) ** 2 for p, o in pairs) / count
    return {
        "estimable": True,
        "stage": stage,
        "station_day_count": count // buckets,
        "bucket_count": buckets,
        "pair_count": count,
        # Scaled to the whole ladder so these read in the same units as the
        # Brier the gate compares.
        "brier": brier * buckets,
        "reliability": reliability * buckets,
        "resolution": resolution * buckets,
        "uncertainty": uncertainty * buckets,
        "reliability_share_of_uncertainty": reliability / uncertainty,
        "resolution_share_of_uncertainty": resolution / uncertainty,
        "brier_skill_score": (resolution - reliability) / uncertainty,
        # The split is exact only for forecasts binned by identical value;
        # with twelve bins the probabilities inside one still vary, and that
        # within-bin spread is the difference between the two sides.  Report
        # it rather than round it away -- a residual that grows means the
        # bins have stopped describing the forecast distribution.
        "reconstruction_residual": abs(
            brier - (reliability - resolution + uncertainty)
        ) * buckets,
        "bin_count": MURPHY_BIN_COUNT,
    }


def check_calibration_regression(murphy: dict) -> list[dict]:
    """Reliability climbing is a calibration layer coming undone.

    Resolution is bounded by the information upstream and is not this
    check's business; reliability is entirely the calibration layers' doing,
    so a rise in it is the one part of Brier they are answerable for.
    """

    if not murphy.get("estimable"):
        return []
    share = float(murphy.get("reliability_share_of_uncertainty") or 0.0)
    if share <= MURPHY_RELIABILITY_ALARM_SHARE:
        return []
    return [{
        "check": "served_reliability_degraded",
        "stage": murphy.get("stage"),
        "reliability_share_of_uncertainty": round(share, 5),
        "resolution_share_of_uncertainty": round(
            float(murphy.get("resolution_share_of_uncertainty") or 0.0), 5
        ),
        "brier_skill_score": round(
            float(murphy.get("brier_skill_score") or 0.0), 5
        ),
        "station_day_count": murphy.get("station_day_count"),
        "alarm_share": MURPHY_RELIABILITY_ALARM_SHARE,
    }]


def check_gate_binding_condition(rates: dict) -> list[dict]:
    """Brier having become the sole blocker is the trigger to revisit it."""

    total = int(rates.get("contested_count") or 0)
    if total < GATE_RATE_MIN_SAMPLE:
        return []
    share = float(rates.get("blocked_by_brier_alone_share") or 0.0)
    if share < GATE_BRIER_ONLY_ALARM_SHARE:
        return []
    return [{
        "check": "tolerance_channel_now_gated_by_brier_alone",
        "contested_count": total,
        "blocked_by_brier_alone_share": round(share, 4),
        "nll_gain_sufficient_share": round(
            float(rates.get("nll_gain_sufficient_share") or 0.0), 4
        ),
        "median_brier_loss": round(
            float(rates.get("median_brier_loss") or 0.0), 4
        ),
        "median_crps_gain": round(
            float(rates.get("median_crps_gain") or 0.0), 4
        ),
        "alarm_share": GATE_BRIER_ONLY_ALARM_SHARE,
    }]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # ``mode=ro`` and not ``immutable=1``: the resident writes continuously,
    # and immutable tells SQLite the file cannot change, so it skips the WAL
    # and reads a torn view -- observed here as "database disk image is
    # malformed" on the second run, which is the loud failure; the quiet one
    # is a stale answer that looks fine.
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = _utc_now()
    live = _live_activations(conn, now)
    since = (
        datetime.now(timezone.utc)
        - timedelta(days=GATE_RATE_LOOKBACK_DAYS)
    ).isoformat().replace("+00:00", "Z")
    gate_rates = gate_condition_rates(conn, since=since)
    murphy = murphy_decomposition(conn, since=since)
    findings = (
        check_spread_bound(live)
        + check_table_freshness(conn, live)
        + check_bias_stability(conn, live)
        + check_gate_binding_condition(gate_rates)
        + check_calibration_regression(murphy)
    )
    conn.close()

    by_check = collections.Counter(row["check"] for row in findings)
    payload = {
        "schema": "native_calibration_health_v1",
        "checked_at_utc": now,
        "live_scope_count": sum(
            1 for key in live if key[3] in LIVE_LEAD_BUCKETS
        ),
        "finding_count": len(findings),
        "by_check": dict(by_check),
        "gate_condition_rates": gate_rates,
        "served_brier_decomposition": murphy,
        "findings": findings,
        "writes_target_database": False,
        "calls_order_gateway": False,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 1 if findings else 0
    print(f"活 scope {payload['live_scope_count']}  发现 {len(findings)} 项")
    if murphy.get("estimable"):
        print(
            f"  服务 Brier 分解（近 {GATE_RATE_LOOKBACK_DAYS} 天"
            f" {murphy['station_day_count']} 站日 ×"
            f" {murphy['bucket_count']} 桶）:"
            f" BS {murphy['brier']:.4f} = REL {murphy['reliability']:.4f}"
            f" − RES {murphy['resolution']:.4f}"
            f" + UNC {murphy['uncertainty']:.4f}"
        )
        print(
            f"    校准还欠 {murphy['reliability_share_of_uncertainty']:.1%}"
            f" · 模型能解释 {murphy['resolution_share_of_uncertainty']:.1%}"
            f" · BSS {murphy['brier_skill_score']:+.4f}"
            f"（校准欠超 {MURPHY_RELIABILITY_ALARM_SHARE:.0%} 才告警）"
        )
    if gate_rates.get("contested_count"):
        print(
            f"  容差通道（近 {GATE_RATE_LOOKBACK_DAYS} 天"
            f" {gate_rates['contested_count']} 个 CRPS赢/Brier输 候选）:"
        )
        print(
            f"    Brier 劣势在容差内 {gate_rates['brier_loss_within_tolerance_share']:.1%}"
            f" · CRPS 增益够 {gate_rates['crps_gain_sufficient_share']:.1%}"
            f" · NLL 增益够 {gate_rates['nll_gain_sufficient_share']:.1%}"
            f" ⇒ 放行 {gate_rates['admitted_share']:.1%}"
        )
        print(
            f"    仅被 Brier 挡住 {gate_rates['blocked_by_brier_alone_share']:.1%}"
            f"（超 {GATE_BRIER_ONLY_ALARM_SHARE:.0%} 才告警）"
        )
    for check, count in by_check.most_common():
        print(f"  {check}: {count}")
    for row in findings[:20]:
        print("   ", json.dumps(row, ensure_ascii=False))
    if len(findings) > 20:
        print(f"    … 另有 {len(findings)-20} 项，用 --json 看全")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
