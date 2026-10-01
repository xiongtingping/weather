"""Conditional-belief research sample: one decision per station-day-stage-3h block.

Read-only.  The population is the valuation lane's own (charter 132/140);
every feature is read as of the decision instant -- from the decision's own
envelope lineage where the stage recorded it, otherwise from a table's
versions filtered to what was knowable then.  See PROTOCOL.md.
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path('docs/research/p1_heating_rain_20261001')
ROOT = Path('/Users/xiong/weather')
sys.path.insert(0, str(ROOT))

from bayes_basket_target import valuation_store as vs  # noqa: E402
from bayes_basket_target.center_width_calibration import served_bucket_axis  # noqa: E402
from bayes_basket_target.cross_consensus import clearness_index  # noqa: E402
from bayes_basket_target.materialization import rehydrate_lineage_documents  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
# P1's own observation-likelihood sample: one row per report instant, every
# feature built from the report prefix up to it (the fitter's as-of table).
P1_SAMPLES = ROOT / "data" / "runtime" / "bayes_basket_target_nwp" / "p1_observation_samples.db"
# A report is read only once it could have arrived.
REPORT_LAG = timedelta(minutes=15)
P1_SAMPLE_FIELDS = (
    "remaining_hours", "hours_since_high_refresh", "drawdown_from_high_c",
    "momentum_c_per_hour", "warming_so_far_c", "running_high_c",
    "cloud_cover_fraction", "precipitation_present", "dewpoint_depression_c",
    "wind_speed_mps", "regional_headroom_c", "regional_live_gap_c",
)
WINDOW_DAYS = 60
REGIONAL_MAX_AGE_H = 18.0


def instant(value):
    if value in (None, "", "NONE"):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def number(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def lineage_of(conn, row):
    hit = conn.execute(
        "SELECT e.lineage_json FROM basket_policy_training_fact f"
        " JOIN basket_probability_envelope e ON e.envelope_id = f.probability_envelope_id"
        " WHERE f.station_id=? AND f.event_date=? AND f.stage=? AND f.decision_at_utc=? LIMIT 1",
        (row["station"], row["date"], row["stage"], row["at"]),
    ).fetchone()
    return None if hit is None else hit[0]


def taf_rows(conn, station, day):
    rows = []
    for r in conn.execute(
        "SELECT issued_at_utc, received_at_utc, provider_available_at_utc,"
        " valid_from_utc, valid_until_utc, tx_c, tx_available,"
        " cloud_cover_fraction, precipitation_probability, wind_speed_mps"
        " FROM calibration_forward_taf_capture WHERE station_id=? AND event_date=?",
        (station, day),
    ):
        known = max(filter(None, (instant(r[0]), instant(r[1]), instant(r[2]))), default=None)
        rows.append({
            "issued": instant(r[0]), "known": known, "tx": number(r[5]) if r[6] else None,
            "cloud": number(r[7]), "precip": number(r[8]), "wind": number(r[9]),
        })
    return rows


def physics_rows(conn, station, day):
    rows = []
    for r in conn.execute(
        "SELECT captured_at_utc, capture_mode, shortwave_radiation_sum, cloudcover_mean,"
        " precipitation_sum, windspeed_10m_max, latitude"
        " FROM calibration_station_forecast_physics_version"
        " WHERE station_id=? AND local_date=? AND capture_mode IN ('forward','p0_envelope_receipt')",
        (station, day),
    ):
        rows.append({
            "at": instant(r[0]), "radiation": number(r[2]), "cloud": number(r[3]),
            "precip": number(r[4]), "wind": number(r[5]),
            "kt": clearness_index(number(r[2]), number(r[6]), day),
        })
    return rows


def regional_rows(conn, station, day, as_of):
    from bayes_basket_target.regional_point_lane import runs_for  # noqa: PLC0415
    try:
        return runs_for(conn, station_id=station, event_date=day, as_of_utc=as_of)
    except sqlite3.Error:
        return []


def p1_samples(samples, station, day):
    return [
        {"known": instant(r[0]) + REPORT_LAG, **dict(zip(P1_SAMPLE_FIELDS, map(number, r[1:])))}
        for r in samples.execute(
            f"SELECT as_of_utc, {', '.join(P1_SAMPLE_FIELDS)} FROM metar_phase_hazard_sample"
            " WHERE station_id=? AND event_date=?",
            (station, day),
        )
    ]


def latest_known(rows, key, at):
    best = None
    for row in rows:
        stamp = row.get(key)
        if stamp is None or stamp > at:
            continue
        if best is None or stamp > best[key]:
            best = row
    return best


def features_for(stage, row, lineage, keys, centers, cache, conn, samples):
    at = instant(row["at"])
    tz = ZoneInfo(row["settlement_timezone"])
    end = datetime.fromisoformat(row["date"]).replace(tzinfo=tz) + timedelta(days=1)
    hours = max(0.0, (end - at).total_seconds() / 3600.0)
    p = [row["p"].get(k, 0.0) for k in keys]
    m = [row["m"][k] for k in keys]
    total = sum(p) or 1.0
    p = [v / total for v in p]
    mu = sum(c * v for c, v in zip(centers, p))
    sd = math.sqrt(max(1e-6, sum(v * (c - mu) ** 2 for c, v in zip(centers, p))))
    ms = sorted(m, reverse=True)
    entropy = -sum(v * math.log(v) for v in m if v > 0)
    f = {
        "hours": hours, "sigma_p": sd, "peak_p": max(p), "mu_p": mu,
        "top_gap_m": ms[0] - (ms[1] if len(ms) > 1 else 0.0), "entropy_m": entropy,
    }
    centers_json = lineage.get("native_calibrated_system_centers_c_json")
    try:
        span = json.loads(centers_json) if centers_json else {}
        vals = [float(v) for v in span.values()]
        f["system_span"] = (max(vals) - min(vals)) if len(vals) > 1 else None
    except (TypeError, ValueError):
        f["system_span"] = None
    key = (row["station"], row["date"])
    if key not in cache["taf"]:
        cache["taf"][key] = taf_rows(conn, *key)
        cache["phys"][key] = physics_rows(conn, *key)
        cache["obs"][key] = p1_samples(samples, *key) if stage == "P1" else []
    # TAF: the stage's own record when it has one, else the capture table as of now.
    tx = number(lineage.get("taf_observed_tx_c"))
    taf = latest_known(cache["taf"][key], "known", at)
    if tx is None and taf is not None:
        tx = taf["tx"]
    f["taf_tx"] = tx
    f["taf_age_h"] = ((at - taf["issued"]).total_seconds() / 3600.0) if taf and taf["issued"] else None
    phys = latest_known(cache["phys"][key], "at", at)
    f["radiation"] = phys["radiation"] if phys else None
    f["kt"] = phys["kt"] if phys else None
    f["phys_cloud"] = (phys or {}).get("cloud")
    f["phys_precip"] = (phys or {}).get("precip")
    f["phys_wind"] = (phys or {}).get("wind")
    # The version table's cloud cover is a percentage; the TAF's is a fraction.
    phys_cloud = (phys or {}).get("cloud")
    f["cloud"] = (taf or {}).get("cloud") if (taf or {}).get("cloud") is not None else (
        None if phys_cloud is None else phys_cloud / 100.0)
    f["precip"] = (taf or {}).get("precip")
    f["wind"] = (taf or {}).get("wind")
    # Regional: the median of each system's freshest knowable run, <=18 h old.
    runs = regional_rows(conn, row["station"], row["date"], row["at"])
    fresh = {}
    for run in runs:
        init = instant(run["run_init_utc"])
        if init is None or (at - init).total_seconds() / 3600.0 > REGIONAL_MAX_AGE_H:
            continue
        fresh.setdefault(run["ensemble_system_id"], run["forecast_tmax_c"])
    f["regional_tmax"] = statistics.median(fresh.values()) if fresh else None
    f["regional_n"] = len(fresh)
    # Observations and the P1 step's own features.
    high = number(lineage.get("observed_running_high_c"))
    if high is None:
        high = number(lineage.get("reported_high_c"))
    obs = {}
    raw = lineage.get("p1_observation_likelihood_json")
    if raw:
        try:
            receipt = json.loads(raw)
            obs = dict(receipt.get("features") or {})
            high = number(receipt.get("running_high_c")) if high is None else high
        except (TypeError, ValueError):
            obs = {}
    f["running_high"] = high
    state = latest_known(cache["obs"][key], "known", at)
    f["obs_age_h"] = None
    for name in P1_SAMPLE_FIELDS:
        f["obs_" + name] = state[name] if state else None
    if state:
        f["obs_age_h"] = (at - state["known"]).total_seconds() / 3600.0 + REPORT_LAG.total_seconds() / 3600.0
    for name in ("remaining_hours", "warming_so_far_c", "drawdown_from_high_c", "momentum_c_per_hour",
                 "regional_headroom_c", "regional_live_gap_c", "dewpoint_depression_c",
                 "root_bias_recent_c", "cloud_cover_fraction", "precipitation_present", "wind_speed_mps"):
        f["p1_" + name] = number(obs.get(name))
    return f


def build():
    started = time.time()
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    samples = sqlite3.connect(f"file:{P1_SAMPLES}?mode=ro", uri=True, timeout=60)
    out = []
    counts = defaultdict(int)
    for scope in ("P0",):
        rows, meta = vs.population(conn, stage_scope=scope, window_days=WINDOW_DAYS, anchored=False)
        print(scope, "population", len(rows), round(time.time() - started), flush=True)
        blocks = defaultdict(list)
        for row in rows:
            tz = ZoneInfo(row["settlement_timezone"])
            local = instant(row["at"]).astimezone(tz)
            blocks[(row["station"], row["date"], row["stage"], local.date().isoformat(), local.hour // 3)].append(row)
        per_day = defaultdict(int)
        for block_key in blocks:
            per_day[block_key[:3]] += 1
        cache = {"taf": {}, "phys": {}, "obs": {}}
        for block_key, members in blocks.items():
            members.sort(key=lambda r: r["at"])
            row = members[len(members) // 2]
            raw = lineage_of(conn, row)
            if raw is None:
                counts["no_lineage"] += 1
                continue
            lineage = rehydrate_lineage_documents(conn, json.loads(raw))
            intervals = json.loads(lineage.get("bucket_intervals_c_json") or "{}")
            keys = [k for k in row["m"] if row["m"][k] > 0.0 and k in intervals]
            if row["label"] not in keys or len(keys) < 3:
                counts["label_or_ladder"] += 1
                continue
            ordered = sorted(keys, key=lambda k: -math.inf if intervals[k][0] is None else float(intervals[k][0]))
            try:
                centers_all, _open = served_bucket_axis(intervals, sorted(intervals, key=lambda k: -math.inf if intervals[k][0] is None else float(intervals[k][0])))
            except ValueError:
                counts["axis"] += 1
                continue
            axis_keys = sorted(intervals, key=lambda k: -math.inf if intervals[k][0] is None else float(intervals[k][0]))
            center_of = dict(zip(axis_keys, centers_all))
            centers = [center_of[k] for k in ordered]
            upper = [intervals[k][1] for k in ordered]
            f = features_for(scope, row, lineage, ordered, centers, cache, conn, samples)
            out.append({
                "stage": scope, "station": row["station"], "date": row["date"], "at": row["at"],
                "label_known_at": row["label_known_at"],
                "weight": 1.0 / per_day[block_key[:3]],
                "keys": ordered, "hit": ordered.index(row["label"]),
                "p": [row["p"].get(k, 0.0) for k in ordered], "m": [row["m"][k] for k in ordered],
                "half_spread": [float((row.get("half_spread") or {}).get(k) or 0.0) for k in ordered],
                "centers": centers, "upper": upper, "features": f,
            })
            counts[f"kept_{scope}"] += 1
        print(scope, dict(counts), round(time.time() - started), flush=True)
    (HERE / "dataset_p0.json").write_text(json.dumps(out))
    (HERE / "dataset_p0_meta.json").write_text(json.dumps({"counts": counts, "rows": len(out), "seconds": round(time.time() - started)}, indent=1))
    print("rows", len(out), "seconds", round(time.time() - started))


if __name__ == "__main__":
    build()
