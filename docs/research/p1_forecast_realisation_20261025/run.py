#!/usr/bin/env python3
"""P1 forecast-realisation features (PROTOCOL.md): walk-forward g with and without them.

    python3 docs/research/p1_forecast_realisation_20261025/run.py
"""

from __future__ import annotations

import bisect
import glob
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts", ROOT / "docs" / "research" / "taf_physics_20261025"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_p1_observation_likelihood as FP  # noqa: E402
from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
NWP = ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
ARCHIVE = ROOT / "data" / "runtime" / "taf_raw_archive"
SCORE_FROM = "2026-10-06"
DRAWS = 2000
NEW = ("real_cloud_nwp", "real_precip_nwp", "real_cloud_taf", "real_precip_taf")
ARMS = {"B": (), "A": NEW, "A_nwp": NEW[:2], "A_taf": NEW[2:]}
COVER = {"FEW": 0.19, "SCT": 0.44, "BKN": 0.75, "OVC": 1.0, "VV": 1.0}
PRECIP = ("RA", "SN", "DZ", "SH", "TS", "GR", "PL", "GS", "UP")
BASE_PHASE, BASE_FULL = tuple(OL.PHASE_FEATURES), tuple(OL.FULL_FEATURES)


def _epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def taf_index():
    by_station = defaultdict(list)
    for path in sorted(glob.glob(str(ARCHIVE / "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            awc = row.get("awc_row") or {}
            if not awc.get("fcsts"):
                continue
            try:
                issued = _epoch(row.get("issue_time_utc") or awc.get("issueTime"))
                start, end = _epoch(awc["validTimeFrom"]), _epoch(awc["validTimeTo"])
            except (KeyError, ValueError, TypeError):
                continue
            by_station[row["station_id"]].append((issued, start, end, awc))
    for v in by_station.values():
        v.sort(key=lambda x: x[0])
    return by_station


def taf_at(entries, as_of, t):
    """Base cloud and any precipitation at ``t`` in the newest bulletin issued by ``as_of`` that covers ``t``."""

    if not entries:
        return None
    i = bisect.bisect_right([e[0] for e in entries], as_of)
    for issued, start, end, awc in reversed(entries[:i]):
        if start <= t < end:
            fc = awc["fcsts"]
            base = [g for g in fc if g.get("fcstChange") in (None, "FM", "BECMG") and g.get("probability") is None]
            cur = None
            for g in sorted(base, key=lambda g: g.get("timeFrom") or 0):
                begin = g.get("timeBec") if g.get("fcstChange") == "BECMG" and g.get("timeBec") else g.get("timeFrom")
                if begin is not None and begin <= t:
                    cur = g
            if cur is None:
                return None
            cloud = max([COVER.get(str(c.get("cover") or "").upper(), 0.0) for c in cur.get("clouds") or []] or [0.0])
            wet = any(p in str(cur.get("wxString") or "") for p in PRECIP)
            for g in fc:
                if (g.get("fcstChange") in ("TEMPO", "PROB") or g.get("probability") is not None) \
                        and (g.get("timeFrom") or 0) <= t < (g.get("timeTo") or 0) \
                        and any(p in str(g.get("wxString") or "") for p in PRECIP):
                    wet = True
            return cloud, float(wet)
    return None


def nwp_tables(conn):
    cloud = defaultdict(list)
    for s, d, cap, cc in conn.execute(
        "SELECT station_id, local_date, captured_at_utc, cloudcover_mean FROM calibration_station_forecast_physics_version"
        " WHERE capture_mode='forward' AND cloudcover_mean IS NOT NULL AND local_date>='2026-09-20'"
    ):
        cloud[(s, d)].append((_epoch(cap), float(cc) / 100.0))
    precip = defaultdict(list)
    for s, d, cap, hj in conn.execute(
        "SELECT station_id, local_date, captured_at_utc, hourly_precip_mm_json FROM calibration_station_forecast_precip_hourly_version"
        " WHERE capture_mode='forward' AND local_date>='2026-09-20'"
    ):
        try:
            precip[(s, d)].append((_epoch(cap), json.loads(hj)))
        except (TypeError, ValueError):
            pass
    for v in list(cloud.values()) + list(precip.values()):
        v.sort(key=lambda x: x[0])
    return cloud, precip


def as_of_pick(versions, at):
    best = None
    for cap, value in versions:
        if cap <= at:
            best = value
        else:
            break
    return best


def add_features(samples, cloud, precip, tafs):
    filled = defaultdict(int)
    for s in samples:
        at = _epoch(s["as_of_utc"])
        key = (s["station_id"], s["event_date"])
        obs_c = s.get("cloud_cover_fraction")
        obs_p = s.get("precipitation_present")
        fc = as_of_pick(cloud.get(key, []), at)
        s["real_cloud_nwp"] = None if (fc is None or obs_c is None) else float(obs_c) - fc
        hp = as_of_pick(precip.get(key, []), at)
        hour = int(s["local_hour"]) if s.get("local_hour") is not None else None
        if hp is not None and hour is not None and obs_p is not None:
            seq = hp if isinstance(hp, list) else [hp.get(str(h)) for h in range(24)] if isinstance(hp, dict) else None
            v = seq[hour] if seq and 0 <= hour < len(seq) else None
            s["real_precip_nwp"] = None if v is None else float(obs_p) - (1.0 if float(v) >= 0.1 else 0.0)
        else:
            s["real_precip_nwp"] = None
        t = at
        tf = taf_at(tafs.get(s["station_id"]), at, t)
        s["real_cloud_taf"] = None if (tf is None or obs_c is None) else float(obs_c) - tf[0]
        s["real_precip_taf"] = None if (tf is None or obs_p is None) else float(obs_p) - tf[1]
        for k in NEW:
            filled[k] += s[k] is not None
    return dict(filled)


def features_with(extra):
    base = FP.features_of

    def f(sample):
        out = base(sample)
        for k in extra:
            v = sample.get(k)
            out[k] = None if v is None else float(v)
        return out
    return f


def boot(per_day):
    days = sorted(per_day)
    sums = np.array([sum(per_day[d]) for d in days])
    cnts = np.array([len(per_day[d]) for d in days])
    rng = np.random.default_rng(281)
    picks = rng.integers(0, len(days), (DRAWS, len(days)))
    st = sums[picks].sum(1) / cnts[picks].sum(1)
    return float(np.percentile(st, 2.5)), float(np.percentile(st, 97.5))


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120)
    roots = FP.RootCache(conn, sqlite3.connect(f"file:{NWP / 'p0_ngr_root_replay.db'}?mode=ro", uri=True))
    raw = FP.load_samples(NWP / "p1_observation_samples.db", since="2026-09-29",
                          through=datetime.now(timezone.utc).date().isoformat())
    samples, dropped = FP.label_samples(raw, roots, FP.load_truth_known_at(conn))
    cloud, precip = nwp_tables(conn)
    tafs = taf_index()
    filled = add_features(samples, cloud, precip, tafs)
    served = OL.load_artifact()
    print("samples", len(samples), "dropped", dropped, "filled", filled, flush=True)
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
            FP.features_of = features_with(extra)
            g = FP.fit_g(train)
            scores = []
            for row in test_rows:
                ev = FP.evidence_of(row, g)
                hours = OL.clock_hours(float(row["sample"]["remaining_hours"]), served, str(row["sample"]["station_id"]))
                ls = FP._log_score(row["view"]["probabilities"], ev, row["excluded"], row["truth"], OL.alpha_for(hours, served))
                scores.append(ls)
            per_arm[arm] = scores
        FP.features_of = features_with(())
        OL.PHASE_FEATURES, OL.FULL_FEATURES = BASE_PHASE, BASE_FULL
        for i in range(len(test_rows)):
            if all(per_arm[a][i] is not None for a in ARMS):
                rows_by_day[day].append({a: -per_arm[a][i] for a in ARMS})
        print(day, len(train), len(rows_by_day[day]),
              {a: round(statistics.mean(r[a] for r in rows_by_day[day]), 4) for a in ARMS}, flush=True)
    out = {"filled": filled, "pairs": {}}
    sdays = sorted(rows_by_day)
    half = sdays[len(sdays) // 2]
    for arm in ("A", "A_nwp", "A_taf"):
        res = {}
        for name, keep in (("all", lambda d: True), ("first_half", lambda d: d < half), ("second_half", lambda d: d >= half)):
            per_day = {d: [r[arm] - r["B"] for r in rs] for d, rs in rows_by_day.items() if keep(d)}
            allv = [v for vs in per_day.values() for v in vs]
            ci = boot(per_day)
            res[name] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)],
                         "n_days": len(per_day), "n": len(allv),
                         "days_better": sum(1 for v in per_day.values() if statistics.mean(v) < 0)}
        a = res["all"]
        res["verdict"] = "INSUFFICIENT" if a["n_days"] < 20 else ("PASS" if a["ci"][1] < 0 else "REFUSED")
        out["pairs"][f"{arm}-B"] = res
    out["mean"] = {a: round(statistics.mean(r[a] for rs in rows_by_day.values() for r in rs), 5) for a in ARMS}
    (HERE / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps(out, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
