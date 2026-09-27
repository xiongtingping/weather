#!/usr/bin/env python3
"""PROTOCOL.md: how many families the production P0 first decision seats, and what waiting for four buys.

Read-only against the production SSOT, so it runs on the machine that holds it:

    python3 docs/research/p0_first_decision_pools_20260927/audit.py [--ssot PATH] [--since 2026-09-01]

The first decision is the Proper line's own (``tiebreak_lane.stage_first_decisions``
plus ``proper_bet_lane._fact``); the seats are the envelope lineage's
``system_weights_json``; hours to settlement, the market vector and the floor
are production's functions.  Writes results.json beside this file.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

DEFAULT_SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
FAR_H = 36.0
THIN_MAX = 3
FULL_MIN = 4
THIN_SHARE_RULE = 0.10
PERIODS = (
    ("A_le_0919", "0000-00-00", "2026-09-19"),
    ("B_0920_0923", "2026-09-20", "2026-09-23"),
    ("C_ge_0924", "2026-09-24", "9999-99-99"),
)
BLOCK, BOOT, SEED = 3, 2000, 20260927

FACTS_SQL = (
    "SELECT station_id, event_date, decision_at_utc, settlement_timezone, probability_envelope_id,"
    " ws_snapshot_id, created_at_utc, fact_id"
    " FROM basket_policy_training_fact WHERE stage='P0' AND event_date>=?"
    " ORDER BY station_id, event_date, decision_at_utc, created_at_utc, fact_id"
)
ENVELOPE_SQL = (
    "SELECT json_extract(lineage_json,'$.system_weights_json'),"
    " json_extract(lineage_json,'$.system_family_ids_json'),"
    " json_extract(lineage_json,'$.independence_family_ids_json'),"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " probabilities_json, posterior_version, state"
    " FROM basket_probability_envelope WHERE envelope_id=?"
)
SNAPSHOT_SQL = "SELECT buckets_json, observed_at, state FROM basket_official_ws_snapshot WHERE snapshot_id=?"
TRUTH_SQL = (
    "SELECT station_id, local_date, official_tmax_c, revision_no FROM calibration_station_daily_high_truth"
    f" WHERE qc_status='ACCEPTED' AND source_lane IN {FORMAL_TRUTH_LANES_SQL} AND local_date>=?"
)


def _decode(value):
    """A lineage value, whether stored as an object or as JSON text (possibly twice)."""

    for _ in range(3):
        if isinstance(value, (bytes, bytearray)):
            value = value.decode()
        if not isinstance(value, str):
            return value
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value


def period_of(day: str) -> str:
    return next(name for name, lo, hi in PERIODS if lo <= day <= hi)


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._envelopes: dict[str, dict | None] = {}
        self._snapshots: dict[str, dict | None] = {}

    def envelope(self, envelope_id: str) -> dict | None:
        if envelope_id in self._envelopes:
            return self._envelopes[envelope_id]
        row = self.conn.execute(ENVELOPE_SQL, (envelope_id,)).fetchone()
        info = None
        if row is not None:
            weights = _decode(row[0]) or {}
            family_map = _decode(row[1]) or {}
            family_list = _decode(row[2]) or []
            seated = sorted(str(s) for s, w in weights.items() if float(w or 0.0) > 0.0) if isinstance(weights, dict) else []
            if seated and isinstance(family_map, dict) and all(s in family_map for s in seated):
                families = sorted({str(family_map[s]) for s in seated})
                family_source = "system_family_ids_json"
            elif isinstance(family_list, list) and family_list:
                families = sorted(str(f) for f in family_list)
                family_source = "independence_family_ids_json"
            else:
                families, family_source = [], "UNKNOWN"
            info = {
                "seated": seated, "families": families, "family_source": family_source,
                "n_families": len(families) if families else None,
                "intervals": _decode(row[3]) or {},
                "probabilities": _decode(row[4]) or {},
                "version": str(row[5]), "state": str(row[6]),
            }
        self._envelopes[envelope_id] = info
        return info

    def market(self, snapshot_id: str) -> dict | None:
        if snapshot_id in self._snapshots:
            return self._snapshots[snapshot_id]
        row = self.conn.execute(SNAPSHOT_SQL, (snapshot_id,)).fetchone()
        out = None
        if row is not None:
            buckets = _decode(row[0]) or []
            reference = market_reference_v1(buckets)
            out = {"state": reference.get("state"), "reason": reference.get("reason"),
                   "q": {str(k): float(v) for k, v in (reference.get("vector") or {}).items()},
                   "observed_at": str(row[1]), "snapshot_state": str(row[2])}
        self._snapshots[snapshot_id] = out
        return out


def truth_table(conn: sqlite3.Connection, since: str) -> dict[tuple[str, str], float]:
    best: dict[tuple[str, str], tuple[float, int]] = {}
    for station, day, value, rev in conn.execute(TRUTH_SQL, (since,)):
        key = (str(station), str(day))
        if key not in best or int(rev) > best[key][1]:
            best[key] = (float(value), int(rev))
    return {k: v[0] for k, v in best.items()}


def truth_rung(intervals: dict, value: float) -> str | None:
    for key, bounds in intervals.items():
        lo, hi = bounds
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return str(key)
    return None


def floored(vector: dict) -> dict[str, float] | None:
    total = sum(float(v) for v in vector.values())
    if not (total > 0.0 and math.isfinite(total)):
        return None
    out, _ = floor_probabilities({str(k): float(v) / total for k, v in vector.items()})
    return out


def scores(p: dict[str, float], rung: str) -> tuple[float, float]:
    return (-math.log(max(p[rung], 1e-12)),
            sum((v - (1.0 if k == rung else 0.0)) ** 2 for k, v in p.items()))


def quantiles(values):
    if not values:
        return None
    return [round(float(np.percentile(values, q)), 1) for q in (10, 50, 90)]


def short(system: str) -> str:
    for prefix in ("ECMWF_", "NOAA_", "DWD_", "ECCC_"):
        system = system.replace(prefix, "")
    return system


# ---------------------------------------------------------------- question one

def question_one(store: Store, days: dict, proper_rows: list) -> dict:
    out = {}
    for name, _lo, _hi in PERIODS:
        firsts = [d for key, d in days.items() if period_of(key[1]) == name]
        if not firsts:
            out[name] = {"first_decisions": 0}
            continue
        fams = Counter()
        far_fams = Counter()
        combos = Counter()
        hours = []
        unknown = 0
        for d in firsts:
            info = d["first_info"]
            n = info["n_families"] if info else None
            if n is None:
                unknown += 1
            fams[n] += 1
            hours.append(d["first_h"])
            if d["first_h"] > FAR_H:
                far_fams[n] += 1
                if info:
                    combos[" + ".join(short(s) for s in info["seated"])] += 1
        far_total = sum(far_fams.values())
        thin = sum(c for n, c in far_fams.items() if n is not None and n <= THIN_MAX)
        # Reference: every far P0 fact's envelope, station-day equal weight, split over its envelopes.
        ref = Counter()
        ref_days = 0
        for key, d in days.items():
            if period_of(key[1]) != name or not d["far_envelopes"]:
                continue
            ref_days += 1
            share = 1.0 / len(d["far_envelopes"])
            for env in d["far_envelopes"]:
                info = store.envelope(env)
                ref[info["n_families"] if info else None] += share
        out[name] = {
            "first_decisions": len(firsts),
            "unknown_families": unknown,
            "families_all": {str(k): v for k, v in sorted(fams.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))},
            "hours_to_settlement_p10_p50_p90": quantiles(hours),
            "share_far": round(far_total / len(firsts), 4),
            "families_far": {str(k): v for k, v in sorted(far_fams.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))},
            "thin_share_far": round(thin / far_total, 4) if far_total else None,
            "top_far_combinations": combos.most_common(5),
            "reference_all_far_facts": {"station_days": ref_days,
                                        "families": {str(k): round(v / ref_days, 4) for k, v in
                                                     sorted(ref.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))}
                                        if ref_days else {}},
        }
    proper = {"rows": 0}
    if proper_rows:
        allc, fired = Counter(), Counter()
        for row in proper_rows:
            info = store.envelope(row["envelope_id"]) if row["envelope_id"] else None
            n = info["n_families"] if info else None
            allc[n] += 1
            if row["planned_legs"]:
                fired[n] += 1
        proper = {"rows": len(proper_rows),
                  "families_all": {str(k): v for k, v in allc.items()},
                  "families_fired": {str(k): v for k, v in fired.items()}}
    out["proper_lane_evaluations"] = proper
    c = out.get("C_ge_0924", {})
    share = c.get("thin_share_far")
    out["verdict"] = (
        "NO_DATA" if share is None
        else "THIN_FIRST_DECISIONS_NOT_A_PRODUCTION_PHENOMENON" if share < THIN_SHARE_RULE
        else "THIN_FIRST_DECISIONS_PRESENT"
    )
    return out


# ---------------------------------------------------------------- question two

def question_two(store: Store, days: dict, truth: dict) -> dict:
    skipped = Counter()
    pairs = []
    for key, d in days.items():
        if d["first_h"] <= FAR_H:
            continue
        first_info = d["first_info"]
        if not first_info or first_info["n_families"] is None or first_info["n_families"] > THIN_MAX:
            continue
        if key not in truth:
            skipped["no_truth"] += 1
            continue
        fuller = None
        for fact in d["facts"][1:]:
            if fact["h"] <= FAR_H:
                break
            info = store.envelope(fact["envelope_id"])
            if info and info["n_families"] is not None and info["n_families"] >= FULL_MIN:
                fuller = (fact, info)
                break
        if fuller is None:
            skipped["never_full_inside_window"] += 1
            continue
        rows = []
        for fact, info in ((d["facts"][0], first_info), fuller):
            market = store.market(fact["snapshot_id"])
            if not market or market["state"] != "complete":
                rows = None
                skipped["market_incomplete"] += 1
                break
            p, q = floored(info["probabilities"]), floored(market["q"])
            if p is None or q is None or set(p) != set(q):
                rows = None
                skipped["ladder_mismatch"] += 1
                break
            rung = truth_rung(info["intervals"], truth[key])
            if rung is None or rung not in p:
                rows = None
                skipped["no_truth_rung"] += 1
                break
            ls_p, br_p = scores(p, rung)
            ls_q, br_q = scores(q, rung)
            kl = sum(v * math.log(v / q[k]) for k, v in p.items())
            rows.append({"h": fact["h"], "at": fact["decision_at_utc"], "n_families": info["n_families"],
                         "q": q, "ls_p": ls_p, "ls_q": ls_q, "brier_p": br_p, "brier_q": br_q,
                         "edge": ls_q - ls_p, "kl_pq": kl})
        if rows is None:
            continue
        first, full = rows
        move = 0.5 * sum(abs(full["q"][k] - first["q"][k]) for k in first["q"])
        pairs.append({"station": key[0], "day": key[1], "wait_h": first["h"] - full["h"],
                      "first": {k: v for k, v in first.items() if k != "q"},
                      "fuller": {k: v for k, v in full.items() if k != "q"}, "q_half_l1_move": move})
    out = {"pairs": len(pairs), "skipped": dict(skipped)}
    if not pairs:
        return out
    metrics = ("edge", "ls_p", "ls_q", "brier_p", "brier_q", "kl_pq")
    first = {m: np.array([pr["first"][m] for pr in pairs]) for m in metrics}
    full = {m: np.array([pr["fuller"][m] for pr in pairs]) for m in metrics}
    days_sorted = sorted({pr["day"] for pr in pairs})
    by_day = defaultdict(list)
    for j, pr in enumerate(pairs):
        by_day[pr["day"]].append(j)
    rng = np.random.default_rng(SEED)
    n_blocks = math.ceil(len(days_sorted) / BLOCK)
    draws = [np.concatenate([by_day[days_sorted[(s + b) % len(days_sorted)]]
                             for s in rng.integers(0, len(days_sorted), n_blocks) for b in range(BLOCK)])
             for _ in range(BOOT)]
    for m in metrics:
        d = full[m] - first[m]
        boots = [float(d[j].mean()) for j in draws]
        out[m] = {"first": float(first[m].mean()), "fuller": float(full[m].mean()), "delta": float(d.mean()),
                  "ci95": [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]}
    out["wait_hours_p25_p50_p75"] = [round(float(np.percentile([p["wait_h"] for p in pairs], q)), 1) for q in (25, 50, 75)]
    out["q_half_l1_move_p25_p50_p75"] = [round(float(np.percentile([p["q_half_l1_move"] for p in pairs], q)), 4)
                                         for q in (25, 50, 75)]
    out["event_days"] = len(days_sorted)
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssot", default=str(DEFAULT_SSOT))
    parser.add_argument("--since", default="2026-09-01", help="first event date read")
    parser.add_argument("--out", default=str(HERE / "results.json"))
    args = parser.parse_args()
    started = time.time()
    conn = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    store = Store(conn)

    days: dict[tuple[str, str], dict] = {}
    facts_read = 0
    for station, day, at, tz, env, snap, _created, fact_id in conn.execute(FACTS_SQL, (args.since,)):
        facts_read += 1
        key = (str(station), str(day))
        h = hours_to_settlement(str(day), as_of_utc=str(at), settlement_timezone=str(tz))
        days.setdefault(key, {"facts": []})["facts"].append(
            {"decision_at_utc": str(at), "h": float(h), "envelope_id": str(env), "snapshot_id": str(snap),
             "fact_id": str(fact_id)})
    for key, d in days.items():
        first = d["facts"][0]
        d["first_h"] = first["h"]
        d["first_info"] = store.envelope(first["envelope_id"])
        d["far_envelopes"] = sorted({f["envelope_id"] for f in d["facts"] if f["h"] > FAR_H})
    print(f"P0 facts {facts_read}, station-days {len(days)} ({time.time() - started:.0f}s)", flush=True)

    proper_rows = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='proper_bet_lane_evaluation'").fetchone():
        for station, day, env, legs in conn.execute(
                "SELECT station_id, event_date, envelope_id, planned_legs FROM proper_bet_lane_evaluation"
                " WHERE stage='P0' AND event_date>=?", (args.since,)):
            proper_rows.append({"station": str(station), "day": str(day), "envelope_id": env, "planned_legs": int(legs or 0)})

    q1 = question_one(store, days, proper_rows)
    print(f"question one done ({time.time() - started:.0f}s)", flush=True)
    q2 = question_two(store, days, truth_table(conn, args.since))
    result = {"ssot": str(args.ssot), "since": args.since, "facts_read": facts_read, "station_days": len(days),
              "envelopes_read": len(store._envelopes), "question_one": q1, "question_two": q2,
              "question_two_reading": ("report_only" if q1["verdict"] != "THIN_FIRST_DECISIONS_PRESENT"
                                       else "per_protocol")}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))

    for name, _lo, _hi in PERIODS:
        r = q1[name]
        print(f"== {name}: first decisions {r['first_decisions']}")
        if r["first_decisions"]:
            print(f"   families (all) {r['families_all']}; hours p10/p50/p90 {r['hours_to_settlement_p10_p50_p90']}; far share {r['share_far']}")
            print(f"   families (far) {r['families_far']}; thin share (far) {r['thin_share_far']}")
            print(f"   top far combinations {r['top_far_combinations']}")
            print(f"   reference, all far facts {r['reference_all_far_facts']}")
    print("proper lane evaluations:", q1["proper_lane_evaluations"])
    print("VERDICT (question one):", q1["verdict"])
    print(f"question two ({result['question_two_reading']}): pairs {q2['pairs']}, skipped {q2['skipped']}")
    for m in ("edge", "ls_p", "ls_q", "brier_p", "brier_q", "kl_pq"):
        if m in q2:
            v = q2[m]
            print(f"   {m:8s} first {v['first']:.4f} -> fuller {v['fuller']:.4f} ({v['delta']:+.4f} [{v['ci95'][0]:+.4f},{v['ci95'][1]:+.4f}])")
    if q2.get("pairs"):
        print(f"   wait h p25/p50/p75 {q2['wait_hours_p25_p50_p75']}; q move p25/p50/p75 {q2['q_half_l1_move_p25_p50_p75']}")
    print(f"done in {time.time() - started:.0f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
