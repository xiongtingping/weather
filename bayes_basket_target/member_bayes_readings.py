"""The member-trajectory Bayes estimate of today's high, for the station board.

User 2026-10-16 「在站点看板里：前端增加贝叶斯更新的温度预估显示」.  The update
pre-registered in docs/research/member_trajectory_bayes_update_20261016:
each seated native system's members (``member_trajectory_store``, the newest
run in hand), shifted by the system's P0 bias, are weighted by how well their
step series match today's reports of the last six hours,
w_m ~ exp(-sum (report - member)^2 / (2 tau^2)); each member's high is the
running high or its own maximum over the steps still to come; the weighted
and the unweighted member highs, dressed on the ladder with a half-rung
kernel, give the posterior and the prior, pooled with the root P0's system
weights; their ratio is the members' likelihood, which times the served
belief is "P1 x members".

Display only, unverified: tau is fixed at the protocol grid's middle and no
layer reads it -- the protocol tests it once 30 event days are in hand.
Computed with the D0 card's sweep every ten minutes (a reader: the SSOT, the
archive and the trajectory store read-only; writes files under data/ only).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import station_day_board as board
from .member_trajectory_store import VALUE_KIND_PERIOD_MAX, store_path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "board_readings"
LATEST_PATH = OUT_DIR / "member_bayes_latest.json"
HISTORY_DIR = OUT_DIR / "member_bayes_history"
TAU_C = 1.5
MISFIT_WINDOW_HOURS = 6.0
KERNEL_RUNGS = 0.5
RATIO_FLOOR = 1e-3


def _t(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def dress(highs: Sequence[float], weights: Sequence[float], axis: Sequence[Mapping[str, Any]],
          sigma_c: float, floor_bucket: str | None) -> dict[str, float]:
    """Weighted member highs as a ladder vector: a Gaussian kernel of sd
    ``sigma_c`` integrated over each rung; rungs below the running high's
    rung get nothing."""

    order = [a["bucket"] for a in axis]
    start = order.index(floor_bucket) if floor_bucket in order else 0
    out = {b: 0.0 for b in order}
    total = sum(weights)
    for h, w in zip(highs, weights):
        if w <= 0.0:
            continue
        mass = []
        for a in axis[start:]:
            lo = -math.inf if a["lo_c"] is None or a["bucket"] == order[start] else (a["lo_c"] - h) / sigma_c
            hi = math.inf if a["hi_c"] is None else (a["hi_c"] - h) / sigma_c
            mass.append(max(0.0, (_phi(hi) if hi != math.inf else 1.0) - (_phi(lo) if lo != -math.inf else 0.0)))
        s = sum(mass) or 1.0
        for a, m in zip(axis[start:], mass):
            out[a["bucket"]] += (w / total) * m / s
    return out


def summary(vec: Mapping[str, float], axis: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = [a["bucket"] for a in axis]
    total = sum(vec.get(k, 0.0) for k in keys) or 1.0
    p = [vec.get(k, 0.0) / total for k in keys]
    centres = [a["centre_c"] for a in axis]
    mean = sum(pi * c for pi, c in zip(p, centres) if c is not None)
    m = max(range(len(p)), key=lambda i: (p[i], -i))
    cum, lo, hi = 0.0, None, None
    for k, pi in zip(keys, p):
        cum += pi
        if lo is None and cum >= 0.10:
            lo = k
        if hi is None and cum >= 0.90:
            hi = k
    return {"mean_c": round(mean, 2), "mode": keys[m], "mode_p": round(p[m], 4), "interval80": [lo, hi],
            "vector": {k: round(v, 4) for k, v in zip(keys, p)}}


def _member_value_at(times: Sequence[datetime], values: Sequence[float | None], at: datetime) -> float | None:
    for i in range(1, len(times)):
        if times[i - 1] <= at <= times[i] and values[i - 1] is not None and values[i] is not None:
            span = (times[i] - times[i - 1]).total_seconds()
            share = 0.0 if span <= 0 else (at - times[i - 1]).total_seconds() / span
            return values[i - 1] + share * (values[i] - values[i - 1])
    return None


def system_update(row: Mapping[str, Any], bias_c: float, reports: Sequence[tuple[datetime, float]],
                  now: datetime, running_high: float | None) -> dict[str, Any]:
    """One system: member weights from the recent reports, each member's high."""

    times = [_t(x) for x in json.loads(row["valid_times_json"])]
    members = {m: [None if v is None else float(v) + bias_c for v in vals]
               for m, vals in json.loads(row["members_json"]).items()}
    recent = [(t, v) for t, v in reports if now - timedelta(hours=MISFIT_WINDOW_HOURS) < t <= now]
    period = row["value_kind"] == VALUE_KIND_PERIOD_MAX
    misfit, used = {}, 0
    for m, vals in members.items():
        d, n = 0.0, 0
        if period:
            for i, end in enumerate(times):
                start = times[i - 1] if i else end - timedelta(hours=3)
                if end > now or vals[i] is None:
                    continue
                inside = [v for t, v in recent if start < t <= end]
                if inside:
                    d += (max(inside) - vals[i]) ** 2; n += 1
        else:
            for t, v in recent:
                x = _member_value_at(times, vals, t)
                if x is not None:
                    d += (v - x) ** 2; n += 1
        misfit[m] = d; used = max(used, n)
    best = min(misfit.values()) if misfit else 0.0
    weights = {m: math.exp(-(d - best) / (2.0 * TAU_C ** 2)) for m, d in misfit.items()}
    highs = {}
    for m, vals in members.items():
        future = [v for t, v in zip(times, vals) if t > now and v is not None]
        top = max(future) if future else None
        cands = [x for x in (top, running_high) if x is not None]
        highs[m] = max(cands) if cands else None
    keep = [m for m in members if highs[m] is not None]
    w = [weights[m] for m in keep]
    s = sum(w) or 1.0
    ess = 1.0 / sum((x / s) ** 2 for x in w) if w else 0.0
    return {"members": len(keep), "highs": [highs[m] for m in keep], "weights": w, "ess": round(ess, 1),
            "reports_compared": used,
            "best_rmse_c": round(math.sqrt(best / used), 2) if used else None}


