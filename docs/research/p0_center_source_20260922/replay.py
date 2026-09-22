"""P0 centre source, step 3 (read-only): recompile each selected P0 envelope with
production's compile_native_ensemble_p0 at its original decision time, under
arms that change ONLY the single-system bias shift:
  prod  production shift b (identity hook -- fidelity check against served)
  raw   0                  (members' own mean)
  flip  -b
  rsA   s*b, s = +/-1 seeded per (envelope, system);  rsB = -s*b  (antithetic pair)
The hook wraps native_ensemble_p0._resolved_bias_c (the one resolver that sets
both the system distribution and the system centre); provisional systems'
centres (set outside the resolver) are rewritten at the bridge entry from the
packet's members + the arm's shift so the bridge sees consistent centres.
Market-registry timezone shim as in the audit (registry rows for <=09-17 are
trimmed).  Output pickle per worker slice.
"""
import gzip, hashlib, json, os, pickle, resource, sqlite3, sys, time
sys.path.insert(0, "/Users/xiong/weather")
import bayes_basket_target.native_ensemble_p0 as p0

SP = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/"
ARCH = "/Users/xiong/weather/data/runtime/bayes_basket_target_nwp/forward_packet_archive"
k, n = int(sys.argv[1]), int(sys.argv[2])
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else None
ARMS = ["prod", "raw", "flip", "rsA", "rsB"]
sel = pickle.load(open(SP + "csrc_replay_sel.pkl", "rb"))[k::n]
if LIMIT:
    sel = sel[:LIMIT]

orig_resolver = p0._resolved_bias_c
orig_bridge = p0.bridged_cross_probability
S = {"arm": "prod", "eid": "", "calls": {}}


def sign_for(mean):
    h = hashlib.md5(f"{S['eid']}|{round(mean, 6)}".encode()).hexdigest()
    return 1.0 if int(h[:8], 16) % 2 == 0 else -1.0


def arm_shift(b, mean):
    a = S["arm"]
    if a == "prod": return b
    if a == "raw": return 0.0
    if a == "flip": return -b
    if a == "rsA": return sign_for(mean) * b
    if a == "rsB": return -sign_for(mean) * b
    raise RuntimeError(a)


def resolver(mean, parameters):
    b = orig_resolver(mean, parameters)
    out = arm_shift(b, float(mean))
    S["calls"][round(float(mean), 9)] = out
    return out


def bridge(**kw):
    centres = kw["system_centers_c"]
    for sid in list(centres):
        vals = S["members"].get(sid)
        if vals:
            m = sum(vals) / len(vals)
            shift = S["calls"].get(round(m, 9))
            if shift is not None:
                centres[sid] = m + shift      # in place: downstream readers see the same dict
    return orig_bridge(**kw)


p0._resolved_bias_c = resolver
p0.bridged_cross_probability = bridge

raw = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
raw.execute("PRAGMA query_only=ON")


class Cur:
    def __init__(s, r): s.r = r
    def fetchall(s): return list(s.r)
    def fetchone(s): return s.r[0] if s.r else None


class Conn:
    tz = None
    shim = 0
    def __init__(s, c): s._c = c
    def execute(s, sql, params=()):
        if "FROM basket_target_market_registry" in sql and "DISTINCT settlement_timezone" in sql:
            real = s._c.execute(sql, params).fetchall()
            if real: return Cur(real)
            Conn.shim += 1
            return Cur([(s.tz,)])
        return s._c.execute(sql, params)
    def __getattr__(s, a): return getattr(s._c, a)


conn = Conn(raw)
out = []
t0 = time.time()
for i, r in enumerate(sel):
    pk = json.loads(gzip.open(r["path"]).read())
    S["members"] = {s["ensemble_system_id"]: [float(m["raw_tmax_c"]) for m in s["members"]] for s in pk["systems"]}
    S["eid"] = r["eid"]
    conn.tz = r["tz"]
    res = dict(eid=r["eid"], st=r["st"], day=r["day"], tier=r["tier"], arms={}, err={})
    sh0 = Conn.shim
    for arm in ARMS:
        S["arm"] = arm
        S["calls"] = {}
        try:
            comp = p0.compile_native_ensemble_p0(conn, forward_packet=pk, decision_at_utc=r["dec"])
            fp = comp["forward_prediction"]
            res["arms"][arm] = dict(prob={b: float(x) for b, x in json.loads(fp["probabilities_json"]).items()},
                                    centres=json.loads(fp["native_calibrated_system_centers_c_json"]) if fp.get("native_calibrated_system_centers_c_json") else None,
                                    iv=json.loads(fp["bucket_intervals_c_json"]) if fp.get("bucket_intervals_c_json") else None)
        except Exception as e:
            res["err"][arm] = repr(e)[:300]
    S["arm"] = "prod"
    res["shim"] = Conn.shim > sh0
    out.append(res)
    if (i + 1) % 25 == 0:
        print(f"{k}/{n} {i + 1}/{len(sel)} {time.time() - t0:.0f}s rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1_000_000}MB", flush=True)
pickle.dump(out, open(SP + f"csrc_replay_{k}of{n}.pkl", "wb"))
print(f"done {k}/{n} n={len(out)} errs={sum(1 for x in out if x['err'])} sec={time.time() - t0:.0f}")
