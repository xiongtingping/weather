"""Out-of-sample REL / RES of each belief, through the board's own decomposition.

Reads the per-row outputs ``fit_attribute.py`` saved and scores them with
``scripts/calibration_dashboard._core_murphy`` -- the north star's function,
not a copy of it.  Writes ``murphy.json``.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from calibration_dashboard import _core_murphy  # noqa: E402

MODELS = ("raw_market", "raw_model", "M0", "M1", "full")


def vectors(z, name, i):
    k = int(z["mask"][i].sum())
    if name == "raw_market":
        v = z["m"][i, :k]
    elif name == "raw_model":
        v = z["p"][i, :k]
    else:
        v = z[f"prob_{name}"][i, :k]
    return list(np.asarray(v, float) / float(np.sum(v)))


def decompose(z, rows, name):
    # A station-day counts once however many blocks it was scored on.
    copies = Counter(z["sd"][i] for i in rows)
    cells = [
        (1.0 / copies[z["sd"][i]], {"probabilities": vectors(z, name, i), "hit": int(z["hit"][i])})
        for i in rows
    ]
    out = _core_murphy(cells)
    return {
        "rel": out["reliability"], "res": out["resolution"], "unc": out["uncertainty"],
        "brier": out["mean_bucket_brier"], "diagram": out["diagram"],
    }


def main():
    result = {}
    for suffix in ("", "_robust"):
        variant = "robust" if suffix else "preregistered"
        result[variant] = {}
        for stage in ("P0", "P0-M", "P1"):
            with np.load(HERE / f"rows_{stage}{suffix}.npz", allow_pickle=False) as raw:
                z = {key: raw[key] for key in raw.files}
            names = list(MODELS) + [k[len("prob_"):] for k in z if k.startswith("prob_+")]
            result[variant][stage] = {}
            for label, subset in (("all", z["scored"]), ("evidence_epoch", z["scored"] & z["epoch"])):
                rows = np.flatnonzero(subset)
                result[variant][stage][label] = {name: decompose(z, rows, name) for name in names}
    (HERE / "murphy.json").write_text(json.dumps(result, indent=1))
    for variant, stages in result.items():
        for stage, slices in stages.items():
            for label, models in slices.items():
                print(variant, stage, label, {n: (round(v["rel"] * 1e3, 3), round(v["res"] * 1e3, 3)) for n, v in models.items()})


if __name__ == "__main__":
    main()
