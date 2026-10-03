#!/usr/bin/env python3
"""Extract every settled station-day's decisions: model p, market book, truth.

Read-only, short read transactions (autocommit, fetchall per statement), so no
long reader pins the SSOT WAL.  Resumable: station-days already written are
skipped.  Output: gzip JSONL, one line per decision fact.
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

LINEAGE_KEYS = (
    "lead_bucket", "run_cycle", "model_center_c", "model_envelope_span_c",
    "ensemble_system_ids_json", "bucket_intervals_c_json", "season_bin",
    "probability_pathway",
)


def top(levels, reverse, n=3):
    out = []
    for lv in levels or ():
        try:
            p = float(lv.get("price"))
            s = float(lv.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s > 0 and 0 < p < 1:
            out.append((p, s))
    out.sort(reverse=reverse)
    return [[round(p, 4), round(s, 2)] for p, s in out[:n]]


def main():
    start = time.time()
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA cache_size=-16000")
    labels = conn.execute(
        "SELECT station_id, event_date, settled_bucket, settled_tmax_c, truth_revision_no"
        " FROM basket_station_day_settlement_label l"
        " WHERE truth_revision_no=(SELECT MAX(truth_revision_no)"
        "   FROM basket_station_day_settlement_label x"
        "   WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"
        " ORDER BY event_date, station_id"
    ).fetchall()
    done = set()
    if os.path.exists(DONE):
        with open(DONE) as fh:
            done = {line.strip() for line in fh if line.strip()}
    todo = [r for r in labels if f"{r[0]}|{r[1]}" not in done]
    print(f"labels={len(labels)} done={len(done)} todo={len(todo)}", flush=True)
    n_facts = 0
    n_days = 0
    with gzip.open(OUT, "at", encoding="utf-8") as out, open(DONE, "a") as done_fh:
        for station, day, y, tmax, rev in todo:
            if time.time() - start > BUDGET_SECONDS:
                print("budget reached; rerun to continue", flush=True)
                break
            facts = conn.execute(
                "SELECT stage, decision_at_utc, probability_envelope_id, ws_snapshot_id,"
                " posterior_version, settlement_timezone"
                " FROM basket_policy_training_fact"
                " WHERE station_id=? AND event_date=? ORDER BY decision_at_utc",
                (station, day),
            ).fetchall()
            seen_stage = set()
            lines = []
            for stage, t, env_id, snap_id, pv, tz in facts:
                env = conn.execute(
                    "SELECT probabilities_json, decision_at_utc, lineage_json, stage"
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
                    lineage = json.loads(env[2] or "{}")
                except ValueError:
                    continue
                market = market_reference_v1(buckets)
                rows = []
                for b in buckets:
                    name = str(b.get("bucket"))
                    asks = top(b.get("ask_levels"), False) if b.get("ask_available", True) else []
                    bids = top(b.get("bid_levels"), True)
                    rows.append([
                        name, b.get("ordinal"), p.get(name), bids, asks,
                        b.get("price_tick_size"),
                        1 if b.get("unmaterialized_book") else 0,
                    ])
                try:
                    hours = hours_to_settlement(day, as_of_utc=t, settlement_timezone=tz)
                except Exception:
                    hours = None
                lin = {}
                for key in LINEAGE_KEYS:
                    val = lineage.get(key)
                    if isinstance(val, str) and key.endswith("_json"):
                        try:
                            val = json.loads(val)
                        except ValueError:
                            pass
                    lin[key] = val
                lines.append(json.dumps({
                    "st": station, "d": day, "tz": tz, "stage": stage, "t": t,
                    "te": env[1], "tq": snap[1], "pv": pv, "h": hours,
                    "first": 0 if stage in seen_stage else 1,
                    "y": y, "tmax": tmax, "rev": rev,
                    "b": rows,
                    "q": market.get("vector") if market.get("state") == "complete" else None,
                    "qr": market.get("reason"), "ms": market.get("mid_sum"),
                    "lin": lin,
                }, ensure_ascii=False, separators=(",", ":")))
                seen_stage.add(stage)
            if lines:
                out.write("\n".join(lines) + "\n")
            done_fh.write(f"{station}|{day}\n")
            done_fh.flush()
            n_facts += len(lines)
            n_days += 1
            if n_days % 50 == 0:
                print(f"  {n_days} station-days, {n_facts} facts, {time.time()-start:.0f}s, at {day}", flush=True)
    print(f"wrote {n_days} station-days, {n_facts} facts in {time.time()-start:.0f}s", flush=True)
    remaining = len(todo) - n_days
    print(f"remaining={remaining}", flush=True)


if __name__ == "__main__":
    main()
