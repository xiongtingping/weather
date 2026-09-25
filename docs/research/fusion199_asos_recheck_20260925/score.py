#!/usr/bin/env python3
"""PROTOCOL.md: the ASOS lift, scored on production's own training rows and ladders.

Every settled, served day-ahead root with its systems' market-ladder
probabilities is re-weighted twice with production's overlay_system_weights,
as of its own decision instant -- current (snapshot table, ASOS at the seat)
and lifted (snapshot + the ASOS rows, exclusion emptied) -- pooled, floored at
charter 224's 1e-3 and scored by bucket Brier and log score.  Writes results.json.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import fusion_blend as FB  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

# The eleven stations production excluded until this study (the constant is gone).
ASOS = frozenset({"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"})
WORK = Path(sys.argv[1])  # scratch dir holding snapshot.db and lifted.db


def rows(db: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(
        "SELECT * FROM fusion_blend_training_fact WHERE probability_source IN ('ENVELOPE','PACKET_RECONSTRUCTION')")]
    conn.close()
    latest = {}
    for r in out:  # one scored row per station-day: its latest truth revision
        key = (r["station_id"], r["event_date"])
        if key not in latest or r["truth_revision"] > latest[key]["truth_revision"]:
            latest[key] = r
    return list(latest.values())


def weights(r: dict, db: Path) -> tuple[dict, str]:
    """Production's overlay as of the root's own decision.  The current arm
    holds the ASOS stations at the seat in ``main``; the overlay itself sees
    every station alike (the exclusion constant is deleted at the lift)."""
    served, receipt = FB.overlay_system_weights(
        station_id=r["station_id"], event_date=r["event_date"],
        decision_at_utc=r["root_decision_at_utc"], timezone_name=r["settlement_timezone"],
        system_weights=json.loads(r["seat_weights_json"]),
        family_by_system=json.loads(r["family_by_system_json"]),
        family_caps=json.loads(r["family_caps_json"]),
        lead_by_system=json.loads(r["lead_by_system_json"]),
        training_db=db, mode=FB.FUSION_BLEND_MODE_BLEND,
    )
    return served, receipt["state"]


def score(r: dict, w: dict) -> tuple[float, float]:
    keys = json.loads(r["bucket_keys_json"])
    probs = json.loads(r["probabilities_json"])
    pooled = {k: sum(float(w.get(s, 0.0)) * probs[s][i] for s in probs) for i, k in enumerate(keys)}
    total = sum(pooled.values())
    floored, _ = floor_probabilities({k: v / total for k, v in pooled.items()})
    hit = keys[int(r["truth_bucket_index"])]
    brier = sum((floored[k] - (1.0 if k == hit else 0.0)) ** 2 for k in keys)
    return brier, -math.log(max(floored[hit], 1e-12))


def compare(items, label):
    days = sorted({it["day"] for it in items})
    by_day = defaultdict(list)
    for i, it in enumerate(items):
        by_day[it["day"]].append(i)
    d_b = [it["lift"][0] - it["cur"][0] for it in items]
    d_n = [it["lift"][1] - it["cur"][1] for it in items]
    rng = random.Random(20260925)
    boots_b, boots_n = [], []
    for _ in range(2000):
        idx = [i for d in rng.choices(days, k=len(days)) for i in by_day[d]]
        boots_b.append(sum(d_b[i] for i in idx) / len(idx))
        boots_n.append(sum(d_n[i] for i in idx) / len(idx))
    boots_b.sort(); boots_n.sort()
    n = len(items)
    mean = lambda arm, j: sum(it[arm][j] for it in items) / n  # noqa: E731
    delta = {"brier": sum(d_b) / n, "nll": sum(d_n) / n}
    return {
        "label": label, "station_days": n, "days": len(days),
        "current": {"brier": mean("cur", 0), "nll": mean("cur", 1)},
        "lifted": {"brier": mean("lift", 0), "nll": mean("lift", 1)},
        "delta": delta,
        "ci95": {"brier": [boots_b[50], boots_b[1949]], "nll": [boots_n[50], boots_n[1949]]},
        "passes_charter_211": delta["brier"] < 0 and delta["nll"] < 0,
    }


def main() -> int:
    snapshot, lifted_db = WORK / "snapshot.db", WORK / "lifted.db"
    items, states = [], defaultdict(lambda: defaultdict(int))
    for r in sorted(rows(lifted_db), key=lambda r: (r["station_id"], r["event_date"])):
        asos = r["station_id"] in ASOS
        if asos:
            cur_w, cur_state = json.loads(r["seat_weights_json"]), "SEAT_LABELS_NOT_BOUND"
        else:
            cur_w, cur_state = weights(r, snapshot)
        lift_w, lift_state = weights(r, lifted_db)
        states["asos" if asos else "other"][f"{cur_state} -> {lift_state}"] += 1
        items.append({"station": r["station_id"], "day": r["event_date"], "asos": asos,
                      "cur": score(r, cur_w), "lift": score(r, lift_w),
                      "moved": 0.5 * sum(abs(float(lift_w.get(s, 0)) - float(cur_w.get(s, 0))) for s in set(cur_w) | set(lift_w))})
    asos = [it for it in items if it["asos"]]
    other = [it for it in items if not it["asos"]]
    result = {
        "protocol": "PROTOCOL.md",
        "asos": compare(asos, "ASOS 11 站：现役（席位）→ 放开（判据 1）"),
        "fleet": compare(items, "全队：现役 → 放开（判据 2）"),
        "non_asos": compare(other, "非 ASOS 站的牵动（只报）"),
        "asos_moved_half_l1_median": sorted(it["moved"] for it in asos)[len(asos) // 2],
        "states": {k: dict(v) for k, v in states.items()},
    }
    result["lift"] = result["asos"]["passes_charter_211"] and result["fleet"]["passes_charter_211"]
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key in ("asos", "fleet", "non_asos"):
        c = result[key]
        print(f"{c['label']}: {c['station_days']} sd / {c['days']} d | Brier {c['current']['brier']:.5f}->{c['lifted']['brier']:.5f} "
              f"({c['delta']['brier']:+.5f} {[round(x, 5) for x in c['ci95']['brier']]}) LogScore {c['current']['nll']:.4f}->{c['lifted']['nll']:.4f} "
              f"({c['delta']['nll']:+.4f} {[round(x, 4) for x in c['ci95']['nll']]}) 211={c['passes_charter_211']}")
    print("states", json.dumps(result["states"], ensure_ascii=False))
    print("asos moved median", round(result["asos_moved_half_l1_median"], 3), "LIFT =", result["lift"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
