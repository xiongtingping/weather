"""Weight arms for the fusion-weight comparison (research only, no serving path).

Skill source: per-system walk-forward single-system forecasts (b_history.py),
scored against the truth revision knowable at the decision instant.

As-of, both sides: a station-day enters a skill window only if its first
truth revision was committed at or before the decision instant, its event
date is strictly before the decided event day, and the scored value is the
latest revision committed by then; the window is the N most recent such days
(so it is bounded on the old side too).

Every arm divides weight over exactly the systems production seated; only the
split changes.  Scores are compared on paired support: a window day counts
only if every seated system has a forecast on it, so no system is judged on
easier days than another.  All skill arms go through production's family cap
(``_capped_family_weights``) exactly as the served rule does.
"""
from __future__ import annotations

import bisect
import hashlib
import math
import pickle
import random
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.native_ensemble_oos import (  # noqa: E402
    _capped_family_weights,
    _metrics,
    _seat_weights,
)
from bayes_basket_target.stable_pool import (  # noqa: E402
    STABLE_POOL_KAPPA,
    stable_pool_weights,
)

HIST = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw/history.pkl"
DEFAULT_CAP = 0.75


def _z(ts: str) -> str:
    return str(ts).replace("Z", "").replace("+00:00", "")[:26]


def capped(raw, family, caps):
    """Normalise positive scores, hold families under the production caps with
    production's own spill rule, split a family's share among its members in
    proportion to their scores."""
    raw = {s: max(float(v), 1e-9) for s, v in raw.items()}
    tot = sum(raw.values())
    raw = {s: v / tot for s, v in raw.items()}
    fw = defaultdict(float)
    for s, v in raw.items():
        fw[family[s]] += v
    if len(fw) < 2:
        return raw
    fc = _capped_family_weights(dict(fw), {f: float(caps.get(f, DEFAULT_CAP)) for f in fw})
    out = {s: fc[family[s]] * raw[s] / fw[family[s]] for s in raw}
    t = sum(out.values())
    return {s: v / t for s, v in out.items()}


def equal(systems):
    return {s: 1.0 / len(systems) for s in systems}


def rotate(weights, key):
    ks = sorted(weights)
    vals = [weights[s] for s in ks]
    if len(ks) < 2:
        return dict(weights)
    rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:8], 16))
    off = rng.randrange(1, len(ks))
    return {s: vals[(i + off) % len(ks)] for i, s in enumerate(ks)}


