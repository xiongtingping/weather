"""Charter 255 (五) re-read of the charter 254 acceptance read, split by the
P0 posterior version each station-day's decision was served under.

Calls the locked reader's own load/score/summarise unchanged
(docs/research/realisation_acceptance_20261006/read.py); only the grouping by
version is added.  Read-only on the SSOT.  Run from the repo root.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "acceptance_read", HERE / "read.py")
read = importlib.util.module_from_spec(spec)
spec.loader.exec_module(read)


def main() -> int:
    theta = json.loads((read.HERE / "frozen_fit.json").read_text())["theta"]
    conn = sqlite3.connect(f"file:{read.DB}?mode=ro", uri=True, timeout=60.0)
    try:
        rows, counts = read.load(conn, theta)
        version = {}
        for r in rows:
            got = conn.execute(
                "SELECT e.posterior_version FROM basket_policy_training_fact f"
                " JOIN basket_probability_envelope e ON e.envelope_id=f.probability_envelope_id"
                " WHERE f.stage='P0' AND f.station_id=? AND f.event_date=? AND f.decision_at_utc=? LIMIT 1",
                (r["station_id"], r["event_date"], r["decided"])).fetchone()
            version[(r["station_id"], r["event_date"])] = got[0] if got else None
    finally:
        conn.close()
    groups: dict = defaultdict(list)
    for r in rows:
        groups[version[(r["station_id"], r["event_date"])]].append(r)
    out = {"counts": counts, "segments": {}}
    for v in sorted(groups, key=str):
        seg = groups[v]
        scores = [read.score(r) for r in seg]
        out["segments"][str(v)] = read.summarise(seg, scores, np.random.default_rng(read.SEED))
    (HERE / "reading_by_version_20261020.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for v, s in out["segments"].items():
        a, d = s["all"], s["modes_disagree"]
        print(v, "days", len(s["event_days"]), s["event_days"][0], "..", s["event_days"][-1], "sd", s["station_days"],
              "| b-p %.3f b-q %.3f | disagree sd %d b-q %s b_over %.4f" % (
                  a["dNLL_b_minus_p"], a["dNLL_b_minus_q"], d["station_days"],
                  None if d["dNLL_b_minus_q"] is None else round(d["dNLL_b_minus_q"], 3),
                  d["four_cell_b_own_legs"]["overclaim"]),
              "| ci", json.dumps(s.get("block_ci95", {}).get("modes_disagree", {}).get("b_overclaim_four")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
