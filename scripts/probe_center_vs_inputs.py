"""Where does the centre error come from: the inputs, or the pipeline?

At each lead bin, for every served probability vector (highest-authority
stage, last checkpoint in the bin), compare the centre of that vector (its
probability-weighted mean in degrees C) against, on the same station-days:

- the market's implied centre (mid-price distribution, same clock, book after);
- the raw ensemble mean of every native system the pipeline could have read
  at that clock (latest run received at or before the decision), one per
  system, plus the equal-weight mean across the systems present;
- the NBM / HRRR point forecasts and the TAF TX where captured.

Errors are against the settled truth.  If a raw ensemble mean sits closer to
the truth than the served centre, the pipeline is losing information it was
handed; if the market sits where the raw means sit, the market is the public
forecast and nothing more.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LEAD_BINS = (
    (36.0, math.inf, ">=36h"),
    (30.0, 36.0, "30-36h"),
    (24.0, 30.0, "24-30h"),
    (18.0, 24.0, "18-24h"),
    (15.0, 18.0, "15-18h"),
    (12.0, 15.0, "12-15h"),
    (10.0, 12.0, "10-12h"),
    (8.0, 10.0, "8-10h"),
    (6.0, 8.0, "6-8h"),
)
STAGES = ("P0", "P0-M", "P1")
# Hours after a run's init before its fields are public; the as-of filter
# admits a run only when init + lag <= the decision clock.
AVAILABILITY_LAG_H = {
    "ECMWF_IFS_ENS_OPEN_PF50": 8.0, "NOAA_GEFS_05": 6.0, "ECCC_GEM_GLOBAL_ENS": 6.0,
    "DWD_ICON_GLOBAL_EPS": 5.0, "NCEP_NBM_POINT": 2.0, "NCEP_HRRR_POINT": 2.0,
    "METAR_PHYSICAL_PATH_TMAX": 0.0, "OFFICIAL_AIRPORT_TAF_TX": 0.0,
}
AUTHORITY = {s: i for i, s in enumerate(STAGES)}


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _lead_bin(hours: float) -> str | None:
    for lo, hi, label in LEAD_BINS:
        if lo <= hours < hi:
            return label
    return None


def _midpoints(rows):
    """rows: (bucket, lower_c, upper_c) in ordinal order -> midpoint per bucket."""
    widths = [hi - lo for _, lo, hi in rows if lo is not None and hi is not None]
    w = statistics.median(widths) if widths else 1.0
    out = {}
    for name, lo, hi in rows:
        if lo is not None and hi is not None:
            out[name] = (lo + hi) / 2.0
        elif lo is None:
            out[name] = hi - w / 2.0
        else:
            out[name] = lo + w / 2.0
    return out, w


def _market_mid(buckets_json):
    out = {}
    for e in json.loads(buckets_json):
        name = str(e.get("bucket"))
        lv = e.get("ask_levels") or []
        ask = float(lv[0]["price"]) if lv else None
        bl = e.get("bid_levels") or []
        bid = float(bl[0]["price"]) if bl else None
        if bid is None and float(e.get("bid_price") or 0.0) > 0.0:
            bid = float(e["bid_price"])
        if ask is not None and bid is not None:
            out[name] = (ask + bid) / 2.0
        elif ask is not None:
            out[name] = ask
        elif bid is not None:
            out[name] = bid
        else:
            out[name] = 0.0
    return out


def _centre(vec, mids):
    z = sum(vec.values())
    if z <= 0:
        return None
    return sum(vec[b] * mids[b] for b in vec) / z


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--since", default="")
    ap.add_argument("--book-tolerance-minutes", type=float, default=90.0)
    ap.add_argument("--out")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")

    systems = [r[0] for r in conn.execute(
        "SELECT ensemble_system_id FROM calibration_native_ensemble_system"
    )]
    truth = {}
    for st, ld, t in conn.execute(
        # Ask the truth table the same question the ledger's settlement asks.
        # This used to read every revision and let dict-overwrite ordering
        # pick the last one -- correct only because ``ORDER BY revision_no``
        # happened to leave the highest last.  285 station-days carry two
        # revisions with different values (revision 0 the METAR body group's
        # whole degree, 1 the remarks group's tenth), so a rebuild or a
        # vacuum could have changed this probe's answer without changing a
        # line of it.  Same defect the band store carried until 2026-09-06.
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth t"
        " WHERE qc_status='ACCEPTED' AND revision_no = ("
        "   SELECT MAX(revision_no) FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date"
        "     AND x.qc_status='ACCEPTED')"
    ):
        truth[(st, ld)] = float(t)

    # One covering-index scan of the member table, aggregated per run, then
    # the as-of choice in memory: per-sample lookups on a 37 GB file were
    # random reads and never finished.
    first_date = min((ld for (_, ld) in truth if not args.since or ld >= args.since), default="2026-07-28")
    first_date = max(first_date, "2026-07-28")
    run_means = defaultdict(list)  # (system, station, date) -> [(init, n, mean)]
    for system, st, ld, init, n, mean in conn.execute(
        "SELECT ensemble_system_id, station_id, target_local_date, native_run_init_utc,"
        " COUNT(*), AVG(raw_tmax_c) FROM calibration_native_ensemble_historical_member_run"
        " WHERE qc_status='ACCEPTED' AND target_local_date>=? AND native_run_init_utc IS NOT NULL"
        " GROUP BY 1,2,3,4", (first_date,),
    ):
        run_means[(system, st, ld)].append((init, int(n), float(mean)))
    for runs in run_means.values():
        runs.sort()
    print(f"member runs aggregated: {sum(len(v) for v in run_means.values())} for {len(run_means)} scopes")

    tol = timedelta(minutes=args.book_tolerance_minutes)
    samples = []
    skipped = defaultdict(int)
    for (st, ed), t_true in sorted(truth.items()):
        if args.since and ed < args.since:
            continue
        cps = conn.execute(
            "SELECT history_id, stage, decision_at_utc, settlement_timezone"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY decision_at_utc",
            (st, ed),
        ).fetchall()
        if not cps:
            skipped["no_checkpoints"] += 1
            continue
        tz = ZoneInfo(cps[0][3])
        close_utc = (datetime.fromisoformat(ed).replace(tzinfo=tz) + timedelta(days=1)).astimezone(timezone.utc)
        chosen = {}
        for hid, stage, decided, _ in cps:
            lead = (close_utc - _iso(decided)).total_seconds() / 3600.0
            label = _lead_bin(lead)
            if label is None:
                continue
            key = label
            rank = (AUTHORITY[stage], _iso(decided))
            if key not in chosen or rank > chosen[key][0]:
                chosen[key] = (rank, hid, stage, decided, lead)
        for label, (_, hid, stage, decided, lead) in chosen.items():
            rows = conn.execute(
                "SELECT bucket, lower_c, upper_c, probability FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=? ORDER BY ordinal", (hid,),
            ).fetchall()
            mids, width = _midpoints([(r[0], r[1], r[2]) for r in rows])
            model = {r[0]: float(r[3]) for r in rows}
            served_c = _centre(model, mids)
            book = conn.execute(
                "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at>=?"
                " ORDER BY observed_at LIMIT 1", (st, ed, decided),
            ).fetchone()
            market_c = None
            if book is not None and abs((_iso(book[0]) - _iso(decided)).total_seconds()) <= tol.total_seconds():
                mid = _market_mid(book[1])
                if set(mid) == set(model):
                    market_c = _centre(mid, mids)
            sources = {}
            decided_dt = _iso(decided)
            for system in systems:
                runs = run_means.get((system, st, ed))
                if not runs:
                    continue
                lag = AVAILABILITY_LAG_H.get(system, 6.0)
                cutoff = (decided_dt - timedelta(hours=lag)).strftime("%Y-%m-%dT%H:%M:%SZ")
                eligible = [r for r in runs if r[0] <= cutoff]
                if not eligible:
                    continue
                run_init, n, mean = eligible[-1]
                age = (decided_dt - _iso(run_init)).total_seconds() / 3600.0
                sources[system] = {"mean_c": float(mean), "members": int(n), "run_age_h": age}
            samples.append({
                "station": st, "event_date": ed, "stage": stage, "lead_bin": label,
                "lead_hours": lead, "truth_c": t_true, "width_c": width,
                "served_c": served_c, "market_c": market_c, "sources": sources,
            })
    print(f"samples: {len(samples)} skipped: {dict(skipped)}")

    order = [label for _, _, label in LEAD_BINS]
    all_samples = samples
    for window_name, window_since in (("FULL", ""), ("SINCE 2026-08-27", "2026-08-27")):
        samples = [x for x in all_samples if x["event_date"] >= window_since]
        print(f"\n################ {window_name}: {len(samples)} samples ################")
        _report(samples, systems, order)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(all_samples, fh)
        print("wrote", args.out)


def _report(samples, systems, order):
    ens_systems = [s for s in systems if any(s in x["sources"] for x in samples)]
    print("systems present:", ", ".join(f"{s}({sum(1 for x in samples if s in x['sources'])})" for s in ens_systems))

    def err_table(title, rows, columns):
        print(f"\n== {title} (n={len(rows)}) ==")
        print(f"{'source':>28} {'n':>5} {'rmse_c':>7} {'mae_c':>6} {'bias_c':>7} {'rmse_bk':>7} {'run_age_h':>9}")
        for name, getter in columns:
            vals = []
            ages = []
            for r in rows:
                v = getter(r)
                if v is None:
                    continue
                c, age = v
                vals.append((c - r["truth_c"], r["width_c"]))
                if age is not None:
                    ages.append(age)
            if len(vals) < 20:
                continue
            e = [v[0] for v in vals]
            rmse = math.sqrt(statistics.fmean(x * x for x in e))
            rmse_bk = math.sqrt(statistics.fmean((v[0] / v[1]) ** 2 for v in vals))
            age = statistics.median(ages) if ages else float("nan")
            print(f"{name:>28} {len(vals):>5} {rmse:7.3f} {statistics.fmean(abs(x) for x in e):6.3f}"
                  f" {statistics.fmean(e):+7.3f} {rmse_bk:7.3f} {age:9.1f}")

    def col_served(r):
        return (r["served_c"], None) if r["served_c"] is not None else None

    def col_market(r):
        return (r["market_c"], None) if r["market_c"] is not None else None

    def col_system(s):
        return lambda r: ((r["sources"][s]["mean_c"], r["sources"][s]["run_age_h"]) if s in r["sources"] else None)

    def col_equal(r):
        vals = [v["mean_c"] for k, v in r["sources"].items() if k in ENSEMBLE_ONLY]
        return (statistics.fmean(vals), None) if len(vals) >= 2 else None

    ENSEMBLE_ONLY = {s for s in ens_systems if "ENS" in s or "GEFS" in s or "EPS" in s or "PF50" in s}
    columns = [("served (pipeline)", col_served), ("market mid", col_market), ("equal-mean of ensembles", col_equal)]
    columns += [(s, col_system(s)) for s in ens_systems]

    for label in order:
        rows = [r for r in samples if r["lead_bin"] == label]
        if len(rows) < 30:
            continue
        # common set: served, market and the ECMWF pf50 mean all present
        common = [r for r in rows if r["served_c"] is not None and r["market_c"] is not None
                  and "ECMWF_IFS_ENS_OPEN_PF50" in r["sources"]]
        err_table(f"{label}: common station-days (served, market, ECMWF pf50 all present)", common, columns)

    # by stage at the trading leads
    for stage in STAGES:
        rows = [r for r in samples if r["stage"] == stage and r["lead_bin"] in ("18-24h", "12-15h", "10-12h")
                and r["served_c"] is not None and r["market_c"] is not None]
        if len(rows) >= 30:
            err_table(f"stage {stage} at 10-24h", rows, columns[:3] + [c for c in columns[3:] if c[0] == "ECMWF_IFS_ENS_OPEN_PF50"])



if __name__ == "__main__":
    main()
