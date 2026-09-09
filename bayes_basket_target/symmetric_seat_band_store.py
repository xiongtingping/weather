"""Where the symmetric seat rule still applies, fitted rather than declared.

Charter 79 forces a symmetric day's P0/P0-M seat onto the mean bucket and its
two neighbours.  Charter 122 confines that to a band of the served width,
because the reading "symmetric" is a monotone function of that width alone and
three admitted width changes between 2026-09-03 and 09-07 pushed the rule from
binding about half the days to 95.8% of P0 seats without anyone ruling that it
should.  The band's first value, [1.0, inf) in bucket steps, was hand-set from
a one-off replay sweep.  This module is what replaces the hand.

WHY THE PAIR, AND NOT A REPLAY.  The obvious way to score a band is to rebuild
the book under it, which is what the sweep did: five arms, twenty-eight minutes
each.  It also carried a floor of about +-15 USDC from interference -- arms
differ in which seats they take, which changes chains and the capital left for
every other station -- and the narrow band's own effect was +-2.  A measurement
whose noise is seven times its signal cannot be run on a schedule and believed.

So the runtime records the pair instead (``symmetric_seat_counterfactual``, in
the seat audit of every plan on a symmetric day): the centre+-1 window the rule
would force, and the largest-edge admissible window the enumeration would
choose, each carrying the all-in price it would have been bought at on the same
book at the same instant.  A settled day answers by subtraction.  Same day,
same book, no interference.

WHAT IT CANNOT SEE.  This scores the SEAT, one unit per arm.  An effect that
exists only through sizing, chain composition or capital competition is
invisible here, and the replay stays the instrument for those.  The band is a
seat rule, so the seat is the right unit -- but a reading from this lane is not
a claim about the book's return.

The fit is deliberately coarse.  Charter 81 measured what happens when a served
coefficient is refined to its own curve: two arms that fitted the curve more
closely both lost money, because the windows a finer rule admits are the ones
the market prices richest.  So the lower bound is chosen from a seven-point
grid, the upper bound is not fitted at all (the sweep found no difference on
wide days and a bound there would be a second claim the evidence cannot carry),
and a candidate must beat the incumbent on a forward window it was not fitted
on before it serves.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .ssot import content_sha256

METHOD_TAG = "symmetric_seat_width_band_v1"

# The stages charter 79 covers.  P1 is out of the rule and out of this lane.
FIT_STAGES = ("P0", "P0-M")

# The rolling window of settled days the fit reads.
WINDOW_DAYS = 21
# Below either of these the incumbent is held and the lane reports
# ACCUMULATING.  Days matter more than pairs: forty pairs from two days is two
# draws of the weather, and the narrow band's whole problem in the replay
# sweep was that its sample was two days wearing seventeen seats.
MIN_QUALIFYING_DAYS = 8
MIN_QUALIFYING_PAIRS = 60
# Fitted on the older part, judged on the newer.
FORWARD_WINDOW_FRACTION = 0.35
# Per-seat USDC the candidate must beat the incumbent by on the forward
# window.  One cent of expectation per seat: below that the two bands are the
# same rule wearing different numbers, and churning the served value costs the
# ledger a fingerprint and the board an explanation for nothing.
MIN_FORWARD_GAIN_PER_SEAT = 0.01
# Charter 81's lesson, as a grid.  Bucket steps.
LOWER_BOUND_GRID = (0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_symmetric_seat_band ("
    " band_sha256 TEXT NOT NULL CHECK(length(band_sha256)=64),"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M')),"
    " lower_bound REAL NOT NULL,"
    " upper_bound REAL,"
    " pair_count INTEGER NOT NULL,"
    " day_count INTEGER NOT NULL,"
    " forward_gain_per_seat REAL NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT,"
    " PRIMARY KEY(band_sha256, stage))"
)
_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_symmetric_seat_band_journal ("
    " fit_id TEXT PRIMARY KEY,"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M')),"
    " fitted_at_utc TEXT NOT NULL,"
    " band_sha256 TEXT NOT NULL,"
    " lower_bound REAL NOT NULL,"
    " pair_count INTEGER NOT NULL,"
    " day_count INTEGER NOT NULL,"
    " forward_gain_per_seat REAL NOT NULL,"
    " outcome TEXT NOT NULL CHECK(outcome IN"
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD')),"
    " evidence_json TEXT NOT NULL)"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_JOURNAL_DDL)


def _check_stage(stage: str) -> str:
    text = str(stage)
    if text not in FIT_STAGES:
        raise ValueError(f"symmetric_seat_band_stage_unknown: {stage}")
    return text


def band_sha256(*, stage: str, lower: float, upper: float | None) -> str:
    return content_sha256({
        "schema": "symmetric_seat_band_v1",
        "method_tag": METHOD_TAG,
        "stage": _check_stage(stage),
        "lower_bound": round(float(lower), 6),
        "upper_bound": None if upper is None else round(float(upper), 6),
    })


def load_symmetric_seat_band(
    conn: sqlite3.Connection, *, stage: str
) -> tuple[float, float, str] | None:
    """The active band for a stage as ``(lower, upper, sha)``.

    ``upper`` is ``inf`` when unbounded, so a caller can compare without
    special-casing.  None when nothing is active, which is every day before
    this lane has cleared its first forward window -- the caller keeps its
    own default, and that default is charter 122's ruled value.
    """

    try:
        row = conn.execute(
            "SELECT lower_bound, upper_bound, band_sha256"
            " FROM calibration_symmetric_seat_band"
            " WHERE stage=? AND activated=1"
            " ORDER BY activated_at_utc DESC LIMIT 1",
            (_check_stage(stage),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    upper = float("inf") if row[1] is None else float(row[1])
    return (float(row[0]), upper, str(row[2]))


def load_symmetric_seat_bands(
    conn: sqlite3.Connection,
) -> dict[str, tuple[float, float, str]]:
    """Every stage's active band, for one load at the top of a compile."""

    out: dict[str, tuple[float, float, str]] = {}
    for stage in FIT_STAGES:
        found = load_symmetric_seat_band(conn, stage=stage)
        if found is not None:
            out[stage] = found
    return out


