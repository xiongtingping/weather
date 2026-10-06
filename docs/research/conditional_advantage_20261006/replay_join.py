#!/usr/bin/env python3
"""PROTOCOL_V2 tier 丙2: the current-chain approximation replay joined to the
book standing at each replay instant.

Source: ``downstream_after_247_20261002/pass1_down`` (``base`` arm, charter 247
form, 1 degC grid, C stations).  The charter 253 ceiling is applied after the
fact with production ``apply_peak_ceiling`` past 36 h.  Each replay update is
joined to the newest COMMITTED snapshot at or before it, at most 60 minutes
old.  Rows are written in the shape of ``rows/`` so ``analysis.featurise``
reads them.  Read-only, autocommit, resumable per station.

    python3 replay_join.py [budget_seconds]
"""
import bisect
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    FAR_PEAK_CEILING, FAR_PEAK_MIN_HOURS_TO_SETTLEMENT, apply_peak_ceiling,
)

SRC = os.path.join(os.path.dirname(HERE), "downstream_after_247_20261002", "pass1_down")
OUT = os.path.join(HERE, "rows_replay")
DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
BUDGET = float(sys.argv[1]) if len(sys.argv) > 1 else 540.0
MAX_BOOK_AGE_S = 3600.0


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def ladder_value(name, vec):
    m = re.match(r"^(-?\d+)°C$", name)
    if m:
        return vec.get(int(m.group(1)), 0.0), float(m.group(1))
    m = re.match(r"^(-?\d+)°C or below$", name)
    if m:
        return sum(v for k, v in vec.items() if k <= int(m.group(1))), float(m.group(1))
    m = re.match(r"^(-?\d+)°C or higher$", name)
    if m:
        return sum(v for k, v in vec.items() if k >= int(m.group(1))), float(m.group(1))
    return None, None


def best(levels, high):
    vals = []
    for lv in levels or ():
        try:
            p, s = float(lv.get("price")), float(lv.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s > 0 and 0 < p < 1:
            vals.append((p, s))
    if not vals:
        return None
    return max(vals) if high else min(vals)


def main():
    start = time.time()
    os.makedirs(OUT, exist_ok=True)
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    conn.execute("PRAGMA query_only=1")
    labels = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x"
        " WHERE x.station_id=l.station_id AND x.event_date=l.event_date)")}
    stations = sorted(f.split(".")[0] for f in os.listdir(SRC) if f.endswith(".jsonl.gz"))
    stats = {"records": 0, "joined": 0, "no_book": 0, "stale_book": 0, "not_c": 0, "no_label": 0, "incomplete": 0}
    for st in stations:
        out_path = os.path.join(OUT, f"{st}.jsonl.gz")
        if os.path.exists(out_path):
            continue
        if time.time() - start > BUDGET:
            print("budget reached; rerun", flush=True)
            break
        by_day = {}
        with gzip.open(os.path.join(SRC, f"{st}.jsonl.gz"), "rt") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("base") and r["base"].get("vec"):
                    by_day.setdefault(r["event_date"], []).append(r)
        lines = []
        for day, recs in sorted(by_day.items()):
            y = labels.get((st, day))
            if y is None:
                stats["no_label"] += len(recs)
                continue
            snaps = conn.execute(
                "SELECT observed_at, snapshot_id FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY observed_at",
                (st, day)).fetchall()
            times = [ts(s[0]) for s in snaps]
            cache = {}
            recs.sort(key=lambda r: r["at"])
            for seq, r in enumerate(recs):
                stats["records"] += 1
                at = ts(r["at"])
                i = bisect.bisect_right(times, at) - 1
                if i < 0:
                    stats["no_book"] += 1
                    continue
                if (at - times[i]).total_seconds() > MAX_BOOK_AGE_S:
                    stats["stale_book"] += 1
                    continue
                sid = snaps[i][1]
                if sid not in cache:
                    buckets = json.loads(conn.execute(
                        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?", (sid,)).fetchone()[0])
                    cache[sid] = sorted(buckets, key=lambda b: int(b.get("ordinal") or 0))
                buckets = cache[sid]
                names = [str(b.get("bucket")) for b in buckets]
                if not all("°C" in n for n in names):
                    stats["not_c"] += 1
                    continue
                market = market_reference_v1(buckets)
                hours = r["base"].get("hours_to_settlement")
                vec = {int(k): float(v) for k, v in r["base"]["vec"].items()}
                if hours is not None and hours > FAR_PEAK_MIN_HOURS_TO_SETTLEMENT:
                    keys = [str(k) for k in sorted(vec)]
                    capped, _ = apply_peak_ceiling({str(k): v for k, v in vec.items()},
                                                   ceiling=FAR_PEAK_CEILING, ordered_keys=keys)
                    vec = {int(k): float(v) for k, v in capped.items()}
                vals, temps = [], []
                for n in names:
                    v, t = ladder_value(n, vec)
                    if v is None:
                        break
                    vals.append(v)
                    temps.append(t)
                if len(vals) != len(names) or sum(vals) < 0.98 or y not in names:
                    stats["incomplete"] += 1
                    continue
                book = []
                for b in buckets:
                    bid = best(b.get("bid_levels"), True)
                    ask = best(b.get("ask_levels"), False) if b.get("ask_available", True) else None
                    book.append([bid[0] if bid else None, ask[0] if ask else None,
                                 bid[1] if bid else None, ask[1] if ask else None])
                lines.append(json.dumps({
                    "st": st, "d": day, "tz": r.get("tz"), "stage": "P0", "t": r["at"], "seq": seq, "h": hours,
                    "pv": "replay_247_plus_253", "eid": None, "sid": sid, "y": y, "tmax": r.get("actual"), "rev": None,
                    "names": names, "temps": temps, "rung": 1.0, "p": vals, "book": book, "snap_at": snaps[i][0],
                    "book_at": None,
                    "q": [market["vector"].get(n) for n in names] if market.get("state") == "complete" else None,
                    "qr": market.get("reason"), "ms": market.get("mid_sum"), "maxsp": market.get("max_spread"),
                    "mc": r["base"].get("mean"), "lead": r.get("lead"), "season": None,
                    "centres": None, "w": r.get("w"), "run_init": None, "arrival": None,
                    "systems": r.get("pool"), "sys_stats": None, "bridge": None,
                    "cooling": {"shift_sigma": r["base"].get("cooling_shift_sigma")}, "floored": None,
                    "seated": r["base"].get("seated"),
                }, ensure_ascii=False, separators=(",", ":")))
                stats["joined"] += 1
        with gzip.open(out_path, "wt", encoding="utf-8") as out:
            out.write("\n".join(lines) + ("\n" if lines else ""))
        print(f"  {st}: {len(lines)} rows, {time.time()-start:.0f}s", flush=True)
    print(json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
