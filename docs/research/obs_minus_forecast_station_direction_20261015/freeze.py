"""Freeze the main arm (熊先生 10-15 「冻结主臂……约 10-29 复读」).

Direction model: target truth - market-mode centre; features dT, dSW,
dCloud, dRain, market tilt (market mean - mode centre); fleet ridge (lambda 1)
plus per-station ridge toward the fleet, blended by n/(n+20) station-days
(stations under 8 station-days serve the fleet coefficients).  Fitted once on
every row of rows.jsonl.gz (event dates 09-01..10-06) and written to
FROZEN_ARM.json; the forward read applies it unchanged.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

import analyze as A

HERE = Path(__file__).resolve().parent
FEATURES = ["dT", "dSW", "dCloud", "dRain", "market_tilt_c"]


def main():
    recs = A.load()
    n = len(recs)
    st = np.array([r["station"] for r in recs])
    days = np.array([r["day"] for r in recs])
    X = np.array([r["x"] for r in recs], float)
    mode_c = np.array([r["c"][r["mk"]["0"]["mode_i"]] for r in recs])
    tilt = np.array([r["mk"]["0"]["mean"] for r in recs]) - mode_c
    T = np.array([r["T"] for r in recs])
    XX = np.hstack([X, tilt[:, None]])
    mu = np.nanmean(XX, 0)
    sd = np.nanstd(XX, 0) + 1e-9
    Z = np.where(np.isnan((XX - mu) / sd), 0.0, (XX - mu) / sd)
    y = T - mode_c
    Xf = np.hstack([np.ones((n, 1)), Z])
    bf = A.ridge(Xf, y)
    stations = {}
    for s in sorted(set(st)):
        m = st == s
        nsd = len(set(days[m]))
        b = bf
        if nsd >= A.MIN_STATION_DAYS:
            bs = A.ridge(np.hstack([np.ones((m.sum(), 1)), Z[m]]), y[m], lam=1.0, prior=bf)
            w = nsd / (nsd + A.SHRINK_N)
            b = w * bs + (1 - w) * bf
        stations[s] = {"station_days": nsd, "beta": [float(v) for v in b]}
    frozen = {
        "schema": "obs_minus_forecast_mode_keep_direction_frozen_v1",
        "frozen_at": "2026-10-15",
        "ruling": "熊先生 2026-10-15「冻结主臂（增量+市场倾斜、κ=ln1.5、保众数）作兑现层 b 的前推候选臂，10-07 之后事件日只读，约 10-29 复读」",
        "trained_on_event_dates": [str(min(days)), str(max(days))],
        "training_rows": n,
        "training_rows_sha256": hashlib.sha256((HERE / "rows.jsonl.gz").read_bytes()).hexdigest(),
        "features": FEATURES,
        "feature_definitions": {
            "dT": "METAR temperature at the decision minus the Open-Meteo previous_day1 hourly temperature interpolated to the local hour (C)",
            "dSW": "(satellite surface shortwave - previous_day1 shortwave) / max(previous_day1, 200) over the last two available daylight hours; GOES official available 20 min after the hour, relays 75 min",
            "dCloud": "METAR cloud cover fraction minus previous_day1 total cloud / 100 at the local hour",
            "dRain": "1[METAR precipitation now] - 1[previous_day1 precipitation > 0.1 mm at the local hour]",
            "market_tilt_c": "market mean minus the market-mode bucket centre (C), market_reference_v1 on the newest committed WS snapshot <= 90 min old",
        },
        "standardise_mean": [float(v) for v in mu],
        "standardise_sd": [float(v) for v in sd],
        "missing_feature": "standardised value 0 (training mean)",
        "fleet_beta": [float(v) for v in bf],
        "station_beta": stations,
        "target": "truth minus market-mode bucket centre (C)",
        "direction": "sign(e_hat); 0 when |e_hat| < 0.25 x bucket width",
        "likelihood": "mode bucket probability fixed; above-mode mass A and below-mode mass B reweighted by exp(kappa*d) inside R=A+B; within-side shape kept; nothing moves when a side is empty; then the 1e-3 floor",
        "kappa": math.log(1.5),
        "applied_to": "served P1 (last committed P1 decided in each local hour 07-19), as the realisation-layer b candidate arm",
        "forward_window": {"from": "2026-10-07", "reread_on_or_after": "2026-10-29", "read_only": True},
        "verdict": "charter 240 on served P1: LogScore primary, CRPS, Brier; fleet and per station (BH q=0.10, >=10 station-days); market-tilt-only control reported beside it",
    }
    (HERE / "FROZEN_ARM.json").write_text(json.dumps(frozen, ensure_ascii=False, indent=1))
    print("fleet beta", [round(v, 3) for v in bf])
    print({s: v["station_days"] for s, v in stations.items()})


if __name__ == "__main__":
    main()
