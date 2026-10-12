"""Round 44 (熊先生 10-12, after charter 267 补记五 recalibrated F):
「剩下和对标账号的选桶方向性偏差，概率质量、均值、众数不能解释吗？」 and
「f和p0的概率有什么区别？」.

(A) F against P0.  Every roster station-day, event days 09-28..10-11, at the
listing instant + 1 h and + 3 h (strict as-of as in round 39: runs knowable
then, truths at least two calendar days old): production F (recalibrated),
F_base (before the recalibration), and the served vector (P1 > P0-M > P0; at
D+2 it is P0).  F is one temperature -> one bucket; P0 is a probability on
every bucket.  Agreement of F's bucket with P0's mode, P0's probability on
F's bucket, P0's mean temperature - F, hits on the truth (F, F_base, P0 mode,
P0 mean's bucket), MAE (F, F_base, P0 mean), P0's own LogScore and Brier.

(B) The cluster's pre-formation first YES (rounds 40-43, 227 picks) against
the NEW F.  Where it sits relative to F and F_base; on the picks one bucket
off F, how often each mass / mean / mode signal points the right way; single
bucket rules built from them (no fitting: the bucket of P0's mode, mean,
median; of the members' mean; the members' vote mode; F_base) and a
leave-one-event-day-out choice model over candidate buckets F-2..F+2 with
those signals; whether any signal tells WHEN the cluster leaves F (AUC).
"""
import collections
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

here = Path(__file__)
src = here.with_name("cluster_pick_model_r40.py").read_text()
ns = {"__name__": "r40", "__file__": str(here.with_name("cluster_pick_model_r40.py"))}
exec(compile(src.split("all_events = len(events)")[0], "r40_head", "exec"), ns)
events, bucket, prod_f = ns["events"], ns["bucket"], ns["prod_f"]
r39 = ns["ns"]
conn, ALL_TRUTHS, ROSTER = r39["conn"], r39["ALL_TRUTHS"], r39["ROSTER"]
from bayes_basket_target import listing_forecast_f as F  # noqa: E402


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ladder(st, d):
    """label -> (lo, hi) in C from the registry, sorted; open tails closed by the median width."""
    import json
    rows_ = conn.execute("SELECT outcome, registry_json, settlement_unit FROM basket_target_market_registry"
                         " WHERE station_id=? AND event_date=? ORDER BY refreshed_at DESC LIMIT 60", (st, d)).fetchall()
    seen = {}
    for outcome, rj, unit in rows_:
        if outcome in seen:
            continue
        try:
            r = json.loads(rj)
        except ValueError:
            continue
        lo = None if r.get("lower") is None else float(r["lower"]) - 0.5
        hi = None if r.get("upper") is None else float(r["upper"]) + 0.5
        if str(unit).upper().startswith("F"):
            lo = None if lo is None else (lo - 32.0) * 5.0 / 9.0
            hi = None if hi is None else (hi - 32.0) * 5.0 / 9.0
        seen[outcome] = [lo, hi]
    if len(seen) < 3:
        return None
    items = sorted(seen.items(), key=lambda kv: kv[1][0] if kv[1][0] is not None else -1e9)
    widths = [hi - lo for _l, (lo, hi) in items if lo is not None and hi is not None]
    if not widths:
        return None
    w = statistics.median(widths)
    if items[0][1][0] is None:
        items[0][1][0] = items[0][1][1] - w
    if items[-1][1][1] is None:
        items[-1][1][1] = items[-1][1][0] + w
    return [lab for lab, _e in items], [e for _l, e in items]


def served(st, d, t):
    for stage in ("P1", "P0-M", "P0"):
        row = conn.execute("SELECT history_id FROM basket_probability_bridge_history WHERE station_id=? AND event_date=?"
                           " AND stage=? AND state='COMMITTED' AND decision_at_utc<=? ORDER BY decision_at_utc DESC LIMIT 1",
                           (st, d, stage, iso(t))).fetchone()
        if row:
            vec = dict(conn.execute("SELECT bucket, probability FROM basket_probability_bridge_bucket_probability"
                                    " WHERE history_id=?", (row[0],)).fetchall())
            tot = sum(vec.values())
            return stage, {k: v / tot for k, v in vec.items()} if tot > 0 else None
    return None, None


def vec_stats(labels, edges, vec):
    p = np.array([vec.get(lab, 0.0) for lab in labels])
    centres = np.array([(lo + hi) / 2 for lo, hi in edges])
    mean = float(p @ centres)
    cdf = np.cumsum(p)
    med_bucket = int(np.searchsorted(cdf, 0.5))
    return p, mean, int(np.argmax(p)), min(med_bucket, len(labels) - 1)


