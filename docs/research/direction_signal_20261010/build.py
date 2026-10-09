#!/usr/bin/env python3
"""PROTOCOL.md sections 1-2: signals at I1 (D-1 03:00 local), I2 (D0 03:00 local)
and at each cluster first-taker event.  Read-only on the SSOT and archive.db."""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402

OLD = ROOT / "docs/research/conditional_advantage_20261006/rows"
NEW = ROOT / "docs/research/resolution_features_20261010/rows"
EVENTS = ROOT / "docs/research/benchmark_account_0x8fbd_20261008/choice_rows.jsonl.gz"
FIRST, LAST, SPLIT_OLD = "2026-09-12", "2026-10-08", "2026-10-04"
FLOOR = 1e-3
FAMILIES = {"ECMWF_IFS_ENS_OPEN_PF50": "IFS", "ECMWF_AIFS_ENS_GLOBAL": "AIFS", "NOAA_GEFS_05": "GEFS",
            "NOAA_AIGEFS_025": "AIGEFS", "DWD_ICON_GLOBAL_EPS": "ICON", "ECCC_GEM_GLOBAL_ENS": "GEM"}
METAR_TD = re.compile(r"\s(M?\d{2})/(M?\d{2})?\s")
METAR_Q = re.compile(r"\sQ(\d{4})\b")
METAR_A = re.compile(r"\sA(\d{4})\b")


def utc(s):
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=timezone.utc)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def floored(vec):
    v = [max(float(x or 0.0), FLOOR) for x in vec]
    s = sum(v)
    return [x / s for x in v]


def argmax_low(v):
    return max(range(len(v)), key=lambda i: (v[i], -i))


def logit(x):
    x = min(max(x, 1e-6), 1 - 1e-6)
    return math.log(x / (1 - x))


def mtemp(s):
    return -float(s[1:]) if s.startswith("M") else float(s)


def load_rows():
    out = defaultdict(list)
    for folder, keep in ((OLD, lambda d: d <= SPLIT_OLD), (NEW, lambda d: d > SPLIT_OLD)):
        for name in sorted(os.listdir(folder)):
            for line in gzip.open(folder / name, "rt"):
                r = json.loads(line)
                if keep(r["d"]) and r.get("q") and r.get("p") and None not in (r.get("temps") or [None]) and None not in r["q"]:
                    r["_t"] = utc(r["t"])
                    out[(r["st"], r["d"])].append(r)
    for v in out.values():
        v.sort(key=lambda r: r["_t"])
    return out


