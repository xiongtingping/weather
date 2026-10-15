"""Export the D0 observation model for the station board's read-only card
(user 2026-10-15 「不要结案，作为只读，在前端"站点看板"增加只读卡片」).

Fits fit_d0's ridge + spread per time band on every closed day in
rows_d0.jsonl.gz and writes config/ui/d0_obs_model_v1.json: the feature
columns, scaling, coefficients, station effects, spread model, and the
walk-forward readings (MAE of the market, served P1 and D0 per band) the
card shows beside its number.  Display only.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import fit_d0 as F  # noqa: E402

import os as _os
# Automation writes the live model to the runtime path the board reads first
# (station_day_board.D0_MODEL_PATHS); the tracked config file is the seed.
OUT = Path(_os.environ.get("D0_MODEL_OUT") or ROOT / "config" / "ui" / "d0_obs_model_v1.json")


def main() -> None:
    data = F.load()
    stations = sorted({r["station"] for r in data})
    wf = json.loads((F.DATA / "walk_forward_d0.json").read_text())
    bands = {}
    for band, (h0, h1) in F.BANDS.items():
        rows = [r for r in data if h0 <= r["hour"] < h1]
        m = F.fit_band(rows, stations, True)
        get = lambda arm, metric: (wf.get(f"{arm}|{band}|{metric}") or {}).get("point")
        bands[band] = {
            "hours": [h0, h1], "n": len(rows),
            "mu": m["mu"].tolist(), "sd": m["sd"].tolist(), "nf": m["nf"], "beta": m["beta"].tolist(),
            "gamma": m["gamma"].tolist(), "smu": m["smu"].tolist(), "ssd": m["ssd"].tolist(), "scale": m["scale"],
            "walk_forward_mae": {"market": get("market", "mae"), "p1": get("p1", "mae"), "d0": get("full", "mae")},
            "walk_forward_dlog_vs_market": get("full", "dlog_mkt"),
        }
    doc = {
        "schema": "d0_obs_model_v1", "fitted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [min(r["day"] for r in data), max(r["day"] for r in data)],
        "numeric": F.NUMERIC, "satellite": sorted(F.SAT), "stations": stations,
        "missing_indicator_after": sorted(F.SAT | {"dew_dep"}),
        "spread_features": ["hours_to_peak", "hours_to_peak_pos", "fc_rise", "high_minus_base", "abs_t_minus_fc"],
        "baseline": "median of official deterministic Tmax (ECMWF_IFS_HRES_NATIVE_POINT, NOAA_GFS_025_NATIVE_POINT, "
                    "ECMWF_AIFS_SINGLE_POINT) knowable by local midnight",
        "lineage": "docs/research/d0_obs_model_20261015 (research; display only, enters no layer)",
        "bands": bands,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    _os.replace(temporary, OUT)
    print("wrote", OUT, {b: v["n"] for b, v in bands.items()})


if __name__ == "__main__":
    main()