def paired(a, b):
    """Paired difference of two 0/1 lists (a - b) with a 95% interval."""
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


# ---------------------------------------------------------------- (A) F against P0
print("(A) F against P0, every roster station-day with a ladder, a truth and a served vector")
for offset_h in (1, 3):
    rows = []
    d = date(2026, 9, 28)
    while d <= date(2026, 10, 11):
        t = F._listing_dt(d) + timedelta(hours=offset_h)
        payload = prod_f(t.timestamp())
        for key, e in payload["station_days"].items():
            st, ed = key.split("|")
            if ed != d.isoformat() or e.get("state") != "READY" or (st, ed) not in ALL_TRUTHS:
                continue
            lad = ladder(st, ed)
            if lad is None:
                continue
            labels, edges = lad
            stage, vec = served(st, ed, t)
            if not vec or not set(vec) <= set(labels):
                continue
            p, mean, mode, med = vec_stats(labels, edges, vec)
            y = ALL_TRUTHS[(st, ed)]
            yb = bucket(edges, y)
            fb, bb = bucket(edges, e["f_c"]), bucket(edges, e["f_base_c"])
            rows.append({"st": st, "stage": stage, "fb": fb, "bb": bb, "mode": mode, "med": med, "meanb": bucket(edges, mean),
                         "mean": mean, "f": e["f_c"], "fbase": e["f_base_c"], "y": y, "yb": yb,
                         "p_f": float(p[fb]), "p_mode": float(p[mode]), "p_y": float(p[yb]),
                         "brier": float(((p - np.eye(len(p))[yb]) ** 2).sum())})
        d += timedelta(days=1)
    n = len(rows)
    if not n:
        continue
    stages = collections.Counter(r["stage"] for r in rows)
    diff = collections.Counter(max(-2, min(2, r["fb"] - r["mode"])) for r in rows)
    print(f"  listing + {offset_h} h: n {n} ({dict(stages)})")
    print(f"    F bucket = P0 mode {sum(r['fb'] == r['mode'] for r in rows) / n:.1%};  F - P0 mode in buckets:"
          f" <=-2 {diff[-2] / n:.0%}, -1 {diff[-1] / n:.0%}, 0 {diff[0] / n:.0%}, +1 {diff[1] / n:.0%}, >=+2 {diff[2] / n:.0%}"
          f"   (F_base bucket = P0 mode {sum(r['bb'] == r['mode'] for r in rows) / n:.1%})")
    print(f"    P0 probability on F's bucket {statistics.mean(r['p_f'] for r in rows):.3f}, on its own mode {statistics.mean(r['p_mode'] for r in rows):.3f};"
          f"  P0 mean - F {statistics.mean(r['mean'] - r['f'] for r in rows):+.2f} C, P0 mean - F_base {statistics.mean(r['mean'] - r['fbase'] for r in rows):+.2f} C")
    hits = {k: [r[k] == r["yb"] for r in rows] for k in ("fb", "bb", "mode", "meanb", "med")}
    print(f"    hits: F {statistics.mean(hits['fb']):.1%}, F_base {statistics.mean(hits['bb']):.1%}, P0 mode {statistics.mean(hits['mode']):.1%},"
          f" P0 mean bucket {statistics.mean(hits['meanb']):.1%}, P0 median bucket {statistics.mean(hits['med']):.1%}"
          f"   F - P0 mode {paired(hits['fb'], hits['mode'])}")
    print(f"    MAE: F {statistics.mean(abs(r['y'] - r['f']) for r in rows):.3f}, F_base {statistics.mean(abs(r['y'] - r['fbase']) for r in rows):.3f},"
          f" P0 mean {statistics.mean(abs(r['y'] - r['mean']) for r in rows):.3f} C;  bias (truth - x): F {statistics.mean(r['y'] - r['f'] for r in rows):+.2f},"
          f" F_base {statistics.mean(r['y'] - r['fbase'] for r in rows):+.2f}, P0 mean {statistics.mean(r['y'] - r['mean'] for r in rows):+.2f}")
    print(f"    P0 on the truth: probability {statistics.mean(r['p_y'] for r in rows):.3f}, LogScore {statistics.mean(-math.log(max(1e-3, r['p_y'])) for r in rows):.3f},"
          f" Brier {statistics.mean(r['brier'] for r in rows):.3f}")
    dis = [r for r in rows if r["fb"] != r["mode"]]
    if dis:
        print(f"    where F and P0's mode differ (n {len(dis)}): truth in F's bucket {statistics.mean(r['fb'] == r['yb'] for r in dis):.1%},"
              f" in P0's mode {statistics.mean(r['mode'] == r['yb'] for r in dis):.1%}; P0 gave F's bucket {statistics.mean(r['p_f'] for r in dis):.3f}")