def main():
    ssot = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    arch = sqlite3.connect(f"file:{ROOT}/data/archive.db?mode=ro", uri=True, timeout=30)
    rows = load_rows()
    stations = sorted({st for st, _d in rows})
    tz = {}
    for (st, _d), v in rows.items():
        tz[st] = v[0]["tz"]
    truth, regional, metar = {}, {}, {}
    for st in stations:
        t = {}
        for day, value, committed in ssot.execute(
                "SELECT local_date, official_tmax_c, canonical_committed_at_utc FROM calibration_station_daily_high_truth"
                f" WHERE station_id=? AND source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED'"
                " AND local_date>='2026-08-20' ORDER BY local_date, revision_no", (st,)):
            if value is None or not committed:
                continue
            prev = t.get(day)
            t[day] = (float(value), min(utc(committed), prev[1]) if prev else utc(committed))
        truth[st] = t
        reg = defaultdict(list)   # (system, day) -> [(knowable, tmax)]
        for system, day, knowable, tmax in ssot.execute(
                "SELECT ensemble_system_id, target_local_date, knowable_at_utc, forecast_tmax_c FROM calibration_regional_point_run"
                " WHERE station_id=? AND target_local_date>='2026-08-25'", (st,)):
            if knowable and tmax is not None:
                reg[(system, day)].append((utc(knowable), float(tmax)))
        for v in reg.values():
            v.sort()
        regional[st] = reg
        obs = []
        for valid, raw in arch.execute(
                "SELECT valid_at, raw_text FROM reported_trajectory_observations WHERE station_id=? AND valid_at>=? AND valid_at<?"
                " ORDER BY valid_at", (st, "2026-09-05", "2026-10-10")):
            if not raw:
                continue
            m = METAR_TD.search(" " + raw + " ")
            if not m:
                continue
            temp = mtemp(m.group(1)); dew = mtemp(m.group(2)) if m.group(2) else None
            q = METAR_Q.search(raw); a = METAR_A.search(raw)
            pres = float(q.group(1)) if q else (float(a.group(1)) / 100.0 * 33.8639 if a else None)
            obs.append((utc(valid), temp, dew, pres))
        metar[st] = obs
    ssot.close(); arch.close()

    def decision_at(st, day, instant, max_age_h=6.0):
        v = rows.get((st, day)) or []
        cands = [r for r in v if r["_t"] <= instant and (instant - r["_t"]).total_seconds() <= max_age_h * 3600]
        return cands[-1] if cands else None

    def market_mu(r):
        q = floored(r["q"]); return sum(a * b for a, b in zip(q, r["temps"]))

    def instants_for(st, day):
        zone = ZoneInfo(tz[st]); d = datetime.fromisoformat(day)
        return {"I1": (d - timedelta(days=1)).replace(hour=3, tzinfo=zone).astimezone(timezone.utc),
                "I2": d.replace(hour=3, tzinfo=zone).astimezone(timezone.utc)}

    def nearest_obs(obs, when, tol_min=90):
        times = [o[0] for o in obs]
        i = bisect_right(times, when) - 1
        best = None
        for j in (i, i + 1):
            if 0 <= j < len(obs) and abs((obs[j][0] - when).total_seconds()) <= tol_min * 60 and obs[j][0] <= when + timedelta(minutes=0):
                if best is None or abs((obs[j][0] - when).total_seconds()) < abs((best[0] - when).total_seconds()):
                    best = obs[j]
        return best

    def signals(st, day, instant, tag):
        r = decision_at(st, day, instant)
        if r is None:
            return None
        names, temps = r["names"], [float(x) for x in r["temps"]]
        rung = float(r["rung"] or 1.0)
        q, p = floored(r["q"]), floored(r["p"])
        M = argmax_low(q); TM = temps[M]
        Qp, Qm = sum(q[M + 1:]), sum(q[:M]); Pp, Pm = sum(p[M + 1:]), sum(p[:M])
        if r["y"] not in names:
            return None
        yk = names.index(r["y"])
        out = {"st": st, "d": day, "tag": tag, "instant": instant.isoformat(), "stage": r["stage"], "age_min": (instant - r["_t"]).total_seconds() / 60,
               "M": M, "y_side": (1 if yk > M else -1 if yk < M else 0), "q_mode": q[M], "Lq": Qp / (Qp + Qm) if Qp + Qm > 0 else 0.5,
               "x": {}}
        x = out["x"]
        x["S1 我方分布倾向"] = logit(Pp / (Pp + Pm)) if Pp + Pm > 0 else None
        # S2 from the newest P0 decision at or before the instant
        p0 = [d for d in (rows.get((st, day)) or []) if d["stage"] == "P0" and d["_t"] <= instant and d.get("centres")]
        cen = p0[-1]["centres"] if p0 else {}
        w = (p0[-1].get("w") or {}) if p0 else {}
        for sid, fam in FAMILIES.items():
            x[f"S2 {fam}中心"] = ((float(cen[sid]) - TM) / rung) if sid in cen and cen[sid] is not None else None
        seated = {k: float(v) for k, v in w.items() if v and k in cen and cen[k] is not None}
        x["S2 六家加权中心"] = ((sum(v * float(cen[k]) for k, v in seated.items()) / sum(seated.values()) - TM) / rung) if seated else None
        # S3 regional deterministic, bias-corrected on the 10 previous truth-known days at the same relative instant
        reg = regional[st]; vals = []
        systems = sorted({s for s, _d in reg})
        for system in systems:
            runs = [v for k, v in reg[(system, day)] if k <= instant] if (system, day) in reg else []
            if not runs:
                x[f"S3 {system}"] = None
                continue
            errs = []
            for back in range(1, 25):
                d2 = (datetime.fromisoformat(day) - timedelta(days=back)).date().isoformat()
                tr = truth[st].get(d2)
                if not tr or tr[1] > instant or (system, d2) not in reg:
                    continue
                inst2 = instants_for(st, d2)[tag] if tag in ("I1", "I2") else instant - timedelta(days=back)
                r2 = [v for k, v in reg[(system, d2)] if k <= inst2]
                if r2:
                    errs.append(tr[0] - r2[-1])
                if len(errs) >= 10:
                    break
            if len(errs) < 5:
                x[f"S3 {system}"] = None
                continue
            val = (runs[-1] + statistics.fmean(errs) - TM) / rung
            x[f"S3 {system}"] = val; vals.append(val)
        x["S3 区域等权"] = statistics.fmean(vals) if vals else None
        # S4 METAR
        obs = [o for o in metar[st] if o[0] <= instant]
        now = obs[-1] if obs and (instant - obs[-1][0]).total_seconds() <= 3 * 3600 else None
        ago = nearest_obs(metar[st], (now[0] - timedelta(hours=24)) if now else instant, 90) if now else None
        x["S4 ΔT24"] = (now[1] - ago[1]) if now and ago else None
        x["S4 ΔTd24"] = (now[2] - ago[2]) if now and ago and now[2] is not None and ago[2] is not None else None
        x["S4 ΔP24"] = (now[3] - ago[3]) if now and ago and now[3] is not None and ago[3] is not None else None
        zone = ZoneInfo(tz[st]); local = instant.astimezone(zone)
        last_day = (local - timedelta(days=1)).date()
        lo = datetime(last_day.year, last_day.month, last_day.day, tzinfo=zone).astimezone(timezone.utc)
        hi = lo + timedelta(days=1)
        dayobs = [o[1] for o in metar[st] if lo <= o[0] < hi]
        x["S4 持续性(昨日METAR最高−众数)"] = ((max(dayobs) - TM) / rung) if len(dayobs) >= 12 else None
        # S5 market error persistence
        x["S5 市场误差延续"] = None
        for back in range(1, 4):
            d2 = (datetime.fromisoformat(day) - timedelta(days=back)).date().isoformat()
            tr = truth[st].get(d2)
            if not tr or tr[1] > instant:
                continue
            inst2 = instants_for(st, d2)[tag] if tag in ("I1", "I2") else instant - timedelta(days=back)
            r2 = decision_at(st, d2, inst2)
            if r2 is not None:
                x["S5 市场误差延续"] = (tr[0] - market_mu(r2)) / float(r2["rung"] or 1.0)
            break
        # S6 market drift
        v = rows.get((st, day)) or []
        early = [d for d in v if timedelta(hours=4) <= instant - d["_t"] <= timedelta(hours=8)]
        x["S6 市场漂移"] = ((market_mu(r) - market_mu(early[0])) / rung) if early else None
        return out

    out = []
    for (st, day) in sorted(rows):
        if not FIRST <= day <= LAST:
            continue
        for tag, inst in instants_for(st, day).items():
            s = signals(st, day, inst, tag)
            if s:
                out.append(s)
    # cluster events: off-mode YES first takes
    ev_out = []
    for line in gzip.open(EVENTS, "rt"):
        e = json.loads(line)
        chosen = [a for a in e["alts"] if a.get("chosen")]
        if not chosen or chosen[0]["side"] != "Yes" or chosen[0]["ord"] == e["mode_ord"]:
            continue
        if e["station"] not in tz:
            continue
        s = signals(e["station"], e["event_date"], utc(e["ts"]), "EV")
        if s and s["M"] == e["mode_ord"]:
            s["their_side"] = 1 if chosen[0]["ord"] > e["mode_ord"] else -1
            ev_out.append(s)
    with gzip.open(HERE / "signals.jsonl.gz", "wt") as fh:
        for s in out + ev_out:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    cov = defaultdict(int)
    for s in out:
        for k, v in s["x"].items():
            cov[(s["tag"], k)] += v is not None
    print("instants", len(out), "events", len(ev_out))
    for (tag, k), n in sorted(cov.items()):
        print(tag, k, n)


if __name__ == "__main__":
    main()
