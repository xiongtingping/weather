#!/usr/bin/env python3
"""PROTOCOL.md: tail weight for the thin far slice, scored offline on the
downstream study's stored vectors (dress_v1 single systems, current chain).

Rows: ``downstream_after_247_20261002/pass1_down`` updates with <=3 seats,
>36 h to settlement and the width layer active.  ``now`` is the live charter
239 slice (lambda 1.1), ``l10`` the slice at lambda 1.0, ``noslice`` the stage
lambda.  The tail transform is a scale mixture about the mean,
p' = (1-eps) p + eps W_kappa(p), followed by the charter 224 floor; every arm
is floored and scored with the gate's own metrics.  Writes results.json.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
import sys  # noqa: E402

sys.path.insert(0, str(ROOT))
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    MAX_WIDTH_LAMBDA, apply_center_width_calibration,
)
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

spec = importlib.util.spec_from_file_location("score_fit", ROOT / "docs/research/single_system_stretch_vs_dress_20261002/score_fit.py")
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
spec2 = importlib.util.spec_from_file_location("audit", ROOT / "docs/research/calibration_mu_sigma_audit_20261002/audit.py")
A = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(A)

SRC = ROOT / "docs/research/downstream_after_247_20261002/pass1_down"
KAPPAS = (1.5, 2.0, 3.0)
EPSILONS = (0.05, 0.10, 0.15, 0.20, 0.30)
REFS = ("now", "noslice", "l10")
SOURCE_OF = {"now": "slice_1.1", "noslice": "base", "l10": "lam_1.0"}


def widen(vec, kappa):
    """Scale the vector's deviations about its mean by kappa on the integer
    grid, splitting each moved point mass between its two neighbouring
    centres -- the width transform's rule, without its lambda bound."""
    keys = sorted(vec, key=int)
    k = np.array([int(x) for x in keys], float)
    p = np.array([float(vec[x]) for x in keys]); p /= p.sum()
    mu = float((p * k).sum())
    out = defaultdict(float)
    for center, mass in zip(k, p):
        if mass <= 0.0:
            continue
        x = mu + kappa * (center - mu)
        lo = math.floor(x); frac = x - lo
        out[int(lo)] += mass * (1.0 - frac)
        if frac > 0.0:
            out[int(lo) + 1] += mass * frac
    total = sum(out.values())
    return {str(b): v / total for b, v in sorted(out.items())}


def tail_mix(vec, kappa, eps):
    w = widen(vec, kappa)
    keys = set(vec) | set(w)
    mixed = {k: (1.0 - eps) * float(vec.get(k, 0.0)) + eps * w.get(k, 0.0) for k in keys}
    total = sum(mixed.values())
    return {k: v / total for k, v in sorted(mixed.items(), key=lambda kv: int(kv[0]))}


def served(vec):
    p, _ = floor_probabilities({k: float(v) for k, v in vec.items()})
    return p


def score(p, actual):
    m = oos._metrics(p, actual)
    return [m["crps"], m["nll"], m["brier"]]


def parity_check(vec):
    """Our widening equals the production transform where the bound allows."""
    keys = sorted(vec, key=int)
    kappa = min(1.5, MAX_WIDTH_LAMBDA)
    prod, _ = apply_center_width_calibration({k: float(vec[k]) for k in keys}, delta_c=0.0, width_lambda=kappa,
                                             ordered_keys=keys, key_centers_c=[float(int(k)) for k in keys])
    ours = widen(vec, kappa)
    inside = {k: v for k, v in ours.items() if k in prod}
    leaked = 1.0 - sum(inside.values())
    diff = max(abs(prod.get(k, 0.0) - inside.get(k, 0.0)) for k in set(prod) | set(inside))
    return diff, leaked


