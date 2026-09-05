"""Where the truth lands relative to the served mean, by served-sigma tier (read-only)."""
import math, random, sqlite3, sys
from collections import defaultdict
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day, _evidence_floor
from bayes_basket_target.center_width_calibration import sigma_floor_lift_v1
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)
TIERS = ((0.0, 1.44, "narrow <1.44"), (1.44, 2.0, "mid 1.44-2.0"), (2.0, 2.5, "wide 2.0-2.5"), (2.5, 3.0, "flat 2.5-3.0"), (3.0, 99.0, "flattest >=3.0"))
def boot_mean(vals_by_day, draws=600, seed=11):
    rng = random.Random(seed); days = list(vals_by_day)
    if not days: return (float("nan"), float("nan"))
    out = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]; xs = [v for d in pick for v in vals_by_day[d]]
        out.append(sum(xs) / len(xs))
    out.sort(); return out[int(0.025 * draws)], out[int(0.975 * draws)]
for stage in ("P0", "P0-M", "P1"):
    floor = _evidence_floor(conn, stage=stage, caller_bound=None)
    by_day = serving_cells_by_day(conn, truth, stage=stage, min_decision_at_utc=floor)
    cells = [c for d in sorted(by_day) for c in by_day[d]]
    rows = []
    for c in cells:
        p = c["probabilities"]; keys = c["ordered_keys"]; centers = c["key_centers_c"]
        actual = truth.get((c["station_id"], c["event_date"]))
        if actual is None: continue
        _lift, sigma_c, rung_c = sigma_floor_lift_v1(p, ordered_keys=keys, key_centers_c=centers, floor_rungs=0.0)
        if rung_c <= 0: continue
        total = sum(p.values()); mean_c = sum(centers[i] * p[k] for i, k in enumerate(keys)) / total
        am = max(range(len(keys)), key=lambda i: (p[keys[i]], -i)); mode_c = centers[am]
        # clamp the truth to the axis (open edge buckets) for a distance in rungs
        e_mean = (actual - mean_c) / rung_c; e_mode = (actual - mode_c) / rung_c
        mc = c.get("model_center_c"); shift = (mean_c - float(mc)) / rung_c if mc not in (None, "") else None
        rows.append((sigma_c / rung_c, e_mean, e_mode, shift, c["station_id"], c["event_date"], sum(p.values())))
    print(f"\n##### {stage}: {len(rows)} cells, {len(by_day)} days since {floor}")
    print(f"{'sigma tier':16} {'n':>5} {'bias(rungs)':>12} {'95% CI':>14} {'median':>7} {'sd':>5} {'%warm':>6} {'|e|<=1 mean':>11} {'|e|<=1 mode':>11} {'mean-model ctr':>14}")
    for lo, hi, name in TIERS:
        sel = [r for r in rows if lo <= r[0] < hi]
        if not sel: continue
        e = [r[1] for r in sel]; em = [r[2] for r in sel]; n = len(e)
        by_day_e = defaultdict(list)
        for r in sel: by_day_e[r[5]].append(r[1])
        lo_ci, hi_ci = boot_mean(by_day_e)
        mean_e = sum(e) / n; med = sorted(e)[n // 2]; sd = math.sqrt(sum((x - mean_e) ** 2 for x in e) / max(1, n - 1))
        warm = sum(1 for x in e if x > 0) / n
        in1 = sum(1 for x in e if abs(x) <= 1.0) / n; in1m = sum(1 for x in em if abs(x) <= 1.0) / n
        shifts = [r[3] for r in sel if r[3] is not None]
        sh = (sum(shifts) / len(shifts)) if shifts else float("nan")
        print(f"{name:16} {n:5d} {mean_e:+12.2f} [{lo_ci:+5.2f},{hi_ci:+5.2f}] {med:+7.2f} {sd:5.2f} {warm*100:5.0f}% {in1*100:10.0f}% {in1m*100:10.0f}% {sh:+14.2f}")
    flat = [r for r in rows if r[0] >= 2.5]
    if flat:
        by_st = defaultdict(list)
        for r in flat: by_st[r[4]].append(r[1])
        print(f"  flat days (sigma>=2.5) by station, top 10 by count: station n bias(rungs) %warm")
        for st, es in sorted(by_st.items(), key=lambda kv: -len(kv[1]))[:10]:
            print(f"    {st} {len(es):4d} {sum(es)/len(es):+6.2f} {100*sum(1 for x in es if x>0)/len(es):4.0f}%")
        days = defaultdict(int)
        for r in flat: days[r[5]] += 1
        print("  flat cells by event date:", dict(sorted(days.items())))
