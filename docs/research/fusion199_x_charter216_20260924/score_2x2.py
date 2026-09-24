#!/usr/bin/env python3
"""Pass 2: the four cells -- {moment fit, charter 216 fit} x {seat, charter 199 blend}.

For each single-system fit the fusion training table is built from that
fit's own day-ahead roots (what production's table would hold once that fit
serves), and the blend weights come from ``fusion_blend.overlay_system_
weights`` itself.  Every comparison is paired on the same station-day roots
and judged by charter 211 as amended: bucket Brier and LogScore both lower.
Writes results.json.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
spec = importlib.util.spec_from_file_location("score_fusion", ROOT / "docs/research/fusion_blend_historical_20260923/score.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

PASS1 = HERE / "pass1"


def arm_records(records, arm):
    out = []
    for r in records:
        a = r["arms"][arm]
        out.append({**{k: v for k, v in r.items() if k != "arms"},
                    "day_rows": a["day_rows"], "cooling": a["cooling"], "seat": dict(a["seat"])})
    return out


def main() -> int:
    records = []
    for path in sorted(PASS1.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            records.extend(json.loads(line) for line in handle)
    records.sort(key=lambda r: (r["station"], r["at"]))
    S.PASS1 = PASS1  # the tables pickled beside these roots
    cells = {}
    states = {}
    for arm in ("prod", "reg"):
        rows = arm_records(records, arm)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / f"training_{arm}.db"
            written = S.training_db(rows, db)
            state, _moved = S.blend_arm(rows, db)
        states[arm] = {"training_rows": written, "states": dict(state)}
        cells[f"{arm}_seat"] = [r["seat"] for r in rows]
        cells[f"{arm}_blend"] = [r["blend"] for r in rows]
        print(arm, states[arm], flush=True)

    def pair(a, b, label):
        paired = [{**{k: v for k, v in r.items() if k != "arms"}, "seat": cells[a][i], "blend": cells[b][i]}
                  for i, r in enumerate(records)]
        c = S.compare(paired, label)
        c["from"], c["to"] = a, b
        return c

    comparisons = [
        pair("prod_seat", "prod_blend", "现役单系统：席位 → 199"),
        pair("reg_seat", "reg_blend", "216 单系统：席位 → 199（199 在 216 之上还有没有用）"),
        pair("prod_seat", "reg_seat", "席位权重：现役单系统 → 216（216 在六家上成不成立）"),
        pair("prod_blend", "reg_blend", "199 融合：现役单系统 → 216"),
        pair("prod_seat", "reg_blend", "现役生产 → 216 + 199 一起上"),
    ]
    result = {"station_days": len({(r["station"], r["event_date"]) for r in records}),
              "event_days": len({r["event_date"] for r in records}),
              "blend_states": states, "comparisons": comparisons}
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    for c in comparisons:
        s, b, ci = c["seat"], c["blend"], c["ci95"]
        print(f"{c['label']}: Brier {s['brier']:.5f}->{b['brier']:.5f} ({b['brier']-s['brier']:+.5f}, ci {[round(x, 5) for x in ci['brier']]}) "
              f"LogScore {s['nll']:.4f}->{b['nll']:.4f} ({b['nll']-s['nll']:+.4f}, ci {[round(x, 4) for x in ci['nll']]}) "
              f"REL {s['rel']*1e3:.3f}->{b['rel']*1e3:.3f} RES {s['res']*1e3:.2f}->{b['res']*1e3:.2f} 211={c['passes_charter_211']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
