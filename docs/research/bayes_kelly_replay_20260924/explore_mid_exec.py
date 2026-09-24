import sys, json, numpy as np
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import replay_model_belief as R
by_sd, truth, _ = R.load()
# Replace each book with a mid-price book: one level at (best bid + best ask)/2, same depth as the ask side.
mid_sd = {}
for key, evs in by_sd.items():
    out = []
    for t, stage, belief, m, asks, bids, mino in evs:
        a2, b2 = {}, {}
        for b, lv in asks.items():
            if not lv:
                continue
            ba = lv[0][0]; bb = (bids.get(b) or [[None]])[0][0]
            mid = (ba + bb) / 2 if bb is not None else ba
            depth = sum(s for _, s in lv)
            a2[b] = [[mid, depth]]; b2[b] = [[mid, depth]]
        out.append((t, stage, belief, m, a2, b2, mino))
    mid_sd[key] = out
for name, kw in (("A_mid_exec", dict(gamma=4.0)), ("C_open_only_mid_exec", dict(gamma=4.0, open_only=True))):
    r = R.run(mid_sd, truth, **kw)
    print(name, {k: (round(v, 4) if isinstance(v, float) else ({kk: round(vv, 1) for kk, vv in v.items()} if isinstance(v, dict) else v)) for k, v in r.items()}, flush=True)
