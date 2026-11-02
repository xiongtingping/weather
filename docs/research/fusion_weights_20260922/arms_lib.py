"""All weight arms for one decision, with rolling (as-of) parameter choice.

Parameter choice (EB shrink k, shrink-to-equal kappa) reads Lens B cells only:
event day in [d-21, d), truth committed by the decision instant.
"""
import math, pickle, sys
from collections import defaultdict
from datetime import date, timedelta
sys.path.insert(0, "/Users/xiong/weather/docs/research/fusion_weights_20260922")
import weights_lib as W

LENSB = "/private/tmp/claude-501/-Users-xiong-weather/d9b59a13-2b1c-4640-af8a-d1a502acff1e/scratchpad/fw/lensb.pkl"
K_GRID = (0.0, 2.0, 5.0, 10.0, 20.0, 50.0, math.inf)
KAPPA_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
SEL_DAYS = 21
ARMS_A = ["prod", "equal", "perm", "prodrule60",
          "fleet_mse_3", "fleet_mse_7", "fleet_mse_14", "fleet_mse_30", "fleet_crps_7",
          "st_mse_3", "st_mse_5", "st_mse_7", "st_mse_14", "st_mse_30", "st_crps_14",
          "stlead_eb7", "stlead_eb14", "st_mse7_shr", "stlead_eb14_shr",
          "skillperm", "oracle"]


class Selector:
    def __init__(self, first_commit, path=LENSB):
        D = pickle.load(open(path, "rb"))
        opts = ([f"stlead_eb{n}_k{k}" for n in (7, 14) for k in K_GRID]
                + [f"st_mse7_shr{k}" for k in KAPPA_GRID]
                + [f"stlead_eb14_shr{k}" for k in KAPPA_GRID])
        self.by_day = defaultdict(list)
        for c in D["cells"]:
            self.by_day[c["d"]].append((c["st"], {o: c["scores"][o][0] for o in opts}))
        self.first_commit = first_commit
        self.memo = {}

    def _prefix(self):
        # per day: rows sorted by first truth commit, with prefix sums per option
        import bisect  # noqa: F401
        self.pref = {}
        for day, rows in self.by_day.items():
            keyed = sorted(((self.first_commit.get((st, day), "9"), sc) for st, sc in rows), key=lambda x: x[0])
            commits = [c for c, _ in keyed]
            sums = {}
            for o in keyed[0][1] if keyed else ():
                acc, run = [0.0], 0.0
                for _, sc in keyed:
                    run += sc[o]
                    acc.append(run)
                sums[o] = acc
            self.pref[day] = (commits, sums)

    def select(self, options, d, t):
        """Option with the lowest mean Lens B CRPS over cells with event day in
        [d-21, d) whose truth was committed by t (ties -> later option)."""
        import bisect
        if not hasattr(self, "pref"):
            self._prefix()
        lo = (date.fromisoformat(d) - timedelta(days=SEL_DAYS)).isoformat()
        tot = {a: 0.0 for a in options}
        n = 0
        for day, (commits, sums) in self.pref.items():
            if day < lo or day >= d:
                continue
            j = bisect.bisect_right(commits, t)
            if j == 0:
                continue
            for a in options:
                tot[a] += sums[a][j]
            n += j
        if n == 0:
            return options[-1]
        return min(options, key=lambda a: (round(tot[a] / n, 10), -options.index(a)))


def all_arms(S: W.Skill, sel: Selector, ctx, prod=None, centres=None, truth=None):
    """ctx: st, d, t (decision, raw string ok), systems, family, caps, leads, key."""
    ctx = dict(ctx)
    ctx["t"] = W._z(ctx["t"])
    systems = ctx["systems"]
    eq = W.equal(systems)
    arms, fb = {"equal": eq}, {}
    if prod is not None:
        arms["prod"] = dict(prod)
        arms["perm"] = W.rotate(prod, ctx["key"])
    stats = {}
    for n in (3, 5, 7, 14, 30):
        for scope, tag in (("fleet", "fleet"), ("station", "st")):
            st_, k = S.window_stats(scope, ctx["st"], ctx["d"], ctx["t"], systems, None, n)
            stats[(tag, n)] = st_
            for metric in ("mse", "crps"):
                name = f"{tag}_{metric}_{n}"
                if st_ is None:
                    arms[name] = eq; fb[name] = True
                else:
                    arms[name] = W.inv_weights(st_, systems, ctx["family"], ctx["caps"], metric)
    for n in (7, 14):
        kname = sel.select([f"stlead_eb{n}_k{k}" for k in K_GRID], ctx["d"], ctx["t"])
        kk = float(kname.split("_k")[1])
        w, info = W.arm_stlead_eb(S, ctx, n, kk)
        if w is None:
            arms[f"stlead_eb{n}"] = eq; fb[f"stlead_eb{n}"] = True
        else:
            arms[f"stlead_eb{n}"] = w
        arms[f"_k_eb{n}"] = kk
    kap = float(sel.select([f"st_mse7_shr{k}" for k in KAPPA_GRID], ctx["d"], ctx["t"]).split("shr")[1])
    arms["st_mse7_shr"] = W.blend(arms["st_mse_7"], systems, kap)
    kap2 = float(sel.select([f"stlead_eb14_shr{k}" for k in KAPPA_GRID], ctx["d"], ctx["t"]).split("shr")[1])
    arms["stlead_eb14_shr"] = W.blend(arms["stlead_eb14"], systems, kap2)
    arms["_kappa"] = (kap, kap2)
    w, info = W.arm_prodrule(S, ctx)
    if w is None:
        arms["prodrule60"] = eq; fb["prodrule60"] = True
    else:
        arms["prodrule60"] = w
    arms["skillperm"] = W.rotate(arms["st_mse_7"], ctx["key"])
    if centres is not None and truth is not None:
        ose = {s: (centres[s] - truth) ** 2 for s in systems}
        arms["oracle"] = W.capped({s: 1.0 / max(ose[s], 0.01) for s in systems}, ctx["family"], ctx["caps"])
    return arms, fb
