#!/usr/bin/env python3
"""PROTOCOL.md criterion 3: the ASOS lift under charter 216's single-system fit.

Day-ahead roots from fusion199_x_charter216_20260924/pass1, its 'prod' arm
(which ran on 216: the code was default-on in the tree when it was captured).
Same two arms as score_asos.py.  Writes results_216.json.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("score_asos", HERE / "score_asos.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
S = A.S
PASS1 = ROOT / "docs/research/fusion199_x_charter216_20260924/pass1"


def main() -> int:
    import gzip
    records = []
    for path in sorted(PASS1.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                arm = r.pop("arms")["prod"]
                records.append({**r, "day_rows": arm["day_rows"], "cooling": arm["cooling"], "seat": dict(arm["seat"])})
    records.sort(key=lambda r: (r["station"], r["at"]))
    S.PASS1 = PASS1  # the tables pickled beside these roots
    arms = {}
    for name, excluded in (("current", A.ASOS), ("lifted", frozenset())):
        arms[name] = A.run_arm(records, excluded)
        for r in records:
            r[name] = r["blend"]
            r[name + "_state"] = r["blend_state"]
        print(name, arms[name], flush=True)
    asos = [r for r in records if r["station"] in A.ASOS]
    result = {
        "protocol": "PROTOCOL.md criterion 3", "pass1": str(PASS1.relative_to(ROOT)), "arms": arms,
        "asos_roots": A.pair(asos, "current", "lifted", "216 拟合，ASOS 日前根：现役 → 放开（判据 3，整度桶）"),
        "asos_roots_f_ladder": A.compare_f(asos, "current", "lifted", "216 拟合，ASOS 日前根，2°F 梯子（判据 3）"),
        "fleet_roots": A.pair(records, "current", "lifted", "216 拟合，全队日前根（只报）"),
        "non_asos_roots": A.pair([r for r in records if r["station"] not in A.ASOS], "current", "lifted", "216 拟合，非 ASOS 日前根（只报）"),
    }
    result["criterion_3"] = result["asos_roots"]["passes_charter_211"] and result["asos_roots_f_ladder"]["passes_charter_211"]
    (HERE / "results_216.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for key, c in result.items():
        if isinstance(c, dict) and "delta" in c:
            ci = c["ci95"]
            print(f"{c['label']}: {c['station_days']} sd | Brier {c['delta']['brier']:+.5f} {[round(x, 5) for x in ci['brier']]}"
                  f" LogScore {c['delta']['nll']:+.4f} {[round(x, 4) for x in ci['nll']]} | 211={c['passes_charter_211']}", flush=True)
    print("criterion 3 =", result["criterion_3"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
