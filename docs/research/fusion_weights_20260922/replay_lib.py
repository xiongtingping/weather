"""Replay harness: production P0 compile with only the weight step replaced.

``compile_with_weights(packet, decision, tz, weight_fn)`` runs
``native_ensemble_p0.compile_native_ensemble_p0`` on the archived packet at
the original decision instant.  The hook sits on
``validate_cross_native_ensemble_weights`` -- the one call between the
selection-evidence weights and the mixture -- and rewrites the caller's
``system_weights`` dict in place, so the bridge, the served centre, the
radiation shift, width/band layers and everything after them run as
production code on the new weights.  Nothing else is changed.

Read-only: mode=ro + query_only.  A per-envelope query cache answers the
identical SQL of arms 2..n from arm 1's results (weights never enter SQL).
The pruned market registry (event days <= 09-17) is answered with the
timezone the served envelope was compiled under -- the only probe that
fails on those days.
"""
import inspect, json, gzip, sqlite3, sys
sys.path.insert(0, "/Users/xiong/weather")
import bayes_basket_target.native_ensemble_p0 as p0
from bayes_basket_target.native_ensemble_oos import _capped_family_weights

DB = "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"


class _Cur:
    def __init__(self, rows, description=None):
        self._rows = list(rows)
        self._i = 0
        self.description = description
    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out
    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]
    def fetchmany(self, n=1):
        out = self._rows[self._i:self._i + n]
        self._i += len(out)
        return out
    def close(self):
        self._i = len(self._rows)
    def __iter__(self):
        while self._i < len(self._rows):
            self._i += 1
            yield self._rows[self._i - 1]


class CachingConn:
    def __init__(self):
        self._c = sqlite3.connect(DB, uri=True, timeout=60)
        self._c.execute("PRAGMA query_only=ON")
        self.cache = {}
        self.tz = None
        self.shimmed = 0
    def reset(self, tz):
        self.cache = {}
        self.tz = tz
    def execute(self, sql, params=()):
        key = (sql, tuple(params) if not isinstance(params, dict) else tuple(sorted(params.items())))
        hit = self.cache.get(key)
        if hit is None:
            cur = self._c.execute(sql, params)
            rows = cur.fetchall()
            desc = cur.description
            if "FROM basket_target_market_registry" in sql and self.tz and len(rows) != 1 and "settlement_timezone" in sql:
                self.shimmed += 1
                rows = [(self.tz,)]
            hit = (rows, desc)
            self.cache[key] = hit
        return _Cur(hit[0], hit[1])
    def __getattr__(self, n):
        return getattr(self._c, n)


STATE = {"fn": None, "cap": None}
_orig_validate = p0.validate_cross_native_ensemble_weights


def _hook(*a, **kw):
    L = inspect.currentframe().f_back.f_locals
    w = L["system_weights"]
    sp = L["system_probabilities"]
    fam = {s: str(L["family_by_system"][s]) for s in sp}
    STATE["cap"] = dict(
        prod={s: float(v) for s, v in w.items()},
        sp={s: dict(v) for s, v in sp.items()},
        centers={s: float(v) for s, v in L["calibrated_system_centers_c"].items()},
        family=fam,
        caps={str(k): float(v) for k, v in L["family_caps"].items()},
    )
    fn = STATE["fn"]
    if fn is not None:
        new = fn(STATE["cap"])
        if new is not None:
            if set(new) != set(w):
                raise RuntimeError(f"weight_keys_mismatch {sorted(new)} vs {sorted(w)}")
            for s in w:
                w[s] = float(new[s])
    return _orig_validate(*a, **kw)


p0.validate_cross_native_ensemble_weights = _hook


def capped(raw, family, caps):
    """Normalise positive raw scores, then hold families under the production
    caps with production's own spill rule; split a family's share among its
    members in proportion to their raw scores."""
    raw = {s: max(float(v), 1e-9) for s, v in raw.items()}
    tot = sum(raw.values())
    raw = {s: v / tot for s, v in raw.items()}
    fw = {}
    for s, v in raw.items():
        fw[family[s]] = fw.get(family[s], 0.0) + v
    fc = _capped_family_weights(fw, {f: caps[f] for f in fw})
    out = {s: fc[family[s]] * raw[s] / fw[family[s]] for s in raw}
    t = sum(out.values())
    return {s: v / t for s, v in out.items()}


def load_packet(path):
    return json.loads(gzip.open(path).read())


def compile_with_weights(conn, packet, decision, weight_fn):
    STATE["fn"] = weight_fn
    STATE["cap"] = None
    try:
        comp = p0.compile_native_ensemble_p0(conn, forward_packet=packet, decision_at_utc=decision)
    finally:
        STATE["fn"] = None
    fp = comp["forward_prediction"]
    return dict(
        prob=json.loads(fp["probabilities_json"]),
        weights=json.loads(fp["system_weights_json"]),
        center=fp.get("model_center_c"),
        cap=STATE["cap"],
    )
