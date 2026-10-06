#!/usr/bin/env python3
"""The book each P0 >36h decision in rows/ priced against, trimmed to the top
three levels a side (all the lanes' rule functions read), keyed by snapshot id.
Read-only, autocommit, resumable.  Output: books.jsonl.gz.

    python3 veto_books.py [budget_seconds]
"""
import glob
import gzip
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("CA_BOOKS_FILE") or os.path.join(HERE, "books.jsonl.gz")
ROWS = os.environ.get("CA_ROWS_DIR") or os.path.join(HERE, "rows")
DB = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
BUDGET = float(sys.argv[1]) if len(sys.argv) > 1 else 480.0
KEEP = ("bucket", "ordinal", "ask_available", "bid_available", "bid_price", "price_tick_size",
        "min_order_size", "book_observed_at", "unmaterialized_book", "token_id")


def trim(levels, reverse):
    out = []
    for lv in levels or ():
        try:
            p, s = float(lv.get("price")), float(lv.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s > 0 and 0 < p < 1:
            out.append({"price": p, "shares": s})
    out.sort(key=lambda x: x["price"], reverse=reverse)
    return out[:3]


def main():
    start = time.time()
    done = set()
    if os.path.exists(OUT):
        with gzip.open(OUT, "rt") as fh:
            done = {json.loads(l)["sid"] for l in fh}
    want = []
    for f in sorted(glob.glob(os.path.join(ROWS, "*.jsonl.gz"))):
        with gzip.open(f, "rt") as fh:
            for l in fh:
                r = json.loads(l)
                if r["stage"] == "P0" and (r["h"] or 0) > 36 and r["sid"] not in done:
                    want.append(r["sid"])
    want = sorted(set(want))
    print(f"done={len(done)} todo={len(want)}", flush=True)
    conn = sqlite3.connect(DB, uri=True, isolation_level=None, timeout=30)
    conn.execute("PRAGMA query_only=1")
    n = 0
    with gzip.open(OUT, "at", encoding="utf-8") as out:
        for sid in want:
            if time.time() - start > BUDGET:
                print("budget reached; rerun", flush=True)
                break
            row = conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?", (sid,)).fetchone()
            if row is None:
                continue
            buckets = []
            for b in json.loads(row[0]):
                x = {k: b.get(k) for k in KEEP if k in b}
                x["ask_levels"] = trim(b.get("ask_levels"), False)
                x["bid_levels"] = trim(b.get("bid_levels"), True)
                buckets.append(x)
            out.write(json.dumps({"sid": sid, "buckets": buckets}, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    print(f"wrote {n} in {time.time()-start:.0f}s", flush=True)


if __name__ == "__main__":
    main()
