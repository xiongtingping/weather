#!/usr/bin/env python3
"""Post-hoc, report only: what lambda does to the three calibration readings, where rebuilding is exact.

Both pre-registered tests are scores only because the fidelity gate failed:
from 08-15 the replay's served chain carried the band table, and after
09-03 the served vector is already contracted.  Before either, the served
vector is the pre-table vector itself, so the width layer's relocation
applied to it IS the lambda arm.  This script keeps exactly those rows --
every grid lambda rebuilt within 1e-4 of the exact LogScore and 1e-5 of the
exact Brier -- and reads PIT, reliability and the two band gaps at lambda
0.75 / 0.90 / 1.00.  It is an earlier, thinner-pool regime than the test
period, chosen after the tests were read; it explains, it does not decide.

    python3 early_window_calibration.py   -> results_early.json and a printed summary
"""

from __future__ import annotations

import json

import numpy as np

import far_lead as F

ARMS = ("0.75", "0.90", "1.00")


def main():
    rows = F.load()
    n = len(rows)
    exact = np.array([r["exact"] for r in rows])
    P, hits, recon = F.rebuild_all(rows)
    ident = np.array([r["served_lambda"] is None for r in rows])
    exact_rows = ident & (np.abs(recon[:, :, 1] - exact[:, :, 1]).max(axis=1) <= 1e-4) \
        & (np.abs(recon[:, :, 0] - exact[:, :, 0]).max(axis=1) <= 1e-5)
    keep = np.nonzero(exact_rows)[0]
    days = sorted({rows[i]["day"] for i in keep})
    far = np.array([r["h"] > F.FAR_H for r in rows])
    first = np.array([bool(r.get("first")) for r in rows])
    slices = {
        "first_decision_far": np.nonzero(exact_rows & far & first)[0],
        "far_all": np.nonzero(exact_rows & far)[0],
        "all_updates": keep,
    }
    result = {"exact_rows": int(len(keep)), "of_rows": n, "event_days": [days[0], days[-1], len(days)],
              "slices": {}}
    print(f"exact rows {len(keep)} of {n}; event days {days[0]}..{days[-1]} ({len(days)})")
    base = F.GRID.index("0.75")
    for name, idx in slices.items():
        out = {}
        for arm in ARMS[1:]:
            g = F.GRID.index(arm)
            scores = {k: F.mean_diff(rows, idx, exact[idx, base, s], exact[idx, g, s])
                      for s, k in ((0, "brier"), (1, "logscore"))}
            cal = F.calibration(rows, idx, P[idx, base], P[idx, g], hits)
            out[arm] = {"scores": scores, "calibration": cal}
        result["slices"][name] = out
        sd = out[ARMS[1]]["calibration"]["station_days"]
        print(f"== {name}: {len(idx)} updates, {sd} station-days")
        for arm in ARMS[1:]:
            sc, cal = out[arm]["scores"], out[arm]["calibration"]
            print(f"  0.75 -> {arm}: Brier {sc['brier']['delta']:+.5f} [{sc['brier']['ci95'][0]:+.5f},{sc['brier']['ci95'][1]:+.5f}]"
                  f"  LogScore {sc['logscore']['delta']:+.5f} [{sc['logscore']['ci95'][0]:+.5f},{sc['logscore']['ci95'][1]:+.5f}]")
            for k in F.SCALARS:
                b, t, ci = cal["base"][k], cal["test"][k], cal["ci95"][k]
                print(f"     {k:12s} {b:+.4f} [{ci['base'][0]:+.4f},{ci['base'][1]:+.4f}] -> {t:+.4f} "
                      f"[{ci['test'][0]:+.4f},{ci['test'][1]:+.4f}]  delta [{ci['delta'][0]:+.4f},{ci['delta'][1]:+.4f}]")
            print(f"     PIT 0.75 {cal['base']['pit_hist']}\n     PIT {arm} {cal['test']['pit_hist']}")
    (F.HERE / "results_early.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