# ---------------------------------------------------------------- (B) the cluster against the new F
print("\n(B) the cluster's pre-formation first YES against the recalibrated F")
for ev in events:
    e = prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    edges = ev["edges"]
    ev["bb"] = bucket(edges, e["f_base_c"])
    corrected = [v["tmax_c"] + v.get("bias_c", 0.0) for v in e["systems"].values()]
    shift = ev["f"] - statistics.median(corrected)
    centred = [x + shift for x in corrected]
    ev["mass_up"] = sum(bucket(edges, x) > ev["fb"] for x in centred) / len(centred)
    ev["mass_dn"] = sum(bucket(edges, x) < ev["fb"] for x in centred) / len(centred)
    ev["memb_mean_b"] = bucket(edges, statistics.mean(centred))
    ev["skew"] = statistics.mean(corrected) - statistics.median(corrected)
    votes = collections.Counter(bucket(edges, x) for x in centred)
    ev["vote_mode"] = max(votes, key=lambda j: (votes[j], -abs(j - ev["fb"])))
    ev["votes"] = votes
    ev["nm"] = len(centred)
    lo, hi = edges[ev["fb"]]
    ev["pos"] = (ev["f"] - lo) / (hi - lo) - 0.5
    if ev["p0"]:
        labels_n = ev["n"]
        p = np.array([ev["p0"].get(j, 0.0) for j in range(labels_n)])
        p = p / p.sum()
        centres = np.array([(a + b) / 2 for a, b in edges])
        ev["p0_arr"] = p
        ev["p0_mean"] = float(p @ centres)
        ev["p0_mean_b"] = bucket(edges, ev["p0_mean"])
        ev["p0_med_b"] = min(int(np.searchsorted(np.cumsum(p), 0.5)), labels_n - 1)
        ev["p0_mode_b"] = int(np.argmax(p))
        ev["p0_up"] = float(p[ev["fb"] + 1:].sum())
        ev["p0_dn"] = float(p[:ev["fb"]].sum())
    else:
        ev["p0_arr"] = None

n = len(events)
off = collections.Counter(max(-2, min(2, ev["choice"] - ev["fb"])) for ev in events)
offb = collections.Counter(max(-2, min(2, ev["choice"] - ev["bb"])) for ev in events)
print(f"  {n} picks.  cluster - F (new):    <=-2 {off[-2]}, -1 {off[-1]}, 0 {off[0]}, +1 {off[1]}, >=+2 {off[2]};"
      f" mean {statistics.mean(ev['choice'] - ev['fb'] for ev in events):+.2f} buckets")
print(f"            cluster - F_base:       <=-2 {offb[-2]}, -1 {offb[-1]}, 0 {offb[0]}, +1 {offb[1]}, >=+2 {offb[2]};"
      f" mean {statistics.mean(ev['choice'] - ev['bb'] for ev in events):+.2f} buckets")
moved = [ev for ev in events if ev["fb"] != ev["bb"]]
print(f"  the recalibration moved F's bucket on {len(moved)} picks (down {sum(ev['fb'] < ev['bb'] for ev in moved)}, up {sum(ev['fb'] > ev['bb'] for ev in moved)});"
      f" there the cluster sat on F_base {sum(ev['choice'] == ev['bb'] for ev in moved)}, on the new F {sum(ev['choice'] == ev['fb'] for ev in moved)}, elsewhere"
      f" {sum(ev['choice'] not in (ev['bb'], ev['fb']) for ev in moved)}")
with_p0 = [ev for ev in events if ev["p0_arr"] is not None]
print(f"  picks with a served vector at the trade: {len(with_p0)} of {n} (the rest traded before our first P0)")

