#!/usr/bin/env python3
"""Score the PROTOCOL.md addendum: constrained arms vs charter 216 under charter 240.  Writes results_constrained.json."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import score_240 as S  # noqa: E402


def load():
    rows = []
    for path in sorted((HERE / "pass1_cons").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def main():
    rows = load()
    seg = {"all": rows,
           "judge_0914_0930": [r for r in rows if r["event_date"] >= "2026-09-14"],
           "new_since_216_0924_0930": [r for r in rows if r["event_date"] >= "2026-09-24"]}
    res = {"updates": len(rows), "stations": len({r["station"] for r in rows}), "comparisons": {}}
    for arm, base in (("c000", "k050"), ("c050", "k050"), ("c000", "k100"), ("c050", "k100"), ("k050", "k100")):
        res["comparisons"][f"{arm}_vs_{base}"] = {k: S.compare(v, arm, base) for k, v in seg.items()}
    fits = [f for r in rows for f in r["fit"].values()]
    res["binding_share"] = {"crps": sum(f["crps_binding"] for f in fits) / len(fits),
                            "brier": sum(f["brier_binding"] for f in fits) / len(fits),
                            "either": sum(1 for f in fits if f["crps_binding"] or f["brier_binding"]) / len(fits),
                            "fit_rows": len(fits)}
    adopt = {a: bool(res["comparisons"][f"{a}_vs_k050"]["all"]["verdict_240"] and res["comparisons"][f"{a}_vs_k050"]["judge_0914_0930"]["verdict_240"]) for a in ("c000", "c050")}
    res["adopt"] = adopt
    (HERE / "results_constrained.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("updates", res["updates"], "stations", res["stations"], "binding", {k: round(v, 3) if isinstance(v, float) else v for k, v in res["binding_share"].items()})
    for name, block in res["comparisons"].items():
        print("==", name)
        for k, c in block.items():
            print(f"  {k:26s} sd={c['station_days']:5d} d={c['event_days']:2d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}")
    print("adopt", adopt)


if __name__ == "__main__":
    main()