def compute(conn, traj: sqlite3.Connection | None, station: str, zone: ZoneInfo, day: str,
            now: datetime) -> dict[str, Any]:
    heads = {s: board._envelope_head(conn, station, day, s) for s in board.STAGES}
    newest = max((h for h in heads.values() if h), key=lambda h: str(h["decided_at_utc"]), default=None)
    if newest is None:
        return {"state": "no_ladder"}
    axis = board.ladder_axis(newest["ordinals"], board._json(newest["lineage"].get("bucket_intervals_c_json"), {}))
    if traj is None:
        return {"state": "no_trajectories"}
    stamp = board._iso(now)
    rows = {}
    for r in traj.execute(
            "SELECT * FROM native_member_trajectory WHERE station_id=? AND event_date=? AND archive_query_at_utc<=?"
            " ORDER BY run_init_utc DESC", (station, day, stamp)):
        rows.setdefault(r["ensemble_system_id"], r)
    if not rows:
        return {"state": "no_trajectories"}
    metar = board._metar(station, day, zone)
    reports = sorted((_t(m["valid_at_utc"]), float(m["temp_c"])) for m in metar
                     if m.get("temp_c") is not None and m.get("valid_at_utc") and _t(m["valid_at_utc"]) <= now)
    running = max((v for _t0, v in reports), default=None)
    floor = board.bucket_of(axis, running) if running is not None else None
    widths = [a["hi_c"] - a["lo_c"] for a in axis if a["lo_c"] is not None and a["hi_c"] is not None]
    sigma = KERNEL_RUNGS * (sorted(widths)[len(widths) // 2] if widths else 1.0)
    p0 = heads.get("P0")
    lin = p0["lineage"] if p0 else {}
    sys_w = board._json(lin.get("system_weights_json"), {}) or {}
    centres = board._json(lin.get("native_calibrated_system_centers_c_json"), {}) or {}
    p0_runs = board._json(lin.get("run_init_utc_by_system_json"), {}) or {}
    systems, prior_parts, post_parts, omegas = [], [], [], []
    for system, row in sorted(rows.items()):
        vals = json.loads(row["members_json"]).values()
        raw_highs = [max(v for v in vals_m if v is not None) for vals_m in vals if any(v is not None for v in vals_m)]
        same_run = str(p0_runs.get(system) or "") == str(row["run_init_utc"])
        bias = (float(centres[system]) - sum(raw_highs) / len(raw_highs)) if same_run and system in centres and raw_highs else 0.0
        up = system_update(row, bias, reports, now, running)
        if not up["members"]:
            continue
        omega = float(sys_w.get(system, 0.0))
        prior_parts.append(dress(up["highs"], [1.0] * up["members"], axis, sigma, floor))
        post_parts.append(dress(up["highs"], up["weights"], axis, sigma, floor))
        omegas.append(omega)
        systems.append({"system": system, "run_init_utc": row["run_init_utc"], "members": up["members"],
                        "ess": up["ess"], "bias_c": round(bias, 2), "bias_from_p0": same_run and system in centres,
                        "weight": omega, "reports_compared": up["reports_compared"], "best_rmse_c": up["best_rmse_c"],
                        "value_kind": row["value_kind"],
                        "prior_mean_c": round(sum(up["highs"]) / len(up["highs"]), 2),
                        "post_mean_c": round(sum(h * w for h, w in zip(up["highs"], up["weights"])) / (sum(up["weights"]) or 1.0), 2)})
    if not systems:
        return {"state": "no_trajectories"}
    if sum(omegas) <= 0.0:
        omegas = [1.0] * len(systems)
    tot = sum(omegas)
    keys = [a["bucket"] for a in axis]
    prior = {k: sum(o / tot * part[k] for o, part in zip(omegas, prior_parts)) for k in keys}
    post = {k: sum(o / tot * part[k] for o, part in zip(omegas, post_parts)) for k in keys}
    for s, o in zip(systems, omegas):
        s["pool_weight"] = round(o / tot, 3)
    stage = board.day_word({s: h for s, h in heads.items() if h})
    belief = heads.get(stage)["vector"] if stage else None
    combined = None
    if belief:
        raw = {k: float(belief.get(k, 0.0)) * max(post[k], RATIO_FLOOR) / max(prior[k], RATIO_FLOOR) for k in keys}
        s = sum(raw.values()) or 1.0
        combined = {k: v / s for k, v in raw.items()}
    return {
        "state": "ok", "as_of_utc": stamp, "tau_c": TAU_C, "kernel_sd_c": round(sigma, 3),
        "running_high_c": running, "reports_in_window": sum(1 for t, _v in reports if now - timedelta(hours=MISFIT_WINDOW_HOURS) < t),
        "latest_report_utc": board._iso(reports[-1][0]) if reports else None,
        "systems": systems, "prior": summary(prior, axis), "posterior": summary(post, axis),
        "belief_stage": stage, "belief": summary(belief, axis) if belief else None,
        "belief_x_members": summary(combined, axis) if combined else None,
    }


def run(*, db: Path = board.DEFAULT_DB, now: datetime | None = None) -> dict[str, Any]:
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    path = store_path()
    traj = None
    if path is not None and path.exists():
        traj = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        traj.row_factory = sqlite3.Row
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    readings: dict[str, Any] = {}
    try:
        conn.execute("PRAGMA query_only=1")
        for entry in board.fleet(conn, now):
            station = entry["station_id"]
            try:
                zone = ZoneInfo(board.station_timezone(conn, station))
                today = now.astimezone(zone).date()
            except Exception as exc:  # noqa: BLE001
                readings[station] = {"_error": {"state": "error", "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}}
                continue
            readings[station] = {}
            # Today (reports in hand) and tomorrow (the prior only, until its reports come).
            for day in (today.isoformat(), (today + timedelta(days=1)).isoformat()):
                try:
                    readings[station][day] = {"event_date": day, **compute(conn, traj, station, zone, day, now)}
                except Exception as exc:  # noqa: BLE001 -- one station-day's failure is its own row
                    readings[station][day] = {"event_date": day, "state": "error",
                                              "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        conn.close()
        if traj is not None:
            traj.close()
    _write(readings, now)
    return {"state": "ok", "stations": len(readings),
            "ok": sum(1 for days in readings.values() for r in days.values() if r.get("state") == "ok"),
            "seconds": round(time.monotonic() - started, 1)}


def _write(readings: Mapping[str, Any], now: datetime) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("readings") or {}
    except (OSError, ValueError):
        previous = {}
    for station, days in readings.items():
        for day, r in days.items():
            if r.get("state") != "ok":
                continue
            old = (previous.get(station) or {}).get(day) or {}
            if (old.get("event_date") == r["event_date"] and old.get("posterior") == r["posterior"]
                    and old.get("belief_x_members") == r["belief_x_members"]):
                continue
            line = {"station": station, "event_date": r["event_date"], "as_of_utc": r["as_of_utc"],
                    "post_mean_c": r["posterior"]["mean_c"], "prior_mean_c": r["prior"]["mean_c"],
                    "post_mode": r["posterior"]["mode"], "running_high_c": r["running_high_c"],
                    "bx_mode": (r["belief_x_members"] or {}).get("mode"), "bx_mean_c": (r["belief_x_members"] or {}).get("mean_c")}
            with (HISTORY_DIR / f"{r['event_date']}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    tmp = LATEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": "member_bayes_readings_v1", "computed_at_utc": board._iso(now),
                               "readings": dict(readings)}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LATEST_PATH)


def stored(station: str, day: str) -> dict[str, Any] | None:
    """What the board shows: the newest stored reading and its history."""

    try:
        doc = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "not_computed"}
    r = ((doc.get("readings") or {}).get(station) or {}).get(day)
    if not r:
        return {"state": "not_computed"}
    history = []
    try:
        with (HISTORY_DIR / f"{day}.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                if f'"station": "{station}"' in line:
                    x = json.loads(line)
                    history.append({k: x.get(k) for k in ("as_of_utc", "post_mean_c", "prior_mean_c", "post_mode",
                                                          "running_high_c", "bx_mode", "bx_mean_c")})
    except OSError:
        pass
    return {**r, "computed_at_utc": doc.get("computed_at_utc"), "history": history}


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
