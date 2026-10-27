#!/usr/bin/env python3
"""PROTOCOL.md section 1: per station-day, the first current-chain P0 checkpoint
of each family (far >36h, near 12-36h) with a complete official WS book within
300 s of the decision (as-of snapshot, PROTOCOL 补记一).  Writes samples.jsonl; reads the SSOT read-only, one
short connection per event day."""
import json, os, sqlite3, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "scripts"))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
from bayes_basket_target.station_season import station_season_bin  # noqa: E402
import live_book  # noqa: E402

SSOT = f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro"
REPLAY = f"file:{ROOT}/data/runtime/bayes_basket_target_nwp/p0_ngr_root_replay.db?mode=ro"
FIRST, LAST = "2026-09-29", "2026-10-26"
MAX_BOOK_AGE = 300.0


def levels(raw):
    out = []
    for lv in raw or ():
        try:
            p, s = float(lv.get("price")), float(lv.get("shares") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s > 0 and 0 < p < 1:
            out.append((p, s))
    return out


def ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def family(h):
    if h is None:
        return None
    return "far" if h > 36 else ("near" if h > 12 else None)


def main():
    rep = sqlite3.connect(REPLAY, uri=True)
    days = [r[0] for r in rep.execute(
        "SELECT DISTINCT event_date FROM root WHERE event_date BETWEEN ? AND ? ORDER BY 1", (FIRST, LAST))]
    out = open(os.path.join(HERE, "samples.jsonl"), "w")
    stats = {"station_days": 0, "no_truth": 0, "written": 0, "checkpoints_read": 0}
    for day in days:
        rows = rep.execute(
            "SELECT checkpoint_id, station_id, decision_at_utc, probabilities_json FROM root"
            " WHERE event_date=? ORDER BY station_id, decision_at_utc", (day,)).fetchall()
        by_station = {}
        for r in rows:
            by_station.setdefault(r[1], []).append(r)
        conn = sqlite3.connect(SSOT, uri=True, timeout=30)
        conn.execute("PRAGMA query_only=1")
        try:
            truth = live_book.settled_buckets(conn, {(s, day) for s in by_station})
            for station, cks in by_station.items():
                stats["station_days"] += 1
                if (station, day) not in truth:
                    stats["no_truth"] += 1
                    continue
                y = truth[(station, day)][0]
                season = station_season_bin(station, day)
                found = set()
                for ck, _, dec, pj in cks:
                    if len(found) == 2:
                        break
                    stats["checkpoints_read"] += 1
                    snap = conn.execute(
                        "SELECT buckets_json, observed_at, settlement_timezone FROM basket_official_ws_snapshot"
                        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<=?"
                        " ORDER BY observed_at DESC, sequence DESC LIMIT 1", (station, day, dec)).fetchone()
                    if not snap:
                        continue
                    try:
                        h = hours_to_settlement(day, as_of_utc=dec, settlement_timezone=snap[2])
                    except Exception:
                        continue
                    fam = family(h)
                    if fam is None or fam in found:
                        continue
                    age = abs((ts(dec) - ts(snap[1])).total_seconds())
                    if age > MAX_BOOK_AGE:
                        continue
                    buckets = sorted(json.loads(snap[0]), key=lambda b: int(b.get("ordinal") or 0))
                    market = market_reference_v1(buckets)
                    if market.get("state") != "complete":
                        continue
                    names = [str(b.get("bucket")) for b in buckets]
                    p = json.loads(pj)
                    if set(p) != set(names) or y not in names:
                        continue
                    book = []
                    for b in buckets:
                        asks = levels(b.get("ask_levels")) if b.get("ask_available", True) else []
                        bids = levels(b.get("bid_levels"))
                        a, bd = min(asks, default=None), max(bids, default=None)
                        book.append([bd[0] if bd else None, a[0] if a else None,
                                     bd[1] if bd else None, a[1] if a else None])
                    out.write(json.dumps({
                        "st": station, "d": day, "fam": fam, "season": season, "h": round(h, 2), "t": dec,
                        "ck": ck, "book_age_s": round(age, 1), "names": names, "y": y,
                        "p": [float(p[n]) for n in names], "q": [float(market["vector"][n]) for n in names],
                        "book": book,
                    }, ensure_ascii=False) + "\n")
                    found.add(fam)
                    stats["written"] += 1
        finally:
            conn.close()
        print(day, stats, flush=True)
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
