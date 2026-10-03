#!/usr/bin/env python3
"""PROTOCOL.md: every vector the three re-judgements need, scored both ways.

    python3 score.py CYYZ,EDDM ...   -> rows/<station>.jsonl.gz

Per decision (caches A and B, same order), the served P0 of the original
downstream -- consensus bridge, ``_holdout_shape_stack``, today's P0 width
layer with lambda replaced -- scored [nll, brier, crps] twice: ``u`` the
replay's own ``_score`` (no floor, as the original studies), ``f`` after
``materialization.floor_probabilities`` (charter 224, the served vector).

    o   : bridge 1.5/2.0, lambda 0.75 -- A0a, EBPa (cache A), A0b, EBPb (cache B)
    cur : bridge 1.0/1.5, lambda 0.75 -- A0b, EBPb
    br  : EBPb weights, bridges CUR/EARLY/OFF/STRONG x lambda 0.60..1.30
"""
import dataclasses
import gzip
import json
import os
import pickle
import sqlite3
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import replay_p0_stable_pool as replay  # noqa: E402
from bayes_basket_target import cross_consensus as CC  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.cross_consensus import bridged_cross_probability  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402

BRIDGES = {"CUR": (1.5, 2.0, 0.4), "EARLY": (1.0, 1.5, 0.4), "OFF": (1.5, 2.0, 0.0), "STRONG": (1.5, 2.0, 0.6)}
LAMS = tuple(round(0.6 + 0.05 * i, 2) for i in range(15))


def scores(v, actual):
    crps, nll, brier, _b, _a = replay._score(v, actual)
    truth = oos._temperature_bucket(actual)
    vf, _ = floor_probabilities({**({truth: 0.0} if truth not in v else {}), **v})
    m = oos._metrics(vf, actual)
    return [[round(nll, 6), round(brier, 6), round(crps, 6)],
            [round(m["nll"], 6), round(m["brier"], 6), round(m["crps"], 6)]]


def served(d, w, bridge, lams, layer):
    w = {s: v for s, v in w.items() if v > 1e-12}
    grid = replay._grid([d["p"][s] for s in w])
    kc = [float(int(k)) for k in grid]
    probs = {s: {k: float(d["p"][s].get(k, 0.0)) for k in grid} for s in w}
    CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX = BRIDGES[bridge]
    if len(w) > 1:
        blended = bridged_cross_probability(system_probabilities=probs, system_weights=w,
                                            system_centers_c={s: d["c"][s] for s in w}, ordered_keys=grid, key_centers_c=kc)[0]
    else:
        only = probs[next(iter(w))]
        total = sum(only.values())
        blended = {k: v / total for k, v in only.items()}
    pre = oos._holdout_shape_stack(blended, grid=grid, cooling_shift_sigma=d["cooling"], station_latitude=d["lat"],
                                   neighbour_center_c=sum(w[s] * d["c"][s] for s in w), center_width_layer=None)
    out = {}
    for lam in lams:
        v, _ = apply_served_table_layers(pre, ordered_keys=grid, key_centers_c=kc,
                                         center_width=dataclasses.replace(layer, width_lambda=lam))
        out[str(lam)] = scores(v, d["actual"])
    return out


def main(stations):
    with open(os.path.join(HERE, "cache_A.pkl"), "rb") as h:
        A = pickle.load(h)
    with open(os.path.join(HERE, "cache_B.pkl"), "rb") as h:
        B = pickle.load(h)
    assert len(A) == len(B) and all((a["station"], a["day"], a["bin"]) == (b["station"], b["day"], b["bin"]) for a, b in zip(A, B))
    saved = (CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX)
    assert saved == BRIDGES["EARLY"], saved
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
    os.makedirs(os.path.join(HERE, "rows"), exist_ok=True)
    idx = defaultdict(list)
    for i, a in enumerate(A):
        idx[a["station"]].append(i)
    try:
        for st in stations:
            t0 = time.monotonic()
            layer = oos.p0_gate_tables(conn, station_id=st)["center_width_layer"]
            assert abs(layer.width_lambda - 0.75) < 1e-12 and abs(layer.delta_c) < 1e-12
            with gzip.open(os.path.join(HERE, "rows", f"{st}.jsonl.gz"), "wt") as z:
                for i in idx[st]:
                    a, b = A[i], B[i]
                    wb = {s: v for s, v in b["EBP"].items() if v > 1e-12}
                    row = {"station": st, "day": a["day"], "known": a["known"], "bin": a["bin"],
                           "thin": len({b["family"][s] for s in wb}) <= 3 and a["bin"] >= 36,
                           "o": {"A0a": served(a, a["A0"], "CUR", (0.75,), layer)["0.75"],
                                 "EBPa": served(a, a["EBP"], "CUR", (0.75,), layer)["0.75"],
                                 "A0b": served(b, b["A0"], "CUR", (0.75,), layer)["0.75"]},
                           "cur": {"A0b": served(b, b["A0"], "EARLY", (0.75,), layer)["0.75"]},
                           "br": {br: served(b, b["EBP"], br, LAMS, layer) for br in BRIDGES}}
                    row["o"]["EBPb"] = row["br"]["CUR"]["0.75"]
                    row["cur"]["EBPb"] = row["br"]["EARLY"]["0.75"]
                    z.write(json.dumps(row) + "\n")
            print(st, len(idx[st]), "%.0fs" % (time.monotonic() - t0), flush=True)
    finally:
        CC.CONSENSUS_BRIDGE_GAP_START_C, CC.CONSENSUS_BRIDGE_GAP_FULL_C, CC.CONSENSUS_BRIDGE_LAMBDA_MAX = saved
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1].split(","))
