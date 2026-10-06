"""Isolated acceptance read for the realisation layer (charter 254 shadow).

Locked by PROTOCOL.md before any post-freeze day settled.  Read-only on the
SSOT; writes ``reading_<UTC>.json`` next to itself.  Run from the repo root:

    python3 docs/research/realisation_acceptance_20261006/read.py
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import realisation_layer as layer  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402
from bayes_basket_target.realisation_store import _ladder_inputs  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement_if_known  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FREEZE_UTC = "2026-10-05T13:46:56Z"
FROZEN_FIT_SHA256 = "76ff1003b99350d1fac42ed8677c38a80a151765b1fa5c6b8b1ef6a9d7e88dae"
MIN_HOURS = 36.0
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
READY_DAYS, READY_STATION_DAYS = 10, 150
BLOCK, DRAWS, SEED = 3, 4000, 20261006
FOUR = ("NO_mode", "NO_adjacent", "YES_adjacent", "YES_far")


def _logit(x: float) -> float:
    x = min(max(x, 1e-6), 1 - 1e-6)
    return math.log(x / (1 - x))


def _quotes(buckets: list[dict], order: list[str]) -> tuple[list, list]:
    rows = {str(r.get("bucket")): r for r in buckets}
    ask, bid = [], []
    for b in order:
        r = rows[b]
        a = min((float(l["price"]) for l in r.get("ask_levels") or []), default=None)
        ask.append(a if r.get("ask_available") else None)
        bid.append(float(r["bid_price"]) if r.get("bid_available") and r.get("bid_price") is not None else None)
    return ask, bid


def _legs(belief: np.ndarray, ask: list, bid: list, mode: int) -> list[dict]:
    """Paper binary-Log legs off one vector: YES at the ask when belief>ask,
    NO at the YES bid when belief<bid; s in YES units (NO negative)."""

    out = []
    for k, v in enumerate(belief):
        d = abs(k - mode)
        if ask[k] is not None and 0 < ask[k] < 1 and v > ask[k]:
            side, price = "YES", ask[k]
        elif bid[k] is not None and 0 < bid[k] < 1 and v < bid[k]:
            side, price = "NO", bid[k]
        else:
            continue
        cell = f"{side}_" + ("mode" if d == 0 else "adjacent" if d == 1 else "far")
        out.append({"k": k, "cell": cell, "price": price, "s": _logit(float(v)) - _logit(price)})
    return out


def load(conn: sqlite3.Connection, theta: list[float]) -> tuple[list[dict], dict]:
    labels = {k: v for k, v in knowable_labels(conn).items()}
    counts = {"facts": 0, "before_freeze": 0, "no_label": 0, "inside_36h": 0, "not_first": 0,
              "missing": 0, "unusable": 0, "label_off_ladder": 0}
    facts = conn.execute(
        "SELECT station_id, event_date, decision_at_utc, probability_envelope_id, ws_snapshot_id,"
        " settlement_timezone FROM basket_policy_training_fact"
        " WHERE stage='P0' AND decision_at_utc > ? ORDER BY decision_at_utc", (FREEZE_UTC,),
    ).fetchall()
    seen: set = set()
    rows = []
    for station, day, decided, env_id, snap_id, tz in facts:
        counts["facts"] += 1
        key = (str(station), str(day))
        if key in seen:
            counts["not_first"] += 1
            continue
        hours = hours_to_settlement_if_known(str(day), as_of_utc=str(decided), settlement_timezone=tz)
        if hours is None or not hours > MIN_HOURS:
            counts["inside_36h"] += 1
            continue
        env = conn.execute("SELECT probabilities_json FROM basket_probability_envelope WHERE envelope_id=?",
                           (env_id,)).fetchone()
        snap = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?",
                            (snap_id,)).fetchone()
        if env is None or snap is None:
            counts["missing"] += 1
            continue
        buckets = json.loads(snap[0] or "[]")
        inputs = _ladder_inputs(json.loads(env[0] or "{}"), buckets)
        if inputs["state"] != "ok":
            counts["unusable"] += 1
            continue
        seen.add(key)  # the first complete >36h decision is the station-day's one
        label = labels.get(key)
        if label is None:
            counts["no_label"] += 1
            continue
        order = inputs["order"]
        if str(label["bucket"]) not in order:
            counts["label_off_ladder"] += 1
            continue
        f = inputs["features"]
        p, q = np.asarray(f["p"]), np.asarray(f["q"])
        b = layer.belief(f["terms"], theta)
        ask, bid = _quotes(buckets, order)
        rows.append({
            "station_id": key[0], "event_date": key[1], "decided": str(decided),
            "unit": "F" if key[0] in F_STATIONS else "C",
            "y": order.index(str(label["bucket"])), "p": p, "q": q, "b": b,
            "mode_market": int(f["mode"]), "mode_model": int(max(range(len(p)), key=lambda i: (p[i], -i))),
            "ask": ask, "bid": bid,
        })
    return rows, counts


def score(r: dict) -> dict:
    y = r["y"]
    out = {f"nll_{v}": -math.log(max(float(r[v][y]), 1e-300)) for v in ("p", "b", "q")}
    onehot = np.zeros(len(r["p"]))
    onehot[y] = 1.0
    for v in ("p", "b"):
        legs = _legs(r[v], r["ask"], r["bid"], r["mode_market"])
        for cell in FOUR:
            sel = [l for l in legs if l["cell"] == cell]
            out[f"{v}_claim_{cell}"] = sum(l["s"] * (float(r[v][l["k"]]) - l["price"]) for l in sel)
            out[f"{v}_real_{cell}"] = sum(l["s"] * (onehot[l["k"]] - l["price"]) for l in sel)
    # The reviews' fixed-leg comparison: p's legs, claim by p versus by b.
    for l in _legs(r["p"], r["ask"], r["bid"], r["mode_market"]):
        if l["cell"] in FOUR:
            out["fixedp_gap_p"] = out.get("fixedp_gap_p", 0.0) + l["s"] * (float(r["p"][l["k"]]) - onehot[l["k"]])
            out["fixedp_gap_b"] = out.get("fixedp_gap_b", 0.0) + l["s"] * (float(r["b"][l["k"]]) - onehot[l["k"]])
    out.setdefault("fixedp_gap_p", 0.0)
    out.setdefault("fixedp_gap_b", 0.0)
    return out


def summarise(rows: list[dict], scores: list[dict], rng: np.random.Generator) -> dict:
    days = sorted({r["event_date"] for r in rows})
    n_days = len(days)

    def mean(metric, idx):
        return float(np.mean([scores[i][metric] for i in idx])) if idx else None

    def diff(a, c, idx):
        return float(np.mean([scores[i][a] - scores[i][c] for i in idx])) if idx else None

    groups = {
        "all": list(range(len(rows))),
        "C": [i for i, r in enumerate(rows) if r["unit"] == "C"],
        "F": [i for i, r in enumerate(rows) if r["unit"] == "F"],
        "modes_agree": [i for i, r in enumerate(rows) if r["mode_model"] == r["mode_market"]],
        "modes_disagree": [i for i, r in enumerate(rows) if r["mode_model"] != r["mode_market"]],
    }
    stats = [("dNLL_b_minus_p", "nll_b", "nll_p"), ("dNLL_b_minus_q", "nll_b", "nll_q"),
             ("dNLL_p_minus_q", "nll_p", "nll_q")]
    by_day = {d: [i for i, r in enumerate(rows) if r["event_date"] == d] for d in days}
    out: dict = {"event_days": days, "station_days": len(rows),
                 "ready": n_days >= READY_DAYS and len(rows) >= READY_STATION_DAYS}
    for g, idx in groups.items():
        res = {"station_days": len(idx)}
        for name, a, c in stats:
            res[name] = diff(a, c, idx)
        for v in ("p", "b"):
            claim = sum(mean(f"{v}_claim_{c}", idx) or 0.0 for c in FOUR)
            real = sum(mean(f"{v}_real_{c}", idx) or 0.0 for c in FOUR)
            res[f"four_cell_{v}_own_legs"] = {"claim": claim, "realised": real, "overclaim": claim - real}
        res["fixed_p_legs_gap"] = {"p": mean("fixedp_gap_p", idx), "b": mean("fixedp_gap_b", idx)}
        out[g] = res
    if n_days >= 10:
        idx_set = {g: set(v) for g, v in groups.items()}
        ci: dict = {}
        metrics = {
            "dNLL_b_minus_p": lambda i: scores[i]["nll_b"] - scores[i]["nll_p"],
            "dNLL_b_minus_q": lambda i: scores[i]["nll_b"] - scores[i]["nll_q"],
            "b_overclaim_four": lambda i: sum(scores[i][f"b_claim_{c}"] - scores[i][f"b_real_{c}"] for c in FOUR),
            "p_overclaim_four": lambda i: sum(scores[i][f"p_claim_{c}"] - scores[i][f"p_real_{c}"] for c in FOUR),
        }
        for g in ("all", "modes_disagree"):
            draws = {m: [] for m in metrics}
            for _ in range(DRAWS):
                starts = rng.integers(0, n_days, size=math.ceil(n_days / BLOCK))
                picked = [days[(s + j) % n_days] for s in starts for j in range(BLOCK)][:n_days]
                idx = [i for d in picked for i in by_day[d] if i in idx_set[g]]
                if not idx:
                    continue
                for m, fn in metrics.items():
                    draws[m].append(float(np.mean([fn(i) for i in idx])))
            ci[g] = {m: [float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))] for m, v in draws.items() if v}
        out["block_ci95"] = ci
    return out


def main() -> int:
    fit_path = HERE / "frozen_fit.json"
    digest = hashlib.sha256(fit_path.read_bytes()).hexdigest()
    if digest != FROZEN_FIT_SHA256:
        raise SystemExit(f"frozen_fit_changed:{digest}")
    theta = json.loads(fit_path.read_text())["theta"]
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    try:
        rows, counts = load(conn, theta)
    finally:
        conn.close()
    scores = [score(r) for r in rows]
    result = {
        "schema": "realisation_acceptance_reading_v1",
        "read_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "freeze_utc": FREEZE_UTC, "frozen_fit_sha256": digest, "counts": counts,
        "summary": summarise(rows, scores, np.random.default_rng(SEED)),
    }
    stamp = result["read_at_utc"].replace(":", "").replace("-", "")
    (HERE / f"reading_{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