class Skill:
    def __init__(self, path=HIST):
        H = pickle.load(open(path, "rb"))
        self.truth = H["truth"]  # (st, day) -> [(commit, value, rev)]
        self.first_commit = {k: min(_z(x[0]) for x in v) for k, v in self.truth.items()}
        # per station: sorted [(day, first_commit)]
        self.days_by_station = defaultdict(list)
        for (st, d), c in self.first_commit.items():
            self.days_by_station[st].append((d, c))
        for st in self.days_by_station:
            self.days_by_station[st].sort()
        self.stations = sorted(self.days_by_station)
        # aggregated per-case metrics: key -> {value: [se_sum, crps_sum, n, err_sum, raw_res_sum]}
        self.agg_all = defaultdict(dict)    # (st, day, sys)
        self.agg_lead = defaultdict(dict)   # (st, day, sys, lead)
        self.cases = H["cases"]
        for c in self.cases:
            k = (c["station"], c["day"])
            if k not in self.truth:
                continue
            for val in {x[1] for x in self.truth[k]}:
                m = _metrics(c["prob"], val)
                se = (c["centre"] - val) ** 2
                for key, store in (((c["station"], c["day"], c["system"]), self.agg_all),
                                   ((c["station"], c["day"], c["system"], c["lead"]), self.agg_lead)):
                    a = store[key].setdefault(val, [0.0, 0.0, 0, 0.0, 0.0])
                    a[0] += se
                    a[1] += m["crps"]
                    a[2] += 1
                    a[3] += val - c["centre"]
                    a[4] += val - c["raw_mean"]
        self._memo = {}

    # -- truth as-of --------------------------------------------------------
    def value_at(self, st, day, t):
        revs = self.truth.get((st, day))
        if not revs:
            return None
        best = None
        for com, val, rev in revs:
            if _z(com) <= t and (best is None or rev > best[0]):
                best = (rev, val)
        return None if best is None else best[1]

    def final_value(self, st, day):
        revs = self.truth.get((st, day))
        return None if not revs else max(revs, key=lambda x: x[2])[1]

    def station_window(self, st, d, t, n):
        out = [day for day, c in self.days_by_station.get(st, ()) if day < d and c <= t]
        return out[-n:] if n else out

    def fleet_window(self, d, t, n):
        key = ("fw", d, t, n)
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        days = set()
        for st in self.stations:
            for day, c in self.days_by_station[st]:
                if day < d and c <= t:
                    days.add(day)
        out = sorted(days)[-n:]
        self._memo[key] = out
        return out

    def unit(self, st, day, sys_, lead, t):
        val = self.value_at(st, day, t)
        if val is None:
            return None
        store = self.agg_all if lead is None else self.agg_lead
        key = (st, day, sys_) if lead is None else (st, day, sys_, lead)
        a = store.get(key, {}).get(val)
        if a is None or a[2] == 0:
            return None
        return (a[0] / a[2], a[1] / a[2], a[3] / a[2], a[4] / a[2])

    def window_stats(self, scope, st, d, t, systems, leads, n):
        """Mean (mse, crps) per system on paired support.

        scope: 'station' | 'fleet'; leads: None (all leads) or {sys: lead}.
        Returns (stats, n_units) with stats[sys] = (mse, crps)."""
        tt = _z(t)
        if scope == "station":
            units = [(st, day) for day in self.station_window(st, d, tt, n)]
        else:
            days = self.fleet_window(d, tt, n)
            units = [(s2, day) for day in days for s2 in self.stations
                     if self.first_commit.get((s2, day), "9") <= tt]
        # memo: same units + same revision count known => same answer
        nrev = sum(1 for u in units for com, _v, _r in self.truth.get(u, ()) if _z(com) <= tt)
        mkey = ("ws", scope, st if scope == "station" else None, tuple(systems),
                None if leads is None else tuple(sorted(leads.items())), tuple(units), nrev)
        hit = self._memo.get(mkey)
        if hit is not None:
            return hit
        acc = {s: [0.0, 0.0] for s in systems}
        k = 0
        for s2, day in units:
            row = {}
            for s in systems:
                u = self.unit(s2, day, s, None if leads is None else leads[s], tt)
                if u is None:
                    break
                row[s] = u
            else:
                for s in systems:
                    acc[s][0] += row[s][0]
                    acc[s][1] += row[s][1]
                k += 1
        if k == 0:
            self._memo[mkey] = (None, 0)
            return None, 0
        res = ({s: (acc[s][0] / k, acc[s][1] / k) for s in systems}, k)
        self._memo[mkey] = res
        return res

    def daily_residuals(self, st, d, t, systems, leads, n=60):
        """Production-rule inputs over the rolling window: raw residual per day
        (actual - raw member mean, what OOS feeds ``_minimum_variance_weights``)
        and the residual sigma about the corrected centre."""
        tt = _z(t)
        floor = (date.fromisoformat(d) - timedelta(days=n)).isoformat()
        days = [day for day in self.station_window(st, d, tt, 0) if day >= floor]
        res = {s: {} for s in systems}
        scale = {}
        for s in systems:
            sq = []
            for day in days:
                u = self.unit(st, day, s, leads[s], tt)
                if u is None:
                    continue
                res[s][day] = u[3]
                sq.append(u[0])
            scale[s] = math.sqrt(sum(sq) / len(sq)) if sq else None
        return res, scale


# ---------------------------------------------------------------------------
# Arms.  ``ctx`` carries: st, d (event day), t (decision instant), systems,
# family {sys: family}, caps {family: cap}, leads {sys: own hour lead},
# key (deterministic seed), and optionally prod weights / oracle errors.
# Each returns (weights, info) or (None, reason) -> caller falls back.
# ---------------------------------------------------------------------------

