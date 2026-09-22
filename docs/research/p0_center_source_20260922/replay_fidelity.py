"""Where does the replay differ from what was served?  Compare the replayed
envelope lineage layer by layer with the stored one (read-only)."""
import gzip, json, pickle, sqlite3, sys
import numpy as np
sys.path.insert(0, "/Users/xiong/weather")
import bayes_basket_target.native_ensemble_p0 as p0
SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/"
sel = pickle.load(open(SP + "csrc_replay_sel.pkl", "rb"))[::8]
raw = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
raw.execute("PRAGMA query_only=ON")


class Cur:
    def __init__(s, r): s.r = r
    def fetchall(s): return list(s.r)
    def fetchone(s): return s.r[0] if s.r else None


class Conn:
    def __init__(s, c, tz): s._c = c; s.tz = tz
    def execute(s, sql, params=()):
        if "FROM basket_target_market_registry" in sql and "DISTINCT settlement_timezone" in sql:
            real = s._c.execute(sql, params).fetchall()
            return Cur(real) if real else Cur([(s.tz,)])
        return s._c.execute(sql, params)
    def __getattr__(s, a): return getattr(s._c, a)


def J(v):
    return json.loads(v) if isinstance(v, str) else v


agg = {k: [] for k in ("centre", "weight", "bridge_lambda", "cw_input", "cw_delta", "cw_lambda", "final", "cooling")}
for s in sel:
    L0 = json.loads(raw.execute("select lineage_json from basket_probability_envelope where envelope_id=?", (s["eid"],)).fetchone()[0])
    pk = json.loads(gzip.open(s["path"]).read())
    comp = p0.compile_native_ensemble_p0(Conn(raw, s["tz"]), forward_packet=pk, decision_at_utc=s["dec"])
    L1 = comp["packet"]["native_probability_envelope"]["lineage"]
    c0, c1 = J(L0["native_calibrated_system_centers_c_json"]), J(L1["native_calibrated_system_centers_c_json"])
    agg["centre"].append(max(abs(c0[k] - c1.get(k, 1e9)) for k in c0))
    w0, w1 = J(L0["system_weights_json"]), J(L1["system_weights_json"])
    agg["weight"].append(max(abs(w0[k] - w1.get(k, 1e9)) for k in w0))
    b0, b1 = J(L0.get("consensus_bridge_json")), J(L1.get("consensus_bridge_json"))
    agg["bridge_lambda"].append(abs(float((b0 or {}).get("lambda", 0)) - float((b1 or {}).get("lambda", 0))))
    cw0, cw1 = J(L0.get("center_width_json")) or {}, J(L1.get("center_width_json")) or {}
    ip0, ip1 = cw0.get("input_probability") or {}, cw1.get("input_probability") or {}
    agg["cw_input"].append(max((abs(ip0[k] - ip1.get(k, 0)) for k in ip0), default=np.nan))
    for key, name in (("delta_c", "cw_delta"), ("width_lambda", "cw_lambda")):
        a, b = cw0.get(key), cw1.get(key)
        agg[name].append(abs(float(a) - float(b)) if a is not None and b is not None else np.nan)
    sc0, sc1 = J(L0.get("synoptic_cooling_json")) or {}, J(L1.get("synoptic_cooling_json")) or {}
    agg["cooling"].append(abs(float(sc0.get("shift_c", 0) or 0) - float(sc1.get("shift_c", 0) or 0)))
    p1 = J(comp["forward_prediction"]["probabilities_json"])
    agg["final"].append(max(abs(float(p1[k]) - s["served"][k]) for k in s["served"]))
print("n", len(sel))
for k, v in agg.items():
    v = np.array(v, dtype=float)
    ok = v[~np.isnan(v)]
    print(f"  {k:14s} n={len(ok):4d} exact(<1e-9) {np.mean(ok < 1e-9):6.1%} median {np.median(ok):.2e} p90 {np.percentile(ok, 90):.2e} max {ok.max():.2e}")
print("cw keys example:", sorted((J(L0.get("center_width_json")) or {}).keys())[:20])
print("cooling keys example:", sorted((J(L0.get("synoptic_cooling_json")) or {}).keys())[:20])
