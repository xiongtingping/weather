#!/usr/bin/env python3
"""TX revision direction, explored on history (descriptive; the forward protocol is written from it).

Every change of a station-day's TX between consecutive bulletins (any issue time up to the
station's local 15:00 of the day), with the P0 (served vector's mean; regression centre where
served) and the market mean current at the later bulletin's issue.  A hit: the truth lies on the
side of the current reference the revision points to.

    python3 docs/research/taf_tx_revision_20261026/explore.py
"""

from __future__ import annotations

import glob
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts", ROOT / "docs" / "research" / "taf_direction_20261026"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import run as D  # noqa: E402  (P0_SQL, MKT_SQL, market_reference_v1, NGR, TF, iso, ep, rate)

HERE = Path(__file__).resolve().parent


def bulletins(tzs):
    """(station, day) -> sorted [(issue epoch, TX, kind, local issue hour relative to the day)]."""

    out = defaultdict(dict)
    for path in sorted(glob.glob(str(D.ARCHIVE / "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s = r.get("station_id")
            if s not in tzs:
                continue
            z = ZoneInfo(tzs[s])
            try:
                t = D.ep(r["issue_time_utc"])
            except (KeyError, ValueError):
                continue
            raw = str(r.get("raw_taf") or "")
            kind = "AMD" if " AMD " in raw else "COR" if " COR " in raw else "routine"
            issued = datetime.fromtimestamp(t, tz=timezone.utc)
            for sign, val, dd, hh in re.findall(r"\bTX(M?)(\d{2})/(\d{2})(\d{2})Z", raw):
                year, month = issued.year, issued.month
                if int(dd) < issued.day - 3:
                    month += 1
                    if month == 13:
                        year, month = year + 1, 1
                try:
                    valid = datetime(year, month, int(dd), int(hh) % 24, tzinfo=timezone.utc)
                except ValueError:
                    continue
                day = valid.astimezone(z).date().isoformat()
                y, m, d = (int(x) for x in day.split("-"))
                start = datetime(y, m, d, 0, 0, tzinfo=z).timestamp()
                if t > start + 15 * 3600:
                    continue
                out[(s, day)][t] = (-float(val) if sign == "M" else float(val), kind, (t - start) / 3600.0)
    return {k: sorted((t, *v) for t, v in seq.items()) for k, seq in out.items()}


def refs(conn, s, day, t):
    row = conn.execute(D.P0_SQL, (s, day, D.iso(t))).fetchone()
    if row is None:
        return None
    pj, oj, ivj, cj = row
    ordinals, intervals = json.loads(oj), json.loads(ivj or "{}")
    ordered = sorted(ordinals, key=lambda k: ordinals[k])
    if set(ordered) - set(intervals):
        return None
    probs = {k: float(v) for k, v in json.loads(pj).items()}
    if cj and '"SERVED"' in cj:
        pre = json.loads(cj).get("pre_shift_probabilities")
        if pre:
            probs = {k: float(v) for k, v in pre.items()}
    geom = D.NGR.geometry(ordered, intervals)
    mu = D.NGR.vector_moments([probs[k] for k in ordered], geom)[0]
    m = None
    mrow = conn.execute(D.MKT_SQL, (s, day, D.iso(t))).fetchone()
    if mrow is not None:
        ref = D.market_reference_v1(json.loads(mrow[0]))
        vec = ref.get("vector") or {}
        if ref.get("state") == "complete" and set(vec) == set(ordered):
            mids = [(a + b) / 2 for a, b in zip(geom["slo"], geom["shi"])]
            tot = sum(float(vec[k]) for k in ordered)
            m = sum(float(vec[k]) * mids[i] for i, k in enumerate(ordered)) / tot
    # The belief served at t (charter 260's authority, P1 > P0-M > P0): its mean.
    srv = mu
    for stage in ("P1", "P0-M"):
        row = conn.execute(
            "SELECT probabilities_json FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
            " WHERE station_id=? AND event_date=? AND stage=? AND state='COMMITTED' AND decision_at_utc<=?"
            " ORDER BY decision_at_utc DESC LIMIT 1", (s, day, stage, D.iso(t)),
        ).fetchone()
        if row is not None:
            p = {k: float(v) for k, v in json.loads(row[0]).items()}
            if set(p) == set(ordered):
                srv = D.NGR.vector_moments([p[k] for k in ordered], geom)[0]
                break
    return mu, m, srv


def main() -> int:
    conn = sqlite3.connect(f"file:{D.DB}?mode=ro", uri=True, timeout=120)
    tzs = dict(conn.execute(
        "SELECT station_id, settlement_timezone FROM basket_probability_envelope"
        " WHERE event_date='2026-10-20' AND stage='P0' GROUP BY station_id"))
    truth = D.TF.load_truth_revisions(conn)
    events = []
    for (s, day), seq in sorted(bulletins(tzs).items()):
        if (s, day) not in truth or not ("2026-08-20" <= day <= "2026-10-24"):
            continue
        y = truth[(s, day)][-1][2]
        prev = None
        for t, tx, kind, rel in seq:
            if prev is not None and tx != prev[1]:
                r = refs(conn, s, day, t)
                if r is not None:
                    mu, m, srv = r
                    events.append({"station": s, "day": day, "step": tx - prev[1], "tx": tx, "prev_tx": prev[1],
                                   "srv": srv, "t": t,
                                   "kind": kind, "rel_h": rel, "gap_h": (t - prev[0]) / 3600.0,
                                   "y": y, "mu": mu, "m": m, "first_of_day": not any(
                                       e["station"] == s and e["day"] == day for e in events)})
            prev = (t, tx)
    conn.close()
    (HERE / "events.json").write_text(json.dumps(events))
    sg = lambda x: (x > 0) - (x < 0)  # noqa: E731

    def summary(rows, ref):
        by_day = defaultdict(list)
        for e in rows:
            r = e[ref]
            if r is None or e["y"] == r:
                continue
            by_day[e["day"]].append(sg(e["step"]) == sg(e["y"] - r))
        by_day = {k: v for k, v in by_day.items() if v}
        return D.rate(by_day) if by_day else None

    out = {"n_events": len(events), "n_station_days": len({(e["station"], e["day"]) for e in events})}
    slices = {
        "all": lambda e: True,
        "step_1": lambda e: abs(e["step"]) == 1, "step_2": lambda e: abs(e["step"]) == 2, "step_3plus": lambda e: abs(e["step"]) >= 3,
        "up": lambda e: e["step"] > 0, "down": lambda e: e["step"] < 0,
        "issued_before_day": lambda e: e["rel_h"] < 0, "issued_00_06": lambda e: 0 <= e["rel_h"] < 6,
        "issued_06_10": lambda e: 6 <= e["rel_h"] < 10, "issued_10_15": lambda e: 10 <= e["rel_h"] < 15,
        "routine": lambda e: e["kind"] == "routine", "AMD": lambda e: e["kind"] == "AMD", "COR": lambda e: e["kind"] == "COR",
        "tx_toward_p0": lambda e: sg(e["step"]) == sg(e["mu"] - e["prev_tx"]),
        "tx_away_from_p0": lambda e: sg(e["step"]) != sg(e["mu"] - e["prev_tx"]),
        "new_tx_warm_of_p0_and_up": lambda e: e["step"] > 0 and e["tx"] > e["mu"],
        "new_tx_cold_of_p0_and_down": lambda e: e["step"] < 0 and e["tx"] < e["mu"],
        "first_half": lambda e: e["day"] < "2026-09-22", "second_half": lambda e: e["day"] >= "2026-09-22",
    }
    out["slices"] = {name: {"vs_p0": summary([e for e in events if f(e)], "mu"),
                            "vs_served": summary([e for e in events if f(e)], "srv"),
                            "vs_market": summary([e for e in events if f(e)], "m")}
                     for name, f in slices.items()}
    out["stations"] = {}
    for s in sorted({e["station"] for e in events}):
        rows = [e for e in events if e["station"] == s]
        out["stations"][s] = {"vs_p0": summary(rows, "mu"), "vs_market": summary(rows, "m"), "events": len(rows)}
    (HERE / "explore_results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print("events", out["n_events"], "station-days", out["n_station_days"])
    for name, r in out["slices"].items():
        p, m, v = r["vs_p0"], r["vs_market"], r["vs_served"]
        fmt = lambda x: "—" if x is None else f"{x['rate']:.3f} [{x['ci'][0]:.3f},{x['ci'][1]:.3f}] n{x['n']}"  # noqa: E731
        print(f"{name:26s} P0 {fmt(p):30s} SERVED {fmt(v):30s} MKT {fmt(m)}")
    print("stations (vs P0 / vs market):")
    for s, r in out["stations"].items():
        p, m = r["vs_p0"], r["vs_market"]
        print(f"  {s} ev{r['events']:3d}  P0 {p['rate'] if p else '—'} n{p['n'] if p else 0}  MKT {m['rate'] if m else '—'} n{m['n'] if m else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