def main():
    rows = []
    for path in sorted(SRC.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                b = r["base"]
                if (b.get("seated") or 9) <= 3 and (b.get("hours_to_settlement") or 0.0) > 36.0 and b.get("cw_active"):
                    rows.append(r)
    print("slice rows", len(rows), "station-days", len({(r["station"], r["event_date"]) for r in rows}))
    diffs = [parity_check(rows[i]["base"]["vec"]) for i in range(0, len(rows), max(1, len(rows) // 40))]
    print("widen parity vs production transform (max |diff|, leaked mass beyond grid): %.2e %.2e" % (max(d for d, _ in diffs), max(l for _, l in diffs)))
    arms = {}
    for ref in REFS:
        arms[ref] = lambda r, ref=ref: served(r[SOURCE_OF[ref]]["vec"])
    for kappa in KAPPAS:
        for eps in EPSILONS:
            arms[f"l10_tail_k{kappa}_e{eps}"] = lambda r, k=kappa, e=eps: served(tail_mix(r["lam_1.0"]["vec"], k, e))
    for eps in EPSILONS:
        arms[f"now_tail_k2.0_e{eps}"] = lambda r, e=eps: served(tail_mix(r["slice_1.1"]["vec"], 2.0, e))
        arms[f"noslice_tail_k2.0_e{eps}"] = lambda r, e=eps: served(tail_mix(r["base"]["vec"], 2.0, e))
    scored = []
    vectors_by_arm = {name: [] for name in arms}
    for r in rows:
        actual = float(r["actual"])
        entry = {"station": r["station"], "event_date": r["event_date"], "tz": r["tz"], "at": r["at"], "actual": actual}
        for name, fn in arms.items():
            p = fn(r)
            entry[name] = {"served": score(p, actual)}
            vectors_by_arm[name].append((p, actual, r["station"], r["event_date"], r["at"]))
        scored.append(entry)
    early = [e for e in scored if e["event_date"] <= "2026-09-13"]
    recent = [e for e in scored if e["event_date"] >= "2026-09-14"]
    copies = Counter((e["station"], e["event_date"]) for e in early)
    w = np.array([1.0 / copies[(e["station"], e["event_date"])] for e in early])
    candidates = [n for n in arms if n != "now"]
    selection = {n: float(np.average([e[n]["served"][1] for e in early], weights=w)) for n in candidates}
    selection["now"] = float(np.average([e["now"]["served"][1] for e in early], weights=w))
    chosen = min(candidates, key=selection.get)
    res = {"protocol": "PROTOCOL.md", "slice_rows": len(scored), "early_rows": len(early), "recent_rows": len(recent),
           "selection_logscore_early": dict(sorted(selection.items(), key=lambda kv: kv[1])), "chosen": chosen,
           "judge_recent_vs_now": S.compare(recent, chosen, "now"), "all_vs_now": S.compare(scored, chosen, "now"),
           "chosen_vs_noslice_recent": S.compare(recent, chosen, "noslice"), "chosen_vs_noslice_all": S.compare(scored, chosen, "noslice"),
           "l10_vs_now": {"recent": S.compare(recent, "l10", "now"), "all": S.compare(scored, "l10", "now")},
           "noslice_vs_now": {"recent": S.compare(recent, "noslice", "now"), "all": S.compare(scored, "noslice", "now")},
           "grid_vs_now_all": {n: S.compare(scored, n, "now")["d_nll"] for n in candidates},
           "grid_vs_now_recent": {n: S.compare(recent, n, "now")["d_nll"] for n in candidates}}

    def shape(name):
        recs = []
        for p, actual, st, day, at in vectors_by_arm[name]:
            mu, sd, u, pt, idx = A.read_vec(p, actual)
            recs.append(dict(station=st, date=day, at=at, truth=actual, mu=mu, sd=sd, u=u, p_truth=pt, ls=-math.log(max(pt, 1e-3))))
        rep = A.report(recs, name)
        return {k: rep[k] for k in ("updates", "station_days", "bias_c", "z_meansq", "pit_var_ratio", "cover_50", "cover_90", "pit_tail_lt05_gt95")}
    res["shape"] = {n: shape(n) for n in ("now", "noslice", "l10", chosen)}
    (HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("selection (early, mean LogScore):", {k: round(v, 4) for k, v in list(res["selection_logscore_early"].items())[:8]}, "... now", round(selection["now"], 4))
    print("chosen:", chosen)
    for label, c in (("judge recent vs now", res["judge_recent_vs_now"]), ("all vs now", res["all_vs_now"]),
                     ("chosen vs noslice recent", res["chosen_vs_noslice_recent"]), ("chosen vs noslice all", res["chosen_vs_noslice_all"]),
                     ("l10 vs now recent", res["l10_vs_now"]["recent"]), ("l10 vs now all", res["l10_vs_now"]["all"]),
                     ("noslice vs now recent", res["noslice_vs_now"]["recent"]), ("noslice vs now all", res["noslice_vs_now"]["all"])):
        print(f"  {label:26s} sd={c['station_days']:5d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}")
    for n, s in res["shape"].items():
        print(f"  shape {n:22s} bias {s['bias_c'][0]:+.3f}  E[z²] {s['z_meansq'][0]:.3f} [{s['z_meansq'][1]:.3f},{s['z_meansq'][2]:.3f}]  PITvar {s['pit_var_ratio'][0]:.3f}  cov50 {s['cover_50'][0]:.3f} cov90 {s['cover_90'][0]:.3f}  tails {s['pit_tail_lt05_gt95'][0]:.3f}/{s['pit_tail_lt05_gt95'][1]:.3f}")


if __name__ == "__main__":
    main()
