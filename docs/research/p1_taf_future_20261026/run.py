#!/usr/bin/env python3
"""TAF's hours after the decision as P1 features (PROTOCOL.md): walk-forward g with and without them.

    python3 docs/research/p1_taf_future_20261026/run.py
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts", ROOT / "docs" / "research" / "p1_forecast_realisation_20261025"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_p1_observation_likelihood as FP  # noqa: E402
from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
import run as RZ  # noqa: E402  (taf_index, features_with, boot, the realisation experiment's helpers)

HERE = Path(__file__).resolve().parent
NEW = ("fut_cloud_up", "fut_precip", "fut_tempo_precip", "fut_convection", "fut_wind_shift")
ARMS = {"B": (), "F": NEW, "F_cloud": NEW[:1], "F_precip": NEW[1:4], "F_wind": NEW[4:]}
SCORE_FROM = "2026-10-06"
BASE_PHASE, BASE_FULL = tuple(OL.PHASE_FEATURES), tuple(OL.FULL_FEATURES)


def _base_groups(fc):
    return sorted([g for g in fc if g.get("fcstChange") in (None, "FM", "BECMG") and g.get("probability") is None],
                  key=lambda g: g.get("timeFrom") or 0)


def _base_at(base, t):
    cur = None
    for g in base:
        begin = g.get("timeBec") if g.get("fcstChange") == "BECMG" and g.get("timeBec") else g.get("timeFrom")
        if begin is not None and begin <= t:
            cur = g
    return cur


def _cloud(g):
    return max([RZ.COVER.get(str(c.get("cover") or "").upper(), 0.0) for c in g.get("clouds") or []] or [0.0])


def _wet(g):
    return any(p in str(g.get("wxString") or "") for p in RZ.PRECIP)


def _conv(g):
    return "TS" in str(g.get("wxString") or "") or any(
        str(c.get("type") or "").upper() in ("CB", "TCU") for c in g.get("clouds") or [])


def future_features(entries, as_of, tz):
    """PROTOCOL features from the newest bulletin issued by ``as_of`` that covers it."""

    if not entries:
        return None
    i = bisect.bisect_right([e[0] for e in entries], as_of)
    awc = None
    for issued, start, end, row in reversed(entries[:i]):
        if start <= as_of < end:
            awc = row
            break
    if awc is None:
        return None
    local = datetime.fromtimestamp(as_of, tz=timezone.utc).astimezone(ZoneInfo(tz))
    cutoff = local.replace(hour=17, minute=0, second=0, microsecond=0).timestamp()
    w_end = min(as_of + 6 * 3600, cutoff)
    zero = {k: 0.0 for k in NEW}
    if w_end <= as_of:
        return zero
    fc = awc["fcsts"]
    base = _base_groups(fc)
    now = _base_at(base, as_of)
    if now is None:
        return None
    probes = [as_of + k * 1800 for k in range(1, int((w_end - as_of) // 1800) + 1)]
    future_base = [g for g in (_base_at(base, t) for t in probes) if g is not None]
    temps = [g for g in fc if (g.get("fcstChange") in ("TEMPO", "PROB") or g.get("probability") is not None)
             and (g.get("timeFrom") or 0) < w_end and (g.get("timeTo") or 0) > as_of]
    shift = 0.0
    wd0, ws0 = now.get("wdir"), float(now.get("wspd") or 0.0)
    for g in future_base:
        if g is now:
            continue
        wd1, ws1 = g.get("wdir"), float(g.get("wspd") or 0.0)
        if isinstance(wd0, (int, float)) and isinstance(wd1, (int, float)) and ws0 >= 5 and ws1 >= 5:
            diff = abs((float(wd1) - float(wd0) + 180.0) % 360.0 - 180.0)
            if diff >= 60.0:
                shift = 1.0
    return {
        "fut_cloud_up": max(0.0, max([_cloud(g) for g in future_base] or [_cloud(now)]) - _cloud(now)),
        "fut_precip": float(any(_wet(g) for g in future_base + temps)),
        "fut_tempo_precip": float(any(_wet(g) for g in temps)),
        "fut_convection": float(any(_conv(g) for g in future_base + temps)),
        "fut_wind_shift": shift,
    }


def main() -> int:
    conn = sqlite3.connect(f"file:{RZ.DB}?mode=ro", uri=True, timeout=120)
    roots = FP.RootCache(conn, sqlite3.connect(f"file:{RZ.NWP / 'p0_ngr_root_replay.db'}?mode=ro", uri=True))
    raw = FP.load_samples(RZ.NWP / "p1_observation_samples.db", since="2026-09-29",
                          through=datetime.now(timezone.utc).date().isoformat())
    samples, dropped = FP.label_samples(raw, roots, FP.load_truth_known_at(conn))
    tafs = RZ.taf_index()
    filled = defaultdict(float)
    for s in samples:
        f = future_features(tafs.get(s["station_id"]), RZ._epoch(s["as_of_utc"]), s["settlement_timezone"])
        for k in NEW:
            s[k] = None if f is None else f[k]
            if f is not None:
                filled[k] += f[k]
    covered = sum(1 for s in samples if s["fut_precip"] is not None)
    served = OL.load_artifact()
    print("samples", len(samples), "covered", covered, "positive", dict(filled), flush=True)
    days = sorted({s["event_date"] for s in samples})
    rows_by_day: dict[str, list] = defaultdict(list)
    for day in days:
        if day < SCORE_FROM:
            continue
        day_start = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc).timestamp()
        train = [s for s in samples if s["event_date"] < day and s["truth_known_epoch"] <= day_start]
        test = [s for s in samples if s["event_date"] == day]
        if len({s["event_date"] for s in train}) < 7 or not test:
            continue
        test_rows = FP.scorable_rows(test, roots)
        per_arm = {}
        for arm, extra in ARMS.items():
            OL.PHASE_FEATURES = BASE_PHASE + tuple(extra)
            OL.FULL_FEATURES = BASE_FULL + tuple(extra)
            FP.features_of = RZ.features_with(extra)
            g = FP.fit_g(train)
            scores = []
            for row in test_rows:
                ev = FP.evidence_of(row, g)
                hours = OL.clock_hours(float(row["sample"]["remaining_hours"]), served, str(row["sample"]["station_id"]))
                scores.append(FP._log_score(row["view"]["probabilities"], ev, row["excluded"], row["truth"],
                                            OL.alpha_for(hours, served)))
            per_arm[arm] = scores
        FP.features_of = RZ.features_with(())
        OL.PHASE_FEATURES, OL.FULL_FEATURES = BASE_PHASE, BASE_FULL
        for i, row in enumerate(test_rows):
            if all(per_arm[a][i] is not None for a in ARMS):
                rec = {a: -per_arm[a][i] for a in ARMS}
                rec["hour"] = row["sample"].get("local_hour")
                rows_by_day[day].append(rec)
        print(day, len(train), len(rows_by_day[day]),
              {a: round(statistics.mean(r[a] for r in rows_by_day[day]), 4) for a in ARMS}, flush=True)
    out = {"covered_rows": covered, "pairs": {}}
    sdays = sorted(rows_by_day)
    half = sdays[len(sdays) // 2]
    for arm in ("F", "F_cloud", "F_precip", "F_wind"):
        res = {}
        for name, keep_day, keep_row in (
            ("all", lambda d: True, lambda r: True),
            ("hours_08_15", lambda d: True, lambda r: r["hour"] is not None and 8 <= int(r["hour"]) < 15),
            ("first_half", lambda d: d < half, lambda r: True),
            ("second_half", lambda d: d >= half, lambda r: True),
        ):
            per_day = {d: [r[arm] - r["B"] for r in rs if keep_row(r)] for d, rs in rows_by_day.items() if keep_day(d)}
            per_day = {d: v for d, v in per_day.items() if v}
            allv = [v for vs in per_day.values() for v in vs]
            ci = RZ.boot(per_day)
            res[name] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)],
                         "n_days": len(per_day), "n": len(allv),
                         "days_better": sum(1 for v in per_day.values() if statistics.mean(v) < 0)}
        a = res["all"]
        res["verdict"] = "INSUFFICIENT" if a["n_days"] < 20 else ("PASS" if a["ci"][1] < 0 else "REFUSED")
        out["pairs"][f"{arm}-B"] = res
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    for p, r in out["pairs"].items():
        print(p, r["verdict"], {k: (v["delta"], v["ci"], f"{v['days_better']}/{v['n_days']}") for k, v in r.items() if k != "verdict"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
