#!/usr/bin/env python3
"""Charter 279: fit the deterministic consensus shift on settled station-days.

Per stage family (P0 beyond 36 h, P0 within 12-36 h, P0 within 12 h, P0-M, P1) and per variant
(fleet or station bias; the ten-system roster): F's bias against the settled
truth, kappa through the origin, and the walk-forward scores of the shift
each event day would have had under the parameters fitted on the days before
it (``deterministic_consensus_shift.walk_forward``; charter 240's verdict), on
decisions from ``SCORE_FROM_DECISION_UTC`` only (补记四; the whole window is
kept as ``walk_forward_whole_window``).
One record per station-day x family: the first decision of that family, its
served vector, the consensus knowable at that decision.  补记七: a P0 that
served the shift contributes the vector it moved, so P0's own fit stays on the
unshifted chain; P0-M and P1 also carry ``walk_forward_after_p0_serving``,
fitted and scored on decisions from ``SERVE_FROM_DECISION_UTC`` alone -- the
residual on top of the served P0 root the next ruling reads.

SSOT ``mode=ro``, no database written; one JSON artifact the P0 and P0-M
compiles read (``deterministic_consensus_shift.FIT_ARTIFACT_PATH``).

    python3 scripts/fit_deterministic_consensus_shift.py [--days 60] [--stations ZUUU,EGLC] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import deterministic_consensus_shift as DCS  # noqa: E402
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket  # noqa: E402
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
WINDOW_DAYS = 60
BUDGET_SECONDS = 1500.0
ENVELOPES_SQL = (
    "SELECT decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.observed_hard_excluded_buckets'),"
    " json_extract(lineage_json,'$.hard_excluded_buckets'),"
    " json_extract(lineage_json,'$.deterministic_consensus_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage=?"
    " ORDER BY decision_at_utc LIMIT ?"
)
# P0 is walked until its three families are found; P0-M and P1 take their first.
STAGE_LIMIT = {"P0": -1, "P0-M": 1, "P1": 1}
TRUTH_SQL = (
    "SELECT event_date, truth_revision_no, settled_tmax_c FROM basket_station_day_settlement_label"
    " WHERE station_id=? AND event_date>=? AND event_date<=? ORDER BY event_date, truth_revision_no"
)


def _truth(conn: sqlite3.Connection, station: str, start: str, end: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for day, _rev, tmax in conn.execute(TRUTH_SQL, (station, start, end)):
        if tmax is not None:
            out[str(day)] = float(tmax)   # revisions ascend: the last one wins
    return out


def _excluded(text) -> list[str]:
    return [b for b in str(text or "").split(",") if b and b != "NONE"]


def _pre_shift(text) -> dict[str, float] | None:
    """The vector a served shift moved (charter 279 补记七), else None."""

    try:
        receipt = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    if not isinstance(receipt, dict) or receipt.get("state") != "SERVED":
        return None
    pre = receipt.get("pre_shift_probabilities")
    return {k: float(v) for k, v in pre.items()} if isinstance(pre, dict) and pre else None


def _first_per_family(conn: sqlite3.Connection, station: str, day: str) -> list[tuple[str, tuple]]:
    """(family, envelope row) for the first P0 beyond 36 h, the first P0 within 12-36 h,
    the first P0 within 12 h (补记八),
    the first P0-M and the first P1 (charter 279 补记三)."""

    picked: list[tuple[str, tuple]] = []
    seen: set[str] = set()
    for stage in ("P0", "P0-M", "P1"):
        for row in conn.execute(ENVELOPES_SQL, (station, day, stage, STAGE_LIMIT[stage])).fetchall():
            dec, tz = row[0], row[1]
            try:
                hours = hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz)
            except Exception:  # noqa: BLE001
                continue
            family = DCS.stage_family(stage, hours)
            if family is None or family in seen:
                continue
            seen.add(family)
            picked.append((family, row))
            if stage != "P0" or len([f for f in seen if f.startswith("P0_")]) == 3:
                break
    return picked


def collect(
    conn: sqlite3.Connection, *, start: str, end: str, stations: list[str] | None,
    budget_seconds: float, started: float,
) -> tuple[list[dict], dict]:
    roster = stations or [r[0] for r in conn.execute(
        "SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    records: list[dict] = []
    counts = {"stations": 0, "station_days": 0, "envelopes": 0, "no_read": 0, "too_few_systems": 0,
              "no_winner": 0, "served_pre_shift": 0, "partial": False}
    for station in roster:
        if time.time() - started > budget_seconds:
            counts["partial"] = True
            break
        truth = _truth(conn, station, start, end)
        if not truth:
            continue
        counts["stations"] += 1
        for day in sorted(truth):
            picked = _first_per_family(conn, station, day)
            if not picked:
                continue
            counts["station_days"] += 1
            try:
                rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            except Exception:  # noqa: BLE001
                rows = None
            for family, (dec, tz, pj, oj, ivj, p0m_excluded, p1_excluded, consensus_json) in picked:
                counts["envelopes"] += 1
                try:
                    served = {k: float(v) for k, v in json.loads(pj).items()}
                    # 补记七: a P0 that served the shift is scored on the vector it moved.
                    pre = _pre_shift(consensus_json)
                    if pre is not None:
                        served = pre
                        counts["served_pre_shift"] += 1
                    ordinals = json.loads(oj)
                    intervals = json.loads(ivj)
                except (TypeError, ValueError):
                    continue
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                if set(ordered) != set(served) or not isinstance(intervals, dict):
                    continue
                winner = _winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                if winner is None or winner not in served:
                    counts["no_winner"] += 1
                    continue
                read = DCS.consensus_read(
                    conn, station_id=station, event_date=day, as_of_utc=dec,
                    settlement_timezone=tz, rows=rows,
                )
                if read.get("state") != "ok":
                    counts["no_read"] += 1
                    continue
                if all(v is None for v in read["f_c"].values()):
                    counts["too_few_systems"] += 1
                    continue
                records.append({
                    "station": station, "day": day, "family": family, "decision_at_utc": dec,
                    "centre_c": DCS.served_mean_c(served, ordered, intervals), "f_c": dict(read["f_c"]),
                    "n": dict(read["n"]), "y_c": truth[day], "p": served, "ordered": ordered,
                    "intervals": intervals, "winner": winner,
                    "excluded": _excluded(p0m_excluded if family == "P0M" else p1_excluded if family == "P1" else ""),
                })
    return records, counts


def fit(db: Path, *, now: datetime, stations: list[str] | None, days: int, budget_seconds: float) -> dict:
    started = time.time()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    today = now.astimezone(timezone.utc).date()
    start = (today - timedelta(days=days)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    try:
        records, counts = collect(conn, start=start, end=end, stations=stations,
                                  budget_seconds=budget_seconds, started=started)
    finally:
        conn.close()
    families: dict[str, dict] = {}
    for family in DCS.STAGE_FAMILIES:
        own = [r for r in records if r["family"] == family]
        params = DCS.fit_parameters(records, family=family)
        # Charter 279 补记四: the verdict and the board read the same-period
        # walk-forward (decisions from SCORE_FROM_DECISION_UTC); the whole
        # window is kept beside it for reference.
        forward = DCS.walk_forward(records, family=family, score_from_utc=DCS.SCORE_FROM_DECISION_UTC)
        forward_whole = DCS.walk_forward(records, family=family)
        # 补记七: P0-M and P1 on the served-consensus root -- what is left on
        # top of P0's shift, fitted and scored on those decisions alone.
        residual = (
            DCS.walk_forward(
                [r for r in records if str(r["decision_at_utc"]) >= DCS.SERVE_FROM_DECISION_UTC],
                family=family,
            )
            if family not in DCS.SERVE_FAMILIES else {}
        )
        variants = {}
        for variant in DCS.VARIANTS:
            entry = dict(params.get(variant) or {})
            entry["walk_forward"] = forward.get(variant)
            entry["walk_forward_whole_window"] = forward_whole.get(variant)
            if family not in DCS.SERVE_FAMILIES:
                entry["walk_forward_after_p0_serving"] = residual.get(variant)
            variants[variant] = entry
        families[family] = {
            "n_records": len(own), "n_days": len({r["day"] for r in own}),
            "n_stations": len({r["station"] for r in own}),
            "f_bias_c": {
                roster: (round(sum(r["y_c"] - r["f_c"][roster] for r in own if r["f_c"].get(roster) is not None)
                               / max(1, sum(1 for r in own if r["f_c"].get(roster) is not None)), 4)
                         if any(r["f_c"].get(roster) is not None for r in own) else None)
                for roster in DCS.ROSTERS
            },
            "variants": variants,
        }
    return {
        "schema": DCS.FIT_SCHEMA, "rule": DCS.RULE_VERSION,
        "fitted_at_utc": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [start, end], "cap_c": DCS.CAP_C, "min_systems": DCS.MIN_SYSTEMS,
        "min_fit_event_days": DCS.MIN_FIT_EVENT_DAYS, "verdict_min_event_days": DCS.VERDICT_MIN_EVENT_DAYS,
        "score_from_decision_utc": DCS.SCORE_FROM_DECISION_UTC,
        "serve_from_decision_utc": DCS.SERVE_FROM_DECISION_UTC,
        "serve_families": sorted(DCS.SERVE_FAMILIES), "mode": DCS.MODE,
        "station_bias_prior_days": DCS.STATION_BIAS_PRIOR_DAYS,
        "rosters": {"wide": list(DCS.PRODUCTS)},
        "primary": DCS.PRIMARY_VARIANT, "counts": counts, "families": families,
        "elapsed_seconds": round(time.time() - started, 1),
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".consensus_fit_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=1, sort_keys=True)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--out", type=Path, default=DCS.FIT_ARTIFACT_PATH)
    parser.add_argument("--stations", default="")
    parser.add_argument("--days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--budget-seconds", type=float, default=BUDGET_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()] or None
    payload = fit(args.db, now=datetime.now(timezone.utc), stations=stations, days=args.days,
                  budget_seconds=args.budget_seconds)
    for family, entry in payload["families"].items():
        for variant, params in entry["variants"].items():
            wf = params.get("walk_forward") or {}
            ls = wf.get("logscore") or {}
            print(f"{family:8s} {variant:14s} bias {params.get('bias_c')} kappa {params.get('kappa')}"
                  f" days {entry['n_days']} wf n={wf.get('n')} LogScore Δ {ls.get('delta')} {ls.get('ci')}"
                  f" {wf.get('verdict')}")
    print(f"counts {payload['counts']} elapsed {payload['elapsed_seconds']}s")
    if args.dry_run:
        return 0
    _write(args.out, payload)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
