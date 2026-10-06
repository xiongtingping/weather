#!/usr/bin/env python3
"""PROTOCOL_V2 conditions 33-35: each served decision's regime artifact (same
checkpoint as its envelope).  Read-only.  Output: regime.json, envelope id ->
[regime, model_disagreement, center_stable]."""
import glob
import gzip
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"


def main():
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    conn.execute("PRAGMA query_only=1")
    by_cp = {}
    for cp, art in conn.execute("SELECT checkpoint_id, artifact_json FROM basket_regime_artifact"):
        try:
            a = json.loads(art)
        except ValueError:
            continue
        by_cp[cp] = [a.get("regime"), a.get("model_disagreement"), a.get("center_stable")]
    out = {}
    for d in ("rows", "rows_early"):
        for f in glob.glob(os.path.join(HERE, d, "*.jsonl.gz")):
            with gzip.open(f, "rt") as fh:
                for line in fh:
                    eid = json.loads(line)["eid"]
                    if not eid or eid in out:
                        continue
                    row = conn.execute("SELECT checkpoint_id FROM basket_probability_envelope WHERE envelope_id=?", (eid,)).fetchone()
                    out[eid] = by_cp.get(row[0]) if row else None
    json.dump(out, open(os.path.join(HERE, "regime.json"), "w"))
    print(len(out), "envelopes,", sum(1 for v in out.values() if v), "with a regime artifact")


if __name__ == "__main__":
    main()