def _truth_by_scope(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """The winning bucket per station-day, read from the production label.

    Never re-derived from the temperature: the per-station grid rule is not
    shared between Celsius and two-degree Fahrenheit stations, and an audit
    that re-implements it invents its own findings.
    """

    out: dict[tuple[str, str], str] = {}
    try:
        rows = conn.execute(
            "SELECT f.station_id, f.event_date, l.winning_bucket"
            " FROM basket_policy_training_label l"
            " JOIN basket_policy_training_fact f USING(fact_id)"
            " WHERE l.winning_bucket IS NOT NULL AND l.winning_bucket<>''"
        )
    except sqlite3.OperationalError:
        return out
    for station, day, bucket in rows:
        out[(str(station), str(day))] = str(bucket)
    return out


def _arm_value(arm: Mapping[str, Any] | None, winner: str) -> float:
    """One unit of the arm, settled.

    A missing arm is the rule's hold-and-do-not-buy case: no seat, no cost,
    no payout, and zero is the honest value of it rather than a gap in the
    sample.  A window with no price could not have been bought at all, so it
    is treated the same way.
    """

    if not arm:
        return 0.0
    price = arm.get("all_in_price")
    if price is None:
        return 0.0
    payout = 1.0 if str(winner) in {str(b) for b in arm.get("buckets") or ()} else 0.0
    return payout - float(price)


def collect_pairs(
    conn: sqlite3.Connection, *, stage: str, min_event_date: str | None = None
) -> list[dict[str, Any]]:
    """One settled pair per station-day: what forcing was worth against not.

    The last plan of the station-day is the one taken, the way the board reads
    a day's seat; earlier plans in the same day are the same question asked
    with less information and would weight a talkative day more than a quiet
    one.
    """

    stage_id = _check_stage(stage)
    truth = _truth_by_scope(conn)
    if not truth:
        return []
    params: list[Any] = [stage_id]
    where = (
        "json_extract(plan_json,'$.compiled_plan.stage')=?"
        " AND json_extract(plan_json,"
        "'$.compiled_plan.plan.basket_support_selection"
        ".symmetric_seat_counterfactual') IS NOT NULL"
    )
    if min_event_date:
        where += " AND event_date>=?"
        params.append(str(min_event_date))
    latest: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    try:
        rows = conn.execute(
            "SELECT station_id, event_date, created_at,"
            " json_extract(plan_json,'$.compiled_plan.plan"
            ".basket_support_selection.symmetric_seat_counterfactual')"
            f" FROM basket_p0_preselection_plan WHERE {where}",
            params,
        )
    except sqlite3.OperationalError:
        return []
    for station, day, created, payload in rows:
        scope = (str(station), str(day))
        if scope not in truth:
            continue
        if scope in latest and latest[scope][0] >= str(created):
            continue
        try:
            record = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        latest[scope] = (str(created), record)
    pairs: list[dict[str, Any]] = []
    for (station, day), (_created, record) in latest.items():
        sigma = record.get("sigma_buckets")
        if sigma is None:
            continue
        winner = truth[(station, day)]
        pairs.append({
            "station_id": station,
            "event_date": day,
            "sigma_buckets": float(sigma),
            "forced_value": _arm_value(record.get("forced"), winner),
            "free_value": _arm_value(record.get("free"), winner),
            "forced_present": bool(record.get("forced")),
        })
    pairs.sort(key=lambda row: (row["event_date"], row["station_id"]))
    return pairs


def _score(pairs: Sequence[Mapping[str, Any]], lower: float) -> float:
    """Mean per-seat value of the band whose lower bound is ``lower``.

    Above the bound the rule fires and the day is worth its forced arm; below
    it the enumeration chooses and the day is worth its free arm.  Mean, not
    total, so bands that differ in how many days they touch stay comparable.
    """

    if not pairs:
        return 0.0
    total = 0.0
    for row in pairs:
        in_band = float(row["sigma_buckets"]) >= float(lower)
        total += float(row["forced_value"] if in_band else row["free_value"])
    return total / len(pairs)


def fit_symmetric_seat_band(
    conn: sqlite3.Connection,
    *,
    stage: str,
    incumbent_lower: float,
    min_event_date: str | None = None,
) -> dict[str, Any]:
    """Choose a lower bound on the older days, judge it on the newer ones."""

    stage_id = _check_stage(stage)
    pairs = collect_pairs(conn, stage=stage_id, min_event_date=min_event_date)
    days = sorted({row["event_date"] for row in pairs})
    evidence: dict[str, Any] = {
        "schema": "symmetric_seat_band_evidence_v1",
        "method_tag": METHOD_TAG,
        "stage": stage_id,
        "pair_count": len(pairs),
        "day_count": len(days),
        "min_event_date": min_event_date,
        "incumbent_lower": float(incumbent_lower),
        "grid": list(LOWER_BOUND_GRID),
        "min_qualifying_days": MIN_QUALIFYING_DAYS,
        "min_qualifying_pairs": MIN_QUALIFYING_PAIRS,
        "min_forward_gain_per_seat": MIN_FORWARD_GAIN_PER_SEAT,
    }
    if len(days) < MIN_QUALIFYING_DAYS or len(pairs) < MIN_QUALIFYING_PAIRS:
        evidence["basis"] = "ACCUMULATING"
        return {
            "stage": stage_id,
            "lower_bound": float(incumbent_lower),
            "accumulating": True,
            "admitted": False,
            "forward_gain_per_seat": 0.0,
            "pair_count": len(pairs),
            "day_count": len(days),
            "evidence": evidence,
        }
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    train_days, forward_days = set(days[:cut]), set(days[cut:] or days[-1:])
    train = [row for row in pairs if row["event_date"] in train_days]
    forward = [row for row in pairs if row["event_date"] in forward_days]
    train_scores = {
        bound: _score(train, bound) for bound in LOWER_BOUND_GRID
    }
    # Ties go to the LOWER bound: charter 79 as ruled applies everywhere, and
    # a tie is no evidence for narrowing it further.
    candidate = min(
        LOWER_BOUND_GRID,
        key=lambda bound: (-train_scores[bound], bound),
    )
    forward_candidate = _score(forward, candidate)
    forward_incumbent = _score(forward, float(incumbent_lower))
    gain = forward_candidate - forward_incumbent
    admitted = (
        candidate != float(incumbent_lower)
        and gain > MIN_FORWARD_GAIN_PER_SEAT
    )
    evidence.update({
        "basis": "FORWARD_WINDOW",
        "train_day_count": len(train_days),
        "forward_day_count": len(forward_days),
        "train_pair_count": len(train),
        "forward_pair_count": len(forward),
        "train_scores": {str(k): round(v, 6) for k, v in train_scores.items()},
        "candidate_lower": float(candidate),
        "forward_score_candidate": round(forward_candidate, 6),
        "forward_score_incumbent": round(forward_incumbent, 6),
        "forward_gain_per_seat": round(gain, 6),
        # What the two arms were worth on their own, so a reader can tell a
        # band that helps from a book where neither arm made money.
        "forward_mean_forced": round(
            sum(r["forced_value"] for r in forward) / len(forward), 6
        ) if forward else 0.0,
        "forward_mean_free": round(
            sum(r["free_value"] for r in forward) / len(forward), 6
        ) if forward else 0.0,
        "forced_absent_pairs": sum(
            1 for r in pairs if not r["forced_present"]
        ),
        "sigma_histogram": {
            f"[{lo},{hi})": sum(
                1 for r in pairs if lo <= r["sigma_buckets"] < hi
            )
            for lo, hi in zip(
                (0.0, 0.5, 1.0, 1.5, 2.0), (0.5, 1.0, 1.5, 2.0, 99.0)
            )
        },
    })
    return {
        "stage": stage_id,
        "lower_bound": float(candidate if admitted else incumbent_lower),
        "accumulating": False,
        "admitted": admitted,
        "forward_gain_per_seat": gain,
        "pair_count": len(pairs),
        "day_count": len(days),
        "evidence": evidence,
    }


def persist_and_activate(
    conn: sqlite3.Connection,
    *,
    stage: str,
    lower_bound: float,
    upper_bound: float | None,
    pair_count: int,
    day_count: int,
    forward_gain_per_seat: float,
    fitted_at_utc: str | None = None,
) -> dict[str, Any]:
    """Insert the band and make it the stage's active one, idempotently."""

    stage_id = _check_stage(stage)
    ensure_schema(conn)
    digest = band_sha256(stage=stage_id, lower=lower_bound, upper=upper_bound)
    active = load_symmetric_seat_band(conn, stage=stage_id)
    if active is not None and active[2] == digest:
        return {"state": "UNCHANGED", "band_sha256": digest}
    stamp = fitted_at_utc or datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    conn.execute(
        "UPDATE calibration_symmetric_seat_band SET activated=0"
        " WHERE stage=? AND activated=1",
        (stage_id,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO calibration_symmetric_seat_band"
        " (band_sha256, stage, lower_bound, upper_bound, pair_count,"
        "  day_count, forward_gain_per_seat, fitted_at_utc, activated,"
        "  activated_at_utc)"
        " VALUES(?,?,?,?,?,?,?,?,1,?)",
        (
            digest, stage_id, float(lower_bound),
            None if upper_bound is None else float(upper_bound),
            int(pair_count), int(day_count), float(forward_gain_per_seat),
            stamp, stamp,
        ),
    )
    return {"state": "ACTIVATED", "band_sha256": digest}


def journal(
    conn: sqlite3.Connection,
    *,
    stage: str,
    lower_bound: float,
    pair_count: int,
    day_count: int,
    forward_gain_per_seat: float,
    outcome: str,
    evidence: Mapping[str, Any],
    fitted_at_utc: str | None = None,
) -> str:
    stage_id = _check_stage(stage)
    ensure_schema(conn)
    stamp = fitted_at_utc or datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    digest = band_sha256(stage=stage_id, lower=lower_bound, upper=None)
    fit_id = "seat-band-" + content_sha256({
        "stage": stage_id, "band_sha256": digest, "fitted_at_utc": stamp,
        "outcome": str(outcome),
    })[:24]
    conn.execute(
        "INSERT OR REPLACE INTO calibration_symmetric_seat_band_journal"
        " (fit_id, stage, fitted_at_utc, band_sha256, lower_bound,"
        "  pair_count, day_count, forward_gain_per_seat, outcome,"
        "  evidence_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            fit_id, stage_id, stamp, digest, float(lower_bound),
            int(pair_count), int(day_count), float(forward_gain_per_seat),
            str(outcome), json.dumps(dict(evidence), ensure_ascii=False,
                                     sort_keys=True),
        ),
    )
    return fit_id


def refit_all(
    conn: sqlite3.Connection,
    *,
    incumbent_lower_by_stage: Mapping[str, float],
    commit: bool = False,
    min_event_date: str | None = None,
) -> dict[str, Any]:
    """Fit every stage, isolated: one stage throwing must not lose the rest."""

    ensure_schema(conn)
    results: dict[str, Any] = {}
    for stage in FIT_STAGES:
        entry: dict[str, Any] = {"stage": stage}
        results[stage] = entry
        try:
            incumbent = float(incumbent_lower_by_stage.get(stage, 0.0))
            fitted = fit_symmetric_seat_band(
                conn, stage=stage, incumbent_lower=incumbent,
                min_event_date=min_event_date,
            )
            outcome = (
                "ACCUMULATING" if fitted["accumulating"]
                else ("ACTIVATED" if fitted["admitted"] else "REFUSED_FORWARD")
            )
            entry.update({
                "lower_bound": fitted["lower_bound"],
                "incumbent_lower": incumbent,
                "admitted": fitted["admitted"],
                "accumulating": fitted["accumulating"],
                "forward_gain_per_seat": fitted["forward_gain_per_seat"],
                "pair_count": fitted["pair_count"],
                "day_count": fitted["day_count"],
                "outcome": outcome,
                "evidence": fitted["evidence"],
            })
            if commit:
                if fitted["admitted"]:
                    entry["commit"] = persist_and_activate(
                        conn, stage=stage, lower_bound=fitted["lower_bound"],
                        upper_bound=None, pair_count=fitted["pair_count"],
                        day_count=fitted["day_count"],
                        forward_gain_per_seat=fitted["forward_gain_per_seat"],
                    )
                entry["fit_id"] = journal(
                    conn, stage=stage, lower_bound=fitted["lower_bound"],
                    pair_count=fitted["pair_count"],
                    day_count=fitted["day_count"],
                    forward_gain_per_seat=fitted["forward_gain_per_seat"],
                    outcome=outcome, evidence=fitted["evidence"],
                )
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
    return results
