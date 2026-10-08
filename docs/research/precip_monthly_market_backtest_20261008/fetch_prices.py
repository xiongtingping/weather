"""Hourly YES price history for every NYC/Seattle monthly-precipitation bucket.

CLOB /prices-history returns nothing for resolved markets at interval=max with
hourly fidelity, so it is fetched in 7-day startTs/endTs windows from the
event's creation to the bucket's close.  The series is what Polymarket charts
(midpoint, or last trade when the spread is wide) -- not executable quotes.
"""
import json, subprocess, time, datetime as dt, gzip, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
URL = "https://clob.polymarket.com/prices-history?market={tok}&startTs={a}&endTs={b}&fidelity=60"


def ts(s):
    s = s.replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    return int(dt.datetime.fromisoformat(s).timestamp())


def get(url):
    for attempt in range(4):
        r = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True, text=True)
        try:
            return json.loads(r.stdout).get("history", [])
        except Exception:
            time.sleep(2 + attempt * 3)
    raise RuntimeError(url)


def main():
    events = json.load(open(HERE / "events.json"))
    out = {}
    for e in events:
        a0 = ts(e["start"])
        for m in e["markets"]:
            end = m["closedTime"] or dt.datetime.now(dt.timezone.utc).isoformat()
            b0 = ts(end)
            pts = {}
            a = a0
            while a < b0:
                b = min(a + 7 * 86400, b0)
                for p in get(URL.format(tok=m["yes_token"], a=a, b=b)):
                    pts[p["t"]] = p["p"]
                a = b
                time.sleep(0.15)
            out[m["yes_token"]] = sorted(pts.items())
            print(e["title"], m["label"], len(pts), file=sys.stderr)
    with gzip.open(HERE / "raw" / "prices_hourly.json.gz", "wt") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
