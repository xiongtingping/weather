#!/usr/bin/env python3
"""Charter 199 with the eleven US ASOS stations let in -- PROTOCOL.md.

Two arms on the six-family replay (recheck_six_families_20260924/pass1):
the production rule as it stands, and the same rule with
``FUSION_BLEND_UNBOUND_LABEL_STATIONS`` emptied (training table and serving
both).  Everything else is production's ``overlay_system_weights`` and the
replay's ``_blend_and_score``, through ``fusion_blend_historical_20260923/
score.py``.  Writes results.json.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PASS1 = ROOT / "docs/research/recheck_six_families_20260924/pass1"
os.environ["FUSION_PASS1_DIR"] = str(PASS1)
spec = importlib.util.spec_from_file_location("score_fusion", ROOT / "docs/research/fusion_blend_historical_20260923/score.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
S.PASS1 = PASS1

ASOS = frozenset(S.FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS)
P_FLOOR = 1e-6


def run_arm(records, excluded: frozenset) -> dict:
    S.FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS = excluded
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "training.db"
            written = S.training_db(records, db)
            states, moved = S.blend_arm(records, db)
    finally:
        S.FB.FUSION_BLEND_UNBOUND_LABEL_STATIONS = ASOS
    return {"training_rows": written, "states": dict(states),
            "moved_half_l1_median": float(np.median(moved)) if moved else None}


def before_day_end(r) -> bool:
    end = datetime.fromisoformat(r["event_date"]).replace(tzinfo=ZoneInfo(r["tz"])) + timedelta(days=1)
    return S._stamp(r["at"]) < end


def day_ahead_roots(records):
    by_day = defaultdict(list)
    for r in records:
        by_day[(r["station"], r["event_date"])].append(r)
    out = []
    for (_station, day), recs in by_day.items():
        midnight = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(recs[0]["tz"]))
        before = [r for r in recs if midnight - timedelta(days=1) <= S._stamp(r["at"]) < midnight]
        if before:
            out.append(max(before, key=lambda r: r["at"]))
    return out


def pair(records, a, b, label):
    paired = [{**r, "seat": r[a], "blend": r[b]} for r in records]
    return S.compare(paired, label)


def f_ladder(vec: dict, actual_c: float) -> tuple[float, float, bool]:
    """Bucket Brier and log score on the 2°F ladder [2m-0.5, 2m+1.5)°F.

    Each integer-°C bucket's mass is spread uniformly over its 1°C interval
    and integrated onto the 2°F bins; the truth falls in the bin of its whole
    °F reading."""
    mass: dict[int, float] = defaultdict(float)
    for key, p in vec.items():
        p = float(p)
        if p <= 0.0:
            continue
        lo_f = (int(key) - 0.5) * 1.8 + 32.0
        hi_f = (int(key) + 0.5) * 1.8 + 32.0
        m = math.floor((lo_f + 0.5) / 2.0)
        while 2 * m - 0.5 < hi_f:
            a, b = max(lo_f, 2 * m - 0.5), min(hi_f, 2 * m + 1.5)
            if b > a:
                mass[m] += p * (b - a) / 1.8
            m += 1
    whole_f = math.floor(actual_c * 1.8 + 32.0 + 0.5)
    hit = whole_f // 2
    brier = sum((p - (1.0 if m == hit else 0.0)) ** 2 for m, p in mass.items())
    if hit not in mass:
        brier += 1.0
    p_hit = mass.get(hit, 0.0)
    return brier, -math.log(max(p_hit, P_FLOOR)), p_hit <= 0.0


def compare_f(records, a, b, label):
    copies = defaultdict(int)
    for r in records:
        copies[(r["station"], r["event_date"])] += 1
    w = np.array([1.0 / copies[(r["station"], r["event_date"])] for r in records])
    arr = {}
    zero = {}
    for arm in (a, b):
        scored = [f_ladder(r[arm]["vec"], r["actual"]) for r in records]
        arr[arm] = (np.array([s[0] for s in scored]), np.array([s[1] for s in scored]))
        zero[arm] = int(sum(s[2] for s in scored))
    tot = w.sum()
    mean = {arm: {"brier": float((w * arr[arm][0]).sum() / tot), "nll": float((w * arr[arm][1]).sum() / tot)} for arm in (a, b)}
    days = sorted({r["event_date"] for r in records})
    day_idx = defaultdict(list)
    for i, r in enumerate(records):
        day_idx[r["event_date"]].append(i)
    rng = np.random.default_rng(20260925)
    n_blocks = math.ceil(len(days) / S.BLOCK)
    boots = {"brier": [], "nll": []}
    for _ in range(S.BOOT):
        idx = np.concatenate([day_idx[days[(s + j) % len(days)]]
                              for s in rng.integers(0, len(days), n_blocks) for j in range(S.BLOCK)])
        ww = w[idx]
        t = ww.sum()
        boots["brier"].append(float((ww * (arr[b][0][idx] - arr[a][0][idx])).sum() / t))
        boots["nll"].append(float((ww * (arr[b][1][idx] - arr[a][1][idx])).sum() / t))
    delta = {k: mean[b][k] - mean[a][k] for k in ("brier", "nll")}
    return {"label": label, "updates": len(records),
            "station_days": len(copies), "event_days": len(days),
            "from": mean[a], "to": mean[b], "delta": delta,
            "ci95": {k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in boots.items()},
            "zero_mass_hits": zero,
            "passes_charter_211": bool(delta["brier"] < 0 and delta["nll"] < 0)}


def main() -> int:
    records = S.load_pass1()
    # Every reading below is on decisions before the local day's end (day-ahead
    # roots are a subset), so later updates are dropped before the arms run.
    records = [r for r in records if before_day_end(r)]
    records.sort(key=lambda r: (r["station"], r["at"]))
    print("updates", len(records), "stations", len({r["station"] for r in records}), flush=True)
    arms = {}
    for name, excluded in (("current", ASOS), ("lifted", frozenset())):
        arms[name] = run_arm(records, excluded)
        for r in records:
            r[name] = r["blend"]
            r[name + "_state"] = r["blend_state"]
        print(name, arms[name], flush=True)

    decided = records
    asos = [r for r in decided if r["station"] in ASOS]
    other = [r for r in decided if r["station"] not in ASOS]
    roots = day_ahead_roots(records)
    result = {
        "protocol": "PROTOCOL.md", "pass1": str(PASS1.relative_to(ROOT)), "arms": arms,
        "fleet_before_day_end": pair(decided, "current", "lifted", "全队 45 站：现役 → 放开（判据 1）"),
        "asos_before_day_end": pair(asos, "current", "lifted", "ASOS 11 站：现役（席位）→ 放开（判据 2a）"),
        "asos_before_day_end_f_ladder": compare_f(asos, "current", "lifted", "ASOS 11 站，2°F 市场梯子（判据 2b）"),
        "non_asos_before_day_end": pair(other, "current", "lifted", "非 ASOS 34 站：现役 → 放开（只报）"),
        "fleet_day_ahead_roots": pair(roots, "current", "lifted", "全队日前根（只报）"),
        "asos_day_ahead_roots": pair([r for r in roots if r["station"] in ASOS], "current", "lifted", "ASOS 日前根（只报）"),
        "asos_day_ahead_roots_f_ladder": compare_f([r for r in roots if r["station"] in ASOS], "current", "lifted", "ASOS 日前根，2°F 梯子（只报）"),
        "asos_lifted_states": dict(sorted(
            ((s, sum(1 for r in asos if r["lifted_state"] == s)) for s in {r["lifted_state"] for r in asos}),
            key=lambda x: -x[1])),
    }
    decisive = ("fleet_before_day_end", "asos_before_day_end", "asos_before_day_end_f_ladder")
    result["lift"] = all(result[k]["passes_charter_211"] for k in decisive)
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key, c in result.items():
        if not isinstance(c, dict) or "delta" not in c:
            continue
        ci = c["ci95"]
        print(f"{c['label']}: {c['station_days']} sd | Brier {c['delta']['brier']:+.5f} {[round(x, 5) for x in ci['brier']]}"
              f" LogScore {c['delta']['nll']:+.4f} {[round(x, 4) for x in ci['nll']]} | 211={c['passes_charter_211']}", flush=True)
    print("asos lifted states", result["asos_lifted_states"])
    print("LIFT =", result["lift"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
