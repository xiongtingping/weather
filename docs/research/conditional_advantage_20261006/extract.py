#!/usr/bin/env python3
"""Every settled station-day's decisions, every stage: the served vector, the
book it priced against, the envelope's own evidence structure, the truth.

Protocol section 1 and 6.  Read-only, autocommit, one fetch per statement, so
no long reader pins the SSOT WAL.  Resumable: station-days already written are
skipped.  Output: rows/<station>.jsonl.gz, one line per decision fact.

    python3 extract.py [budget_seconds] [since] [until]      # CA_ROWS_DIR picks the output folder
"""
import gzip
import json
import math
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROWS = os.environ.get("CA_ROWS_DIR") or os.path.join(HERE, "rows")
DONE = ROWS.rstrip("/") + ".done"
DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
BUDGET_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 480.0
SINCE = sys.argv[2] if len(sys.argv) > 2 else "2026-09-12"
UNTIL = sys.argv[3] if len(sys.argv) > 3 else "9999-12-31"


def levels(raw):
    out = []
    for lv in raw or ():
        try:
            p = float(lv.get("price"))
            s = float(lv.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s > 0 and 0 < p < 1:
            out.append((p, s))
    return out


def parsed(lineage, key):
    val = lineage.get(key)
    if isinstance(val, str):
        if val.startswith("@ref/") or val in ("NONE", "null"):
            return None
        try:
            return json.loads(val)
        except ValueError:
            return None
    return val


def scalar(lineage, key):
    val = lineage.get(key)
    if val in (None, "NONE", "null", ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return val


def compact(d, drop=("sha", "artifact", "_id", "fingerprint")):
    if not isinstance(d, dict):
        return None
    return {k: v for k, v in d.items() if not any(s in k for s in drop) and not isinstance(v, (dict, list))}


def midpoints(names, intervals, rung):
    temps = []
    for n in names:
        iv = intervals.get(n) if isinstance(intervals, dict) else None
        lo, hi = (iv if isinstance(iv, list) and len(iv) == 2 else (None, None))
        if lo is None and hi is None:
            temps.append(None)
        elif lo is None:
            temps.append(float(hi) - rung / 2.0)
        elif hi is None:
            temps.append(float(lo) + rung / 2.0)
        else:
            temps.append((float(lo) + float(hi)) / 2.0)
    return temps


def moments(vec, temps):
    pairs = [(float(v), t) for v, t in zip(vec, temps) if t is not None and v is not None]
    tot = sum(v for v, _ in pairs)
    if tot <= 0:
        return None, None
    mean = sum(v * t for v, t in pairs) / tot
    var = sum(v * (t - mean) ** 2 for v, t in pairs) / tot
    return mean, math.sqrt(max(var, 0.0))


def main():
    start = time.time()
    os.makedirs(ROWS, exist_ok=True)
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA cache_size=-16000")
    labels = conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c, truth_revision_no"
        " FROM basket_station_day_settlement_label l"
        " WHERE event_date>=? AND event_date<=? AND truth_revision_no=(SELECT MAX(truth_revision_no)"
        "   FROM basket_station_day_settlement_label x"
        "   WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"
        " ORDER BY event_date, station_id",
        (SINCE, UNTIL),
    ).fetchall()
    done = set()
    if os.path.exists(DONE):
        with open(DONE) as fh:
            done = {line.strip() for line in fh if line.strip()}
    todo = [r for r in labels if f"{r[0]}|{r[1]}" not in done]
    print(f"labels={len(labels)} done={len(done)} todo={len(todo)}", flush=True)
    n_facts = n_days = 0
    with open(DONE, "a") as done_fh:
        for station, day, y, tmax, rev in todo:
            if time.time() - start > BUDGET_SECONDS:
                print("budget reached; rerun to continue", flush=True)
                break
            facts = conn.execute(
                "SELECT stage, decision_at_utc, probability_envelope_id, ws_snapshot_id,"
                " posterior_version, settlement_timezone"
                " FROM basket_policy_training_fact"
                " WHERE station_id=? AND event_date=? ORDER BY stage, decision_at_utc",
                (station, day),
            ).fetchall()
            lines = []
            seq_by_stage = {}
            for stage, t, env_id, snap_id, pv, tz in facts:
                try:
                    hours = hours_to_settlement(day, as_of_utc=t, settlement_timezone=tz)
                except Exception:
                    hours = None
                env = conn.execute(
                    "SELECT probabilities_json, lineage_json FROM basket_probability_envelope"
                    " WHERE envelope_id=?",
                    (env_id,),
                ).fetchone()
                snap = conn.execute(
                    "SELECT buckets_json, observed_at FROM basket_official_ws_snapshot"
                    " WHERE snapshot_id=?",
                    (snap_id,),
                ).fetchone()
                if env is None or snap is None:
                    continue
                try:
                    p = json.loads(env[0])
                    buckets = json.loads(snap[0])
                    lineage = json.loads(env[1] or "{}")
                except ValueError:
                    continue
                buckets = sorted(buckets, key=lambda b: int(b.get("ordinal") or 0))
                names = [str(b.get("bucket")) for b in buckets]
                market = market_reference_v1(buckets)
                book = []
                stamps = []
                for b in buckets:
                    asks = levels(b.get("ask_levels")) if b.get("ask_available", True) else []
                    bids = levels(b.get("bid_levels"))
                    ask = min(asks, default=None)
                    bid = max(bids, default=None)
                    book.append([
                        round(bid[0], 4) if bid else None, round(ask[0], 4) if ask else None,
                        round(bid[1], 1) if bid else None, round(ask[1], 1) if ask else None,
                    ])
                    bo = b.get("book_observed_at")
                    if isinstance(bo, str) and bo:
                        stamps.append(bo)
                cw = parsed(lineage, "center_width_json") or {}
                rung = float(cw.get("bucket_rung_c") or 1.0)
                temps = midpoints(names, parsed(lineage, "bucket_intervals_c_json") or {}, rung)
                systems = parsed(lineage, "native_system_probabilities_json") or {}
                sys_stats = {}
                for sid, vec in systems.items():
                    if isinstance(vec, dict):
                        m, s = moments([vec.get(n, 0.0) for n in names], temps)
                        if m is not None:
                            sys_stats[sid] = [round(m, 4), round(s, 4)]
                cooling = parsed(lineage, "synoptic_cooling_json") or {}
                bridge = parsed(lineage, "consensus_bridge_json") or {}
                floor = parsed(lineage, "probability_floor_json") or {}
                seq = seq_by_stage.get(stage, 0)
                seq_by_stage[stage] = seq + 1
                row = {
                    "st": station, "d": day, "tz": tz, "stage": stage, "t": t, "seq": seq, "h": hours,
                    "pv": pv, "eid": env_id, "sid": snap_id, "y": y, "tmax": tmax, "rev": rev,
                    "names": names, "temps": [round(x, 3) if x is not None else None for x in temps],
                    "rung": rung, "p": [p.get(n) for n in names],
                    "book": book, "snap_at": snap[1],
                    "book_at": [min(stamps), max(stamps)] if len(stamps) == len(buckets) else None,
                    "q": [market["vector"].get(n) for n in names] if market.get("state") == "complete" else None,
                    "qr": market.get("reason"), "ms": market.get("mid_sum"), "maxsp": market.get("max_spread"),
                    "mc": scalar(lineage, "model_center_c"), "lead": lineage.get("lead_bucket"),
                    "season": lineage.get("season_bin"),
                    "centres": parsed(lineage, "native_calibrated_system_centers_c_json"),
                    "w": parsed(lineage, "system_weights_json"),
                    "run_init": parsed(lineage, "run_init_utc_by_system_json"),
                    "arrival": parsed(lineage, "archive_query_at_utc_by_system_json"),
                    "systems": parsed(lineage, "ensemble_system_ids_json"),
                    "sys_stats": sys_stats or None,
                    "bridge": compact(bridge), "cooling": compact(cooling),
                    "floored": floor.get("floored_buckets") if isinstance(floor, dict) else None,
                }
                if stage == "P0-M":
                    row["taf"] = {
                        "tx_c": scalar(lineage, "taf_observed_tx_c"),
                        "mode": lineage.get("taf_likelihood_mode"),
                        "rev": lineage.get("taf_revision_state"),
                        "like": compact(parsed(lineage, "p0m_taf_likelihood_json")),
                        "running_high_c": scalar(lineage, "observed_running_high_c"),
                    }
                if stage == "P1":
                    row["obs"] = {
                        "running_high_c": scalar(lineage, "metar_trajectory_running_high_c"),
                        "like": compact(parsed(lineage, "p1_observation_likelihood_json")),
                        "committed_at": lineage.get("canonical_observation_committed_at"),
                        "excluded": lineage.get("observed_hard_excluded_buckets"),
                    }
                lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            if lines:
                with gzip.open(os.path.join(ROWS, f"{station}.jsonl.gz"), "at", encoding="utf-8") as out:
                    out.write("\n".join(lines) + "\n")
            done_fh.write(f"{station}|{day}\n")
            done_fh.flush()
            n_facts += len(lines)
            n_days += 1
            if n_days % 100 == 0:
                print(f"  {n_days} station-days, {n_facts} facts, {time.time()-start:.0f}s, at {day}", flush=True)
    print(f"wrote {n_days} station-days, {n_facts} facts in {time.time()-start:.0f}s", flush=True)
    print(f"remaining={len(todo) - n_days}", flush=True)


if __name__ == "__main__":
    main()
