#!/usr/bin/env python3
"""PROTOCOL.md: the charter 201 feature gate judged by RPS (today) and by charter 211's two proper scores.

The loop is scripts/fit_deterministic_feature_gate.py's ``fit`` (its helpers are
imported, the body is followed step for step); every arm is additionally
scored by bucket Brier and by the log score of the vector floored at charter
224's 1e-3.  Read-only; writes results.json.
"""
from __future__ import annotations

import collections
import importlib.util
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("gate", ROOT / "scripts" / "fit_deterministic_feature_gate.py")
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

DFC = G.DFC


def _brier(p, order, winner):
    return sum((p[k] - (1.0 if k == winner else 0.0)) ** 2 for k in order)


def _logscore(p, order, winner):
    floored, _ = floor_probabilities({k: float(p[k]) for k in order})
    return -math.log(max(floored[winner], 1e-12))


def main():
    now = datetime.now(timezone.utc)
    rng = np.random.default_rng(G.SEED)
    conn = __import__("sqlite3").connect(f"file:{G.DB}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    import fit_regional_point_debias as debias_script  # noqa: PLC0415
    runs_all, truth_all = debias_script.collect(G.DB)
    debias_cache = {}

    def debias_as_of(cursor):
        if cursor not in debias_cache:
            debias_cache[cursor] = G.fit_calibration(runs_all, truth_all, before_date=cursor)
        return debias_cache[cursor]

    today = now.date()
    start = (today - timedelta(days=G.WINDOW_DAYS)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    roster = [r[0] for r in conn.execute("SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    systems_at = collections.defaultdict(set)
    for system, station in conn.execute("SELECT DISTINCT ensemble_system_id, station_id FROM calibration_regional_point_run"):
        if system in G.FAMILY_SYSTEM_IDS:
            systems_at[str(station).upper()].add(str(system))
    out = {}
    for station in roster:
        if not systems_at.get(station):
            continue
        truth = G._truth(conn, station, start, end)
        zone = None
        cells = collections.defaultdict(list)
        for day in sorted(truth):
            rows = G.runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            envelopes = conn.execute(G.ENVELOPES_SQL, (station, day)).fetchall()
            by_tier = collections.defaultdict(list)
            for dec, tz, pj, oj, ivj, provj in envelopes:
                prov = json.loads(provj) if provj else {}
                if isinstance(prov, dict) and any(any("bias_refused" in n for n in (v.get("notes") or [])) for v in prov.values()):
                    continue
                hours = G.hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz)
                if hours < 12:
                    continue
                by_tier[G._tier(hours)].append((dec, tz, pj, oj, ivj))
            for tier, items in by_tier.items():
                if len(items) > G.MAX_ENVELOPES_PER_CELL:
                    step = len(items) / G.MAX_ENVELOPES_PER_CELL
                    items = [items[int(i * step)] for i in range(G.MAX_ENVELOPES_PER_CELL)]
                for dec, tz, pj, oj, ivj in items:
                    zone = tz
                    served = {k: float(v) for k, v in json.loads(pj).items()}
                    ordinals = json.loads(oj)
                    order = sorted(ordinals, key=lambda k: ordinals[k])
                    intervals = json.loads(ivj)
                    winner = G._winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                    centre = DFC.served_mean_c(served, order, intervals)
                    cursor = (G._ts(dec).date() - timedelta(days=1)).isoformat()
                    features = G.feature_at(conn, station_id=station, event_date=day, as_of_utc=dec,
                                            settlement_timezone=tz, rows=rows)
                    for system, feature in features.items():
                        correction = G.resolve_correction(
                            debias_as_of(cursor), system_id=system, station_id=station,
                            run_init_utc=feature["run_init_utc"],
                            lead_hours_to_local_day_start=feature["lead_hours_to_local_day_start"],
                            forecast_level_c=feature["day_max_c"])
                        if correction is None:
                            continue
                        gap = float(feature["day_max_c"]) + float(correction["bias_c"]) - centre
                        cells[(system, day)].append(dict(tier=tier, dec=dec, centre=centre, gap=gap, truth=truth[day],
                                                         served=served, order=order, intervals=intervals, winner=winner))
        if not cells:
            continue
        truth_known = {d: datetime.combine(date.fromisoformat(d) + timedelta(days=2), datetime.min.time(),
                                            tzinfo=ZoneInfo(zone or "UTC")) for d in truth}
        station_out = {}
        for system in sorted({s for s, _d in cells}):
            days = sorted(d for s, d in cells if s == system)
            cell_means = []
            for day in days:
                per_tier = collections.defaultdict(list)
                for item in cells[(system, day)]:
                    per_tier[item["tier"]].append(item)
                for tier, items in per_tier.items():
                    cell_means.append((day, tier, float(np.mean([i["centre"] for i in items])),
                                       float(np.mean([i["gap"] for i in items])), items[0]["truth"]))

            def k_at(when):
                known = [(c, g, t) for d, _t, c, g, t in cell_means if truth_known[d] <= when]
                n_days = len({d for d, *_rest in cell_means if truth_known[d] <= when})
                if n_days < DFC.MIN_K_EVENT_DAYS:
                    return 0.0, n_days
                den = sum(g * g for _c, g, _t in known)
                raw = sum((t - c) * g for c, g, t in known) / den if den > 0 else 0.0
                return min(1.0, max(0.0, raw)), n_days

            scored = collections.defaultdict(list)
            for day in days:
                for item in cells[(system, day)]:
                    k, n_days = k_at(G._ts(item["dec"]))
                    if n_days < DFC.MIN_K_EVENT_DAYS:
                        continue
                    delta = k * item["gap"]
                    arms = {"base": 0.0, "roll": delta, "rsA": G._sign(station, day) * delta, "rsB": -G._sign(station, day) * delta}
                    score = {}
                    for arm, d_c in arms.items():
                        p = DFC.shift_distribution(item["served"], item["order"], item["intervals"], d_c)
                        score[arm] = (G._rps(p, item["order"], item["winner"]), p[item["winner"]],
                                      _brier(p, item["order"], item["winner"]), _logscore(p, item["order"], item["winner"]))
                    scored[(day, item["tier"])].append(score)
            if not scored:
                station_out[system] = {"event_days": 0}
                continue

            def cell_pairs(fn):
                res = []
                for (day, _tier), items in scored.items():
                    values = [v for v in (fn(s) for s in items) if v is not None]
                    if values:
                        res.append((day, float(np.mean(values))))
                return res

            stat = {}
            for name, j in (("rps", 0), ("brier", 2), ("logscore", 3)):
                d = cell_pairs(lambda s, j=j: s["roll"][j] - s["base"][j])
                vs = cell_pairs(lambda s, j=j: s["roll"][j] - 0.5 * (s["rsA"][j] + s["rsB"][j]))
                per_day = collections.defaultdict(list)
                for day, value in d:
                    per_day[day].append(value)
                best_two = set(sorted(per_day, key=lambda x: np.mean(per_day[x]))[:2])
                stat[name] = G._bootstrap(d, rng)
                stat[name + "_drop_best2"] = G._bootstrap([(x, v) for x, v in d if x not in best_two], rng)
                stat[name + "_vs_random_sign"] = G._bootstrap(vs, rng)
                stat["event_days"] = len(per_day)
            stat["d_nll_nz"] = G._bootstrap(cell_pairs(lambda s: (-math.log(s["roll"][1]) + math.log(s["base"][1]))
                                                       if all(s[a][1] >= G.NLL_FLOOR for a in s) else None), rng)
            enough = stat["event_days"] >= DFC.GATE_MIN_EVENT_DAYS
            rps_pass = enough and all(stat[x] and stat[x][2] < 0 for x in ("rps", "rps_drop_best2", "rps_vs_random_sign")) \
                and stat["d_nll_nz"] is not None and stat["d_nll_nz"][0] <= 0
            p211 = enough and all(stat[x] and stat[x][2] < 0 for x in (
                "brier", "brier_drop_best2", "brier_vs_random_sign", "logscore", "logscore_drop_best2", "logscore_vs_random_sign"))
            stat["verdict_rps"] = "PASS" if rps_pass else ("INSUFFICIENT" if not enough else "FAIL")
            stat["verdict_211"] = "PASS" if p211 else ("INSUFFICIENT" if not enough else "FAIL")
            station_out[system] = stat
        out[station] = station_out
        print(station, {s: (v.get("event_days"), v.get("verdict_rps"), v.get("verdict_211")) for s, v in station_out.items()}, flush=True)
    conn.close()
    (HERE / "results.json").write_text(json.dumps({"as_of": now.isoformat(), "window": [start, end], "stations": out}, indent=1))


if __name__ == "__main__":
    main()
