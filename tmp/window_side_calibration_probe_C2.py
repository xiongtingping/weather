"""C2: is the posted window pointed at the right side, and does the entry
distribution's own mass predict where truth lands?  Reads only."""
import json, sqlite3, math, collections, sys, time

DB = "file:data/bayes_basket_target_ssot.db?mode=ro"
for _ in range(20):
    try:
        conn = sqlite3.connect(DB, uri=True, timeout=30)
        conn.execute("select count(*) from paper_trade_settlement").fetchone(); break
    except sqlite3.OperationalError as e:
        print("retry", e, file=sys.stderr); time.sleep(10)

pos = collections.defaultdict(list)
for r in conn.execute(
    "select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,"
    " posted_buckets_json,window_buckets_json,probabilities_json,leg_shares_json,"
    " win_probability from paper_trade_position"):
    pos[(r[0], r[1])].append(r)
setl = {(r[0], r[1]): r for r in conn.execute(
    "select station_id,event_date,stage,settled_bucket,hit from paper_trade_settlement")}

def wilson(k, n):
    if not n: return (0, 0)
    p = k / n; z = 1.96; d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (c-h, c+h)

for lane, keyf in (("posted", 5), ("filled", 6)):
    rows = []
    for k, sr in sorted(setl.items()):
        cands = sorted(pos.get(k) or [], key=lambda r: r[3])
        if not cands: continue
        r = cands[0]
        p = json.loads(r[7]); order = list(p); idx = {b:i for i,b in enumerate(order)}
        w = json.loads(r[keyf] or "[]")
        w = [b for b in w if b in idx]
        if not w or r[4] not in idx: continue
        lo, hi, c = min(idx[b] for b in w), max(idx[b] for b in w), idx[r[4]]
        s = idx.get(sr[3])
        if s is None: continue
        pred_in = sum(p[b] for b in order[lo:hi+1])
        pred_below = sum(p[b] for b in order[:lo])
        pred_above = sum(p[b] for b in order[hi+1:])
        # union over the whole chain of posted windows
        uni = set()
        for cr in cands:
            uni |= {b for b in json.loads(cr[keyf] or "[]") if b in idx}
        ulo, uhi = (min(idx[b] for b in uni), max(idx[b] for b in uni)) if uni else (lo, hi)
        # side-of-centre mass in the full entry distribution
        m_below_c = sum(p[b] for b in order[:c])
        m_above_c = sum(p[b] for b in order[c+1:])
        n_b = sum(1 for b in w if idx[b] < c); n_a = sum(1 for b in w if idx[b] > c)
        onesided = (n_b == 0) != (n_a == 0) or (n_b == 0 and n_a == 0)
        ext = "up" if n_a > n_b else ("down" if n_b > n_a else "even")
        heavier = "up" if m_above_c > m_below_c else "down"
        rows.append(dict(
            key=k, s=s, c=c, lo=lo, hi=hi, ulo=ulo, uhi=uhi,
            pred_in=pred_in, pred_below=pred_below, pred_above=pred_above,
            in_w=(lo <= s <= hi), in_u=(ulo <= s <= uhi),
            below=(s < lo), above=(s > hi),
            onesided=onesided, ext=ext, heavier=heavier,
            m_below_c=m_below_c, m_above_c=m_above_c,
            aband_mass=(m_below_c if ext == "up" else m_above_c if ext=="down" else 0.0),
            truth_aband=((s < c) if ext=="up" else (s > c) if ext=="down" else None),
            hit=sr[4], nlegs=len(w)))
    n = len(rows)
    print("\n===== window = %s | n=%d station-days (entry stage)" % (lane, n))
    ki = sum(r["in_w"] for r in rows)
    print("model says truth in window: mean %.3f   |  truth actually in window: %.3f (%d/%d) 95%%CI %.3f-%.3f"
          % (sum(r["pred_in"] for r in rows)/n, ki/n, ki, n, *wilson(ki, n)))
    kb = sum(r["below"] for r in rows); ka = sum(r["above"] for r in rows)
    print("model says BELOW window: %.3f  | actual %.3f (%d)" % (sum(r["pred_below"] for r in rows)/n, kb/n, kb))
    print("model says ABOVE window: %.3f  | actual %.3f (%d)" % (sum(r["pred_above"] for r in rows)/n, ka/n, ka))
    print("chain-union window contains truth: %.3f (%d/%d)"
          % (sum(r["in_u"] for r in rows)/n, sum(r["in_u"] for r in rows), n))
    one = [r for r in rows if r["ext"] in ("up","down")]
    print("window extends toward the HEAVIER flank of its own distribution: %d/%d"
          % (sum(r["ext"] == r["heavier"] for r in one), len(one)))
    ab = [r for r in one]
    kk = sum(bool(r["truth_aband"]) for r in ab)
    print("truth on ABANDONED side of centre: %.3f (%d/%d) 95%%CI %.3f-%.3f  | model's own mass there: %.3f"
          % (kk/len(ab), kk, len(ab), *wilson(kk, len(ab)), sum(r["aband_mass"] for r in ab)/len(ab)))
    strict = [r for r in rows if r["onesided"]]
    kk2 = sum(bool(r["truth_aband"]) for r in strict if r["truth_aband"] is not None)
    dd = len([r for r in strict if r["truth_aband"] is not None])
    print("  strictly one-sided windows only: %d/%d = %.3f" % (kk2, dd, kk2/dd if dd else 0))