one = [ev for ev in events if abs(ev["choice"] - ev["fb"]) == 1]
print(f"\n  direction on the {len(one)} picks one bucket off the new F (up {sum(ev['choice'] > ev['fb'] for ev in one)}, down {sum(ev['choice'] < ev['fb'] for ev in one)}):")
SIGNALS = {
    "F position in its bucket": lambda ev: ev["pos"],
    "members' mass up - down": lambda ev: ev["mass_up"] - ev["mass_dn"],
    "members' mean bucket - F": lambda ev: ev["memb_mean_b"] - ev["fb"],
    "members' vote mode - F": lambda ev: ev["vote_mode"] - ev["fb"],
    "members' skew (mean - median)": lambda ev: ev["skew"],
    "P0 mass up - down": lambda ev: None if ev["p0_arr"] is None else ev["p0_up"] - ev["p0_dn"],
    "P0 mean - F (C)": lambda ev: None if ev["p0_arr"] is None else ev["p0_mean"] - ev["f"],
    "P0 mode - F": lambda ev: None if ev["p0_arr"] is None else ev["p0_mode_b"] - ev["fb"],
    "P0 median - F": lambda ev: None if ev["p0_arr"] is None else ev["p0_med_b"] - ev["fb"],
    "F_base - F (undo recalibration)": lambda ev: ev["bb"] - ev["fb"] if ev["bb"] != ev["fb"] else F_base_frac(ev),
}


def F_base_frac(ev):
    e = prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    return e["f_base_c"] - e["f_c"]


for name, fn in SIGNALS.items():
    told = [(fn(ev), ev) for ev in one if fn(ev) not in (None, 0, 0.0)]
    right = sum((v > 0) == (ev["choice"] > ev["fb"]) for v, ev in told)
    ups = sum(ev["choice"] > ev["fb"] for _v, ev in told)
    base = max(ups, len(told) - ups) / max(1, len(told))
    print(f"    {name:32s} has a sign on {len(told):3d}: right way {right / max(1, len(told)):.1%}"
          f"   (always the commoner side on the same picks: {base:.1%})")

# Combined direction, leave one event day out, against always guessing the training days' commoner side.
DIR = ["F position in its bucket", "members' mass up - down", "members' vote mode - F", "P0 mass up - down", "P0 mode - F"]
Xd = np.array([[SIGNALS[k](ev) or 0.0 for k in DIR] for ev in one])
Yd = np.array([1.0 if ev["choice"] > ev["fb"] else 0.0 for ev in one])


def logit_fit(x, y, l2=1.0):
    w = np.zeros(x.shape[1] + 1)
    xb = np.hstack([x, np.ones((len(x), 1))])
    for _ in range(3000):
        q = 1 / (1 + np.exp(-xb @ w))
        w -= 0.1 * (xb.T @ (q - y) / len(y) + l2 * np.r_[w[:-1], 0] / len(y))
    return w


ok = base_ok = 0
for day in sorted({ev["ed"] for ev in one}):
    tr = [i for i, ev in enumerate(one) if ev["ed"] != day]
    te = [i for i, ev in enumerate(one) if ev["ed"] == day]
    w = logit_fit(Xd[tr], Yd[tr])
    commoner = 1.0 if Yd[tr].mean() >= 0.5 else 0.0
    for i in te:
        ok += (1.0 if np.r_[Xd[i], 1] @ w > 0 else 0.0) == Yd[i]
        base_ok += commoner == Yd[i]
print(f"    combined ({', '.join(DIR)}), leave one day out: right way {ok / len(one):.1%}; always the commoner side {base_ok / len(one):.1%}")