def inv_weights(stats, systems, family, caps, metric):
    idx = 0 if metric == "mse" else 1
    raw = {s: 1.0 / max(stats[s][idx], 1e-6) for s in systems}
    return capped(raw, family, caps)


def arm_recent(S: Skill, ctx, scope, n, metric, by_lead=False):
    leads = ctx["leads"] if by_lead else None
    stats, k = S.window_stats(scope, ctx["st"], ctx["d"], ctx["t"], ctx["systems"], leads, n)
    if stats is None:
        return None, "no_support"
    return inv_weights(stats, ctx["systems"], ctx["family"], ctx["caps"], metric), k


def arm_stlead_eb(S: Skill, ctx, n, k_shrink, metric="mse"):
    """Station x own-lead cell shrunk toward the fleet x own-lead mean:
    m = (n_cell * m_cell + k * m_fleet) / (n_cell + k)."""
    systems = ctx["systems"]
    cell, nc = S.window_stats("station", ctx["st"], ctx["d"], ctx["t"], systems, ctx["leads"], n)
    prior, npr = S.window_stats("fleet", ctx["st"], ctx["d"], ctx["t"], systems, ctx["leads"], n)
    if prior is None:
        prior, npr = S.window_stats("fleet", ctx["st"], ctx["d"], ctx["t"], systems, None, n)
    idx = 0 if metric == "mse" else 1
    if prior is None and cell is None:
        return None, "no_support"
    m = {}
    for s in systems:
        if cell is None or math.isinf(k_shrink):
            m[s] = prior[s][idx] if prior is not None else None
        elif prior is None or k_shrink == 0:
            m[s] = cell[s][idx]
        else:
            m[s] = (nc * cell[s][idx] + k_shrink * prior[s][idx]) / (nc + k_shrink)
    if any(v is None for v in m.values()):
        return None, "no_support"
    raw = {s: 1.0 / max(m[s], 1e-6) for s in systems}
    return capped(raw, ctx["family"], ctx["caps"]), nc


def arm_prodrule(S: Skill, ctx, n=60, kappa=STABLE_POOL_KAPPA):
    """Production's weight rule (``_seat_weights`` then ``stable_pool_weights``)
    refitted on the same walk-forward history: one fit per station x own lead,
    all cycles pooled per day -- no per-cycle-combination artifacts."""
    systems = ctx["systems"]
    res, scale = S.daily_residuals(ctx["st"], ctx["d"], ctx["t"], systems, ctx["leads"], n)
    if any(scale[s] is None for s in systems):
        return None, "no_support"
    try:
        w, rule = _seat_weights(
            residuals_by_system=res,
            train_error_scale_by_system=scale,
            family_by_system=ctx["family"],
            family_caps={f: float(ctx["caps"].get(f, DEFAULT_CAP)) for f in set(ctx["family"].values())},
            train_n_by_system={s: len(res[s]) for s in systems},
        )
        w = {s: float(w.get(s, 0.0)) for s in systems}
        tot = sum(w.values())
        w = {s: v / tot for s, v in w.items()}
        w = stable_pool_weights(w, ctx["family"], kappa=kappa)
    except Exception as e:  # noqa: BLE001
        return None, repr(e)[:80]
    return w, rule


def blend(w, systems, kappa):
    eq = equal(systems)
    out = {s: (1 - kappa) * w[s] + kappa * eq[s] for s in systems}
    t = sum(out.values())
    return {s: v / t for s, v in out.items()}


def pool_crps_nll(probs, weights, actual):
    keys = set()
    for p in probs.values():
        keys |= set(p)
    mix = {k: sum(weights[s] * probs[s].get(k, 0.0) for s in weights) for k in keys}
    tot = sum(mix.values())
    mix = {k: v / tot for k, v in mix.items()}
    m = _metrics(mix, actual)
    return m["crps"], m["nll"], mix.get(str(math.floor(actual + 0.5)), 0.0)
