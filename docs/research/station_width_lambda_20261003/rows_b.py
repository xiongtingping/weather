#!/usr/bin/env python3
"""pass1_b -> rows_b/: the representative decisions of data B, in data A's
shape (``station_model_weights_20261001/score.py``'s rule: per station-day and
horizon bin -- hours to the local day's end -- the bin's last update)."""
import gzip
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = (0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.1, 1.2, 1.35, 1.5)
BINS = ((48, 1e9), (36, 48), (24, 36), (12, 24), (0, 12))


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main():
    os.makedirs(os.path.join(HERE, "rows_b"), exist_ok=True)
    states = Counter()
    for name in sorted(os.listdir(os.path.join(HERE, "pass1_b"))):
        recs = [json.loads(line) for line in gzip.open(os.path.join(HERE, "pass1_b", name), "rt")]
        by_day = defaultdict(list)
        for r in recs:
            by_day[r["event_date"]].append(r)
        out = []
        for day, rs in sorted(by_day.items()):
            end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(rs[0]["tz"])) + timedelta(days=1)
            for lo, hi in BINS:
                inbin = [r for r in rs if lo <= (end - utc(r["at"])).total_seconds() / 3600 < hi]
                if not inbin:
                    continue
                r = max(inbin, key=lambda x: x["at"])
                b = r["base"]
                states[b["fusion_state"]] += 1
                out.append({
                    "station": r["station"], "day": day, "known": utc(r["truth_known_at"]).isoformat(), "bin": lo,
                    "nf": b["nf"], "seated": b["seated"],
                    "s": {str(lam): [t[1], t[2], t[0]] for lam, t in zip(GRID, b["lam"])},
                })
        with open(os.path.join(HERE, "rows_b", name.replace(".jsonl.gz", ".json")), "w") as handle:
            json.dump(out, handle)
    print("fusion states", dict(states))


if __name__ == "__main__":
    main()
