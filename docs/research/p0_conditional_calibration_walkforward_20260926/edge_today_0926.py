"""Today's (2026-09-26) claimed edge per station: KL(p||m), the rule seat's model-minus-mid, half-L1 distance.  Read-only, live SSOT."""
import json, sqlite3, sys
from datetime import datetime, timezone
import numpy as np
sys.path.insert(0, "/Users/xiong/weather"); sys.path.insert(0, "/Users/xiong/weather/docs/research/p0_conditional_calibration_walkforward_20260926")
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1
from bayes_basket_target.materialization import floor_probabilities
import stations_0926 as S
DAY = "2026-09-26"
now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60); conn.execute("PRAGMA query_only=ON")
out = []
for st in sorted(r[0] for r in conn.execute("SELECT station_id FROM calibration_station_market_timezone_binding")):
    f = conn.execute("SELECT stage, probability_envelope_id FROM basket_policy_training_fact WHERE station_id=? AND event_date=? ORDER BY decision_at_utc DESC LIMIT 1", (st, DAY)).fetchone()
    if not f: continue
    env = conn.execute("SELECT bucket_ordinals_json, probabilities_json FROM basket_probability_envelope WHERE envelope_id=?", (f[1],)).fetchone()
    book = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot INDEXED BY basket_official_ws_snapshot_asof_idx WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (st, DAY, now)).fetchone()
    if not book: continue
    mk = market_reference_v1(json.loads(book[0]))
    probs = {k: float(v) for k, v in json.loads(env[1]).items()}
    if mk["state"] != "complete" or set(mk["vector"]) != set(probs): continue
    keys = sorted(probs, key=lambda k: json.loads(env[0])[k]); ords = {k: int(v) for k, v in json.loads(env[0]).items()}
    excl = [k for k in keys if probs[k] == 0.0]
    fp, _ = floor_probabilities(probs, excl)
    p = np.array([fp[k] for k in keys]); p /= p.sum()
    m = np.clip(np.array([float(mk["vector"][k]) for k in keys]), 1e-12, None); m /= m.sum()
    live = {k for k in keys if probs[k] > 0}
    kl = float(np.sum([pi * np.log(pi / mi) for pi, mi in zip(p, m) if pi > 0]))
    centre, seat, sign = S.seat({k: float(v) for k, v in zip(keys, p)}, ords, keys, live, f[0])
    idx = [keys.index(b) for b in (seat or [])]
    out.append((st, f[0], kl, float(p[idx].sum() - m[idx].sum()) if idx else None, float(np.abs(p - m).sum() / 2)))
conn.close()
kls = [o[2] for o in out]; seat_e = [o[3] for o in out if o[3] is not None]; tv = [o[4] for o in out]
print(now, "stations", len(out))
print("claimed log edge KL(p||m): mean %.3f median %.3f min %.3f max %.3f" % (np.mean(kls), np.median(kls), min(kls), max(kls)))
print("rule-seat window: model prob − market mid: mean %+.3f median %+.3f; positive %d / %d" % (np.mean(seat_e), np.median(seat_e), sum(e > 0 for e in seat_e), len(seat_e)))
print("half L1 distance between model and market (share of probability placed differently): mean %.3f" % np.mean(tv))
for stage in ("P0", "P0-M", "P1"):
    sel = [o for o in out if o[1] == stage]
    if sel: print(stage, len(sel), "KL mean %.3f" % np.mean([o[2] for o in sel]), "seat edge mean %+.3f" % np.mean([o[3] for o in sel if o[3] is not None]))
top = sorted(out, key=lambda o: -o[2])[:5]
print("largest claimed:", [(o[0], round(o[2], 3), None if o[3] is None else round(o[3], 3)) for o in top])
