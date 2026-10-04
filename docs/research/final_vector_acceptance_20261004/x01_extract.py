#!/usr/bin/env python3
"""Every settled station-day's P0 decisions more than 36 h out: the served
vector, the layers its envelope kept, the book it priced against, the truth.

Read-only, short read transactions (autocommit, one fetch per statement), so no
long reader pins the SSOT WAL.  Resumable: station-days already written are
skipped.  Output: gzip JSONL, one line per decision fact.

    python3 x01_extract.py [budget_seconds]
"""
import gzip
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "decisions.jsonl.gz")
DONE = os.path.join(HERE, "decisions.done")
DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
BUDGET_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 540.0
SINCE = "2026-09-12"
MIN_HOURS = 36.0


def best(levels, reverse):
    out = []
    for lv in levels or ():
        try:
            p = float(lv.get("price"))
            s = float(lv.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s > 0 and 0 < p < 1:
            out.append(p)
    return (max(out) if reverse else min(out)) if out else None


def parsed(lineage, key):
    val = lineage.get(key)
    if isinstance(val, str):
        if val.startswith("@ref/"):
            return None
        try:
            return json.loads(val)
        except ValueError:
            return None
    return val


def main():
    start = time.time()
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA cache_size=-16000")
    labels = conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c, truth_revision_no"
        " FROM basket_station_day_settlement_label l"
        " WHERE event_date>=? AND truth_revision_no=(SELECT MAX(truth_revision_no)"
        "   FROM basket_station_day_settlement_label x"
        "   WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"
        " ORDER BY event_date, station_id",
        (SINCE,),
    ).fetchall()
    done = set()
    if os.path.exists(DONE):
        with open(DONE) as fh:
            done = {line.strip() for line in fh if line.strip()}
    todo = [r for r in labels if f"{r[0]}|{r[1]}" not in done]
    print(f"labels={len(labels)} done={len(done)} todo={len(todo)}", flush=True)
    n_facts = n_days = 0
    with gzip.open(OUT, "at", encoding="utf-8") as out, open(DONE, "a") as done_fh:
        for station, day, y, tmax, rev in todo:
            if time.time() - start > BUDGET_SECONDS:
                print("budget reached; rerun to continue", flush=True)
                break
            facts = conn.execute(
                "SELECT decision_at_utc, probability_envelope_id, ws_snapshot_id,"
                " posterior_version, settlement_timezone"
                " FROM basket_policy_training_fact"
                " WHERE station_id=? AND event_date=? AND stage='P0' ORDER BY decision_at_utc",
                (station, day),
            ).fetchall()
            lines = []
            for seq, (t, env_id, snap_id, pv, tz) in enumerate(facts):
                try:
                    hours = hours_to_settlement(day, as_of_utc=t, settlement_timezone=tz)
                except Exception:
                    hours = None
                if hours is None or hours <= MIN_HOURS:
                    continue
                env = conn.execute(
                    "SELECT probabilities_json, lineage_json, fresh_until_utc"
                    " FROM basket_probability_envelope WHERE envelope_id=?",
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
                market = market_reference_v1(buckets)
                names = [str(b.get("bucket")) for b in buckets]
                rows = []
                for b in buckets:
                    name = str(b.get("bucket"))
                    ask = best(b.get("ask_levels"), False) if b.get("ask_available", True) else None
                    rows.append([name, b.get("ordinal"), p.get(name), best(b.get("bid_levels"), True), ask])
                systems = parsed(lineage, "native_system_probabilities_json") or {}
                sys_vec = {
                    sid: [round(float(vec.get(n, 0.0)), 7) for n in names]
                    for sid, vec in systems.items() if isinstance(vec, dict)
                }
                cw = parsed(lineage, "center_width_json") or {}
                cooling = parsed(lineage, "synoptic_cooling_json") or {}
                floor = parsed(lineage, "probability_floor_json") or {}
                lines.append(json.dumps({
                    "st": station, "d": day, "t": t, "seq": seq, "h": hours, "pv": pv,
                    "y": y, "tmax": tmax, "rev": rev, "eid": env_id,
                    "b": rows,
                    "q": market.get("vector") if market.get("state") == "complete" else None,
                    "qr": market.get("reason"), "ms": market.get("mid_sum"),
                    "sys": sys_vec,
                    "w": parsed(lineage, "system_weights_json"),
                    "bridge": parsed(lineage, "consensus_bridge_json"),
                    "cw": {k: cw.get(k) for k in cw if "sha" not in k},
                    "shift_c": cooling.get("shift_c") if isinstance(cooling, dict) else None,
                    "floored": floor.get("floored_buckets") if isinstance(floor, dict) else None,
                    "mc": lineage.get("model_center_c"),
                    "lead": lineage.get("lead_bucket"),
                }, ensure_ascii=False, separators=(",", ":")))
            if lines:
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
