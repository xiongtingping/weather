#!/usr/bin/env python3
"""Charter 201 step 2: the P0 deterministic-feature gate, fitted daily on settled days.

For every station and every family source with rows at it, over the settled
event days of the last ``WINDOW_DAYS``: each clean P0 envelope (no refused
bias, >= 12 h to settlement) is read with the feature knowable at its
decision (``deterministic_forecast_family.feature_at``), the source's debias
as of the day before the decision (``regional_point_debias.fit_calibration``
on rows and truth up to then) and a rolling k fitted on the station's own
earlier cells whose truth was known by then; its served vector is shifted by
delta = k * ((R + b) - c) (capped) with the width layer's transform, and the
three are scored against the settled bucket.  Cells are (event day, lead
tier) means; intervals resample event days (never stations).

Pre-registered (research: docs/research/station_regional_features_20260922):

    PASS  >= GATE_MIN_EVENT_DAYS evaluable event days, and upper 95% bounds
          below zero for dRPS, dRPS with the two best days removed, and dRPS
          against the random-sign control, and mean dNLL on rows where every
          arm put >= 1e-4 on the truth not above zero.

Writes one JSON artifact (``deterministic_feature_correction.GATE_ARTIFACT_
PATH``): per station, per source, the stats, the verdict, the serving k
(the rolling k over every settled cell), and the station's candidate (the
passing source with the lowest dRPS, else the best-reading source for the
shadow record).  SSOT ``mode=ro``; no database write.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import deterministic_feature_correction as DFC  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis  # noqa: E402
from bayes_basket_target.delta_v2_settlement_label import _winning_bucket  # noqa: E402
from bayes_basket_target.deterministic_forecast_family import (  # noqa: E402
    FAMILY_SYSTEM_IDS, feature_at,
)
from bayes_basket_target.regional_point_debias import (  # noqa: E402
    fit_calibration, resolve_correction,
)
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
WINDOW_DAYS = 60
MAX_ENVELOPES_PER_CELL = 12
NLL_FLOOR = 1e-4
BOOTSTRAP = 2000
SEED = 20260923

ENVELOPES_SQL = (
    "SELECT decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.provisional_parameter_sources_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P0'"
    " ORDER BY decision_at_utc"
)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _tier(hours: float) -> str:
    return "12-24" if hours < 24 else "24-36" if hours < 36 else "36-48" if hours < 48 else "48+"


def _rps(p, order, winner) -> float:
    k = order.index(winner)
    cp = total = 0.0
    for i in range(len(order) - 1):
        cp += p[order[i]]
        total += (cp - (1.0 if i >= k else 0.0)) ** 2
    return total


def _sign(station: str, day: str) -> float:
    return 1.0 if int(hashlib.md5(f"{station}|{day}".encode()).hexdigest()[:8], 16) % 2 == 0 else -1.0


def _truth(conn, station: str, start: str, end: str) -> dict[str, float]:
    out: dict[str, tuple[int, float]] = {}
    for day, rev, tmax in conn.execute(
        "SELECT event_date, truth_revision_no, settled_tmax_c FROM basket_station_day_settlement_label"
        " WHERE station_id=? AND event_date>=? AND event_date<=?", (station, start, end),
    ):
        if day not in out or rev > out[day][0]:
            out[day] = (int(rev), float(tmax))
    return {day: value for day, (_rev, value) in out.items()}


def _bootstrap(pairs, rng) -> list[float] | None:
    if not pairs:
        return None
    grouped = collections.defaultdict(list)
    for day, value in pairs:
        grouped[day].append(value)
    days = list(grouped)
    sums = np.array([sum(grouped[d]) for d in days])
    counts = np.array([len(grouped[d]) for d in days])
    idx = rng.integers(0, len(days), (BOOTSTRAP, len(days)))
    draws = sums[idx].sum(1) / counts[idx].sum(1)
    return [float(np.mean([v for _, v in pairs])), float(np.percentile(draws, 2.5)),
            float(np.percentile(draws, 97.5))]


def fit(db: Path, *, now: datetime, stations: list[str] | None = None) -> dict:
    rng = np.random.default_rng(SEED)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    sys.path.insert(0, str(ROOT / "scripts"))
    import fit_regional_point_debias as debias_script  # noqa: PLC0415

    runs_all, truth_all = debias_script.collect(db)
    debias_cache: dict[str, dict] = {}

    def debias_as_of(cursor: str) -> dict:
        if cursor not in debias_cache:
            debias_cache[cursor] = fit_calibration(runs_all, truth_all, before_date=cursor)
        return debias_cache[cursor]

    today = now.astimezone(timezone.utc).date()
    start = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    roster = stations or [r[0] for r in conn.execute(
        "SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    systems_at = collections.defaultdict(set)
    for system, station in conn.execute(
        "SELECT DISTINCT ensemble_system_id, station_id FROM calibration_regional_point_run"
    ):
        if system in FAMILY_SYSTEM_IDS:
            systems_at[str(station).upper()].add(str(system))
    out_stations: dict[str, dict] = {}
    for station in roster:
        if not systems_at.get(station):
            continue
        truth = _truth(conn, station, start, end)
        zone = None
        cells: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
        for day in sorted(truth):
            rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            envelopes = conn.execute(ENVELOPES_SQL, (station, day)).fetchall()
            by_tier: dict[str, list] = collections.defaultdict(list)
            for dec, tz, pj, oj, ivj, provj in envelopes:
                prov = json.loads(provj) if provj else {}
                if isinstance(prov, dict) and any(
                    any("bias_refused" in n for n in (v.get("notes") or [])) for v in prov.values()
                ):
                    continue
                hours = hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz)
                if hours < 12:
                    continue
                by_tier[_tier(hours)].append((dec, tz, pj, oj, ivj))
            for tier, items in by_tier.items():
                if len(items) > MAX_ENVELOPES_PER_CELL:
                    step = len(items) / MAX_ENVELOPES_PER_CELL
                    items = [items[int(i * step)] for i in range(MAX_ENVELOPES_PER_CELL)]
                for dec, tz, pj, oj, ivj in items:
                    zone = tz
                    served = {k: float(v) for k, v in json.loads(pj).items()}
                    ordinals = json.loads(oj)
                    order = sorted(ordinals, key=lambda k: ordinals[k])
                    intervals = json.loads(ivj)
                    winner = _winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                    centre = DFC.served_mean_c(served, order, intervals)
                    cursor = (_ts(dec).date() - timedelta(days=1)).isoformat()
                    features = feature_at(conn, station_id=station, event_date=day, as_of_utc=dec,
                                          settlement_timezone=tz, rows=rows)
                    for system, feature in features.items():
                        correction = resolve_correction(
                            debias_as_of(cursor), system_id=system, station_id=station,
                            run_init_utc=feature["run_init_utc"],
                            lead_hours_to_local_day_start=feature["lead_hours_to_local_day_start"],
                            forecast_level_c=feature["day_max_c"],
                        )
                        if correction is None:
                            continue
                        gap = float(feature["day_max_c"]) + float(correction["bias_c"]) - centre
                        cells[(system, day)].append(dict(
                            tier=tier, dec=dec, centre=centre, gap=gap, truth=truth[day],
                            served=served, order=order, intervals=intervals, winner=winner,
                        ))
        if not cells:
            continue
        stats_by_system: dict[str, dict] = {}
        truth_known = {d: datetime.combine(date.fromisoformat(d) + timedelta(days=2), datetime.min.time(),
                                            tzinfo=ZoneInfo(zone or "UTC")) for d in truth}
        for system in sorted({s for s, _d in cells}):
            days = sorted(d for s, d in cells if s == system)
            # cell means for the rolling k: (day, tier) -> (centre, gap, truth)
            cell_means = []
            for day in days:
                per_tier = collections.defaultdict(list)
                for item in cells[(system, day)]:
                    per_tier[item["tier"]].append(item)
                for tier, items in per_tier.items():
                    cell_means.append((day, tier, float(np.mean([i["centre"] for i in items])),
                                       float(np.mean([i["gap"] for i in items])), items[0]["truth"]))

            def k_at(when: datetime) -> tuple[float, int]:
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
                    k, n_days = k_at(_ts(item["dec"]))
                    if n_days < DFC.MIN_K_EVENT_DAYS:
                        continue
                    delta = k * item["gap"]
                    arms = {"base": 0.0, "roll": delta, "rsA": _sign(station, day) * delta,
                            "rsB": -_sign(station, day) * delta}
                    score = {}
                    for arm, d_c in arms.items():
                        p = DFC.shift_distribution(item["served"], item["order"], item["intervals"], d_c)
                        score[arm] = (_rps(p, item["order"], item["winner"]), p[item["winner"]])
                    scored[(day, item["tier"])].append(score)
            if not scored:
                stats_by_system[system] = {"verdict": "INSUFFICIENT", "event_days": 0}
                continue

            def cell_pairs(fn):
                out = []
                for (day, _tier), items in scored.items():
                    values = [fn(s) for s in items]
                    values = [v for v in values if v is not None]
                    if values:
                        out.append((day, float(np.mean(values))))
                return out

            d_rps = cell_pairs(lambda s: s["roll"][0] - s["base"][0])
            vs_sign = cell_pairs(lambda s: s["roll"][0] - 0.5 * (s["rsA"][0] + s["rsB"][0]))
            d_nll = cell_pairs(lambda s: (-math.log(s["roll"][1]) + math.log(s["base"][1]))
                               if all(s[a][1] >= NLL_FLOOR for a in s) else None)
            per_day = collections.defaultdict(list)
            for day, value in d_rps:
                per_day[day].append(value)
            best_two = set(sorted(per_day, key=lambda d: np.mean(per_day[d]))[:2])
            dropped = [(d, v) for d, v in d_rps if d not in best_two]
            stat = dict(
                event_days=len(per_day), cells=len(d_rps),
                d_rps=_bootstrap(d_rps, rng), d_rps_drop_best2=_bootstrap(dropped, rng),
                d_rps_vs_random_sign=_bootstrap(vs_sign, rng), d_nll_nz=_bootstrap(d_nll, rng),
            )
            passed = (
                stat["event_days"] >= DFC.GATE_MIN_EVENT_DAYS
                and stat["d_rps"] and stat["d_rps"][2] < 0
                and stat["d_rps_drop_best2"] and stat["d_rps_drop_best2"][2] < 0
                and stat["d_rps_vs_random_sign"] and stat["d_rps_vs_random_sign"][2] < 0
                and stat["d_nll_nz"] is not None and stat["d_nll_nz"][0] <= 0
            )
            stat["verdict"] = "PASS" if passed else (
                "INSUFFICIENT" if stat["event_days"] < DFC.GATE_MIN_EVENT_DAYS else "FAIL"
            )
            k_now, n_now = k_at(now)
            stat["k"] = round(k_now, 4)
            stat["k_event_days"] = n_now
            stats_by_system[system] = stat
        passing = [s for s, v in stats_by_system.items() if v.get("verdict") == "PASS"]
        readable = [s for s, v in stats_by_system.items() if v.get("d_rps")]
        candidate = (
            min(passing, key=lambda s: stats_by_system[s]["d_rps"][0]) if passing
            else min(readable, key=lambda s: stats_by_system[s]["d_rps"][0]) if readable else None
        )
        out_stations[station] = {
            "sources": stats_by_system,
            "selected_system": min(passing, key=lambda s: stats_by_system[s]["d_rps"][0]) if passing else None,
            "candidate_system": candidate,
        }
    conn.close()
    return {
        "schema": DFC.GATE_SCHEMA, "rule": DFC.RULE_VERSION,
        "fitted_at_utc": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [start, end], "gate_min_event_days": DFC.GATE_MIN_EVENT_DAYS,
        "min_k_event_days": DFC.MIN_K_EVENT_DAYS, "cap_c": DFC.CAP_C,
        "stations": out_stations,
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, indent=1, sort_keys=True))
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--out", type=Path, default=DFC.GATE_ARTIFACT_PATH)
    parser.add_argument("--stations", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    stations = [s.strip().upper() for s in args.stations.split(",") if s.strip()] or None
    payload = fit(args.db, now=datetime.now(timezone.utc), stations=stations)
    for station, entry in sorted(payload["stations"].items()):
        line = []
        for system, stat in sorted(entry["sources"].items()):
            d = stat.get("d_rps")
            line.append(f"{system.split('_')[0]}:{stat['verdict']}"
                        + (f"({d[0]:+.3f}[{d[1]:+.3f},{d[2]:+.3f}] n={stat['event_days']} k={stat.get('k')})" if d else ""))
        print(station, "候选", entry.get("candidate_system"), "过门", entry.get("selected_system"), "|", " ".join(line))
    if not args.dry_run:
        _write(args.out, payload)
    print(f"用时 {time.monotonic() - started:.0f}s，{len(payload['stations'])} 站")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