print("\n  single-bucket rules, no fitting (share = cluster's bucket; hits on the truth where known):")
RULES = {
    "F (new)": lambda ev: ev["fb"],
    "F_base": lambda ev: ev["bb"],
    "members' mean bucket": lambda ev: ev["memb_mean_b"],
    "members' vote mode": lambda ev: ev["vote_mode"],
    "P0 mode": lambda ev: ev.get("p0_mode_b"),
    "P0 mean bucket": lambda ev: ev.get("p0_mean_b"),
    "P0 median bucket": lambda ev: ev.get("p0_med_b"),
}
days = sorted({ev["ed"] for ev in events})
mid = days[len(days) // 2]
for name, fn in RULES.items():
    for label, sub in (("all picks", events), ("picks with P0", with_p0)):
        sub = [ev for ev in sub if fn(ev) is not None]
        if not sub:
            continue
        same = [fn(ev) == ev["choice"] for ev in sub]
        base = [ev["fb"] == ev["choice"] for ev in sub]
        tr = [ev for ev in sub if ev["truth"] is not None]
        h1 = [fn(ev) == ev["choice"] for ev in sub if ev["ed"] < mid]
        h2 = [fn(ev) == ev["choice"] for ev in sub if ev["ed"] >= mid]
        print(f"    {name:22s} {label:14s} n {len(sub):3d}: = cluster {statistics.mean(same):.1%} (halves {statistics.mean(h1) if h1 else 0:.1%} / {statistics.mean(h2) if h2 else 0:.1%};"
              f" vs F {paired(same, base)}); hits {statistics.mean(fn(ev) == ev['truth'] for ev in tr):.1%} (cluster {statistics.mean(ev['choice'] == ev['truth'] for ev in tr):.1%})")


# Choice model over F-2..F+2, leave one event day out.
def feats(ev, j):
    d = j - ev["fb"]
    lo, hi = ev["edges"][j]
    have = ev["p0_arr"] is not None
    return [1.0 if d == 0 else 0.0, 1.0 if abs(d) == 1 else 0.0,
            -abs(ev["f"] - (lo + hi) / 2) / (hi - lo),
            ev["votes"].get(j, 0) / ev["nm"],
            float(j == ev["memb_mean_b"]), float(j == ev["vote_mode"]), float(j == ev["bb"]),
            float(ev["p0_arr"][j]) if have else 0.0, float(have and j == ev["p0_mode_b"]),
            float(have and j == ev["p0_mean_b"]), 0.0 if have else 1.0 * (d == 0)]


def fit(X, Y, l2=0.5, iters=500, lr=0.3):
    w = np.zeros(X[0].shape[1])
    for _ in range(iters):
        g = l2 * w
        for x, y in zip(X, Y):
            u = x @ w
            p = np.exp(u - u.max())
            p /= p.sum()
            g += x.T @ p - x[y]
        w -= lr * g / len(X)
    return w


cases = []
for ev in events:
    cands = [j for j in range(ev["fb"] - 2, ev["fb"] + 3) if 0 <= j < ev["n"]]
    if ev["choice"] in cands:
        cases.append((ev, cands, np.array([feats(ev, j) for j in cands]), cands.index(ev["choice"])))
hit = []
for day in days:
    tr = [c for c in cases if c[0]["ed"] != day]
    te = [c for c in cases if c[0]["ed"] == day]
    if not te:
        continue
    w = fit([c[2] for c in tr], [c[3] for c in tr])
    for ev, cands, X, y in te:
        hit.append((ev, int(np.argmax(X @ w)) == y))
acc = statistics.mean(h for _e, h in hit)
fb_acc = statistics.mean(ev["fb"] == ev["choice"] for ev, _h in hit)
h1 = [h for ev, h in hit if ev["ed"] < mid]
h2 = [h for ev, h in hit if ev["ed"] >= mid]
print(f"\n  choice model (F-2..F+2; position, members' mass, members' mean / vote mode, F_base, P0 probability / mode / mean),"
      f" leave one event day out, n {len(hit)}: = cluster {acc:.1%} (halves {statistics.mean(h1):.1%} / {statistics.mean(h2):.1%}) vs always F {fb_acc:.1%}"
      f"   {paired([h for _e, h in hit], [ev['fb'] == ev['choice'] for ev, _h in hit])}")


# When does the cluster leave F?  AUC of each signal's strength.
def auc(scores, labels):
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        return float("nan")
    return sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))


left = [ev["choice"] != ev["fb"] for ev in events]
print(f"\n  WHEN it leaves F (it does on {sum(left)} of {n}): AUC of each signal's strength (0.5 = no information)")
STRENGTH = {
    "|F position| (near an edge)": lambda ev: abs(ev["pos"]),
    "members off F's bucket": lambda ev: ev["mass_up"] + ev["mass_dn"],
    "|members' mass up - down|": lambda ev: abs(ev["mass_up"] - ev["mass_dn"]),
    "|F_base - F|": lambda ev: abs(F_base_frac(ev)),
    "members' spread": lambda ev: float(np.std([v["tmax_c"] + v.get("bias_c", 0.0) for v in prod_f(ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]["systems"].values()])),
}
for name, fn in STRENGTH.items():
    print(f"    {name:30s} {auc([fn(ev) for ev in events], left):.3f}")
lp = [ev["choice"] != ev["fb"] for ev in with_p0]
for name, fn in {"1 - P0 on F's bucket": lambda ev: 1 - float(ev["p0_arr"][ev["fb"]]),
                 "P0 mode != F": lambda ev: float(ev["p0_mode_b"] != ev["fb"]),
                 "|P0 mean - F|": lambda ev: abs(ev["p0_mean"] - ev["f"])}.items():
    print(f"    {name:30s} {auc([fn(ev) for ev in with_p0], lp):.3f}   (picks with P0, n {len(with_p0)})")
