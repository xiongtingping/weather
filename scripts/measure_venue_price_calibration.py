"""What a bucket is worth at each side of the book, and what a resting order
actually gets.

Two measurements that only mean something together.

**A -- the venue price calibration.**  Every two-sided quote in the official
WS snapshots, binned by its mid, against the settled truth: what fraction of
buckets quoted around 3c actually resolve YES, around 30c, around 50c.  The
answer is not the price.  The ladder's mids sum to about 1.036 rather than
1.000, and that overround is not spread evenly -- it sits almost entirely on
the longshots, which is the favourite-longshot bias every parimutuel market
has ever had.  Reported as the ROI of buying that bucket at the bid, at the
mid and at the ask, because "is this bucket dear" has no answer until you
say which side of the spread you are standing on: on the cheap end the ask
is 91% above the bid.

**B -- what a resting order realises.**  The maker shadow book has posted
the same bucket at the bid, one tick inside it, at the mid and at the ask,
for four horizons, for months.  A's numbers are unconditional -- they assume
you can have the bid whenever you like.  B's are conditional on a fill, and
a resting bid only fills when somebody chooses to sell into it.  The gap
between the two IS the adverse selection, per tier and per rung, in points.

Neither number is a decision on its own.  A says the money is all in the
spread; B says how much of it survives having to wait for a counterparty.

Read-only.  Settlement uses the production rule (``_settled_bucket``) rather
than a copy of it, so this cannot disagree with the ledger about which
bucket won.

    python3 scripts/measure_venue_price_calibration.py \
        --db data/bayes_basket_target_ssot.db --since 2026-08-15 \
        --out docs/research/venue_price_calibration.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from bayes_basket_target.maker_shadow_store import attach_maker_shadow  # noqa: E402
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from paper_trading_ledger import _settled_bucket  # noqa: E402

# Bins on the mid.  Deliberately finer at the cheap end: that is where the
# overround lives and where the $1 notional floor forces the most shares.
MID_BINS = (
    (0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
    (0.20, 0.35), (0.35, 0.50), (0.50, 0.70), (0.70, 1.01),
)
# The three tiers the execution layer actually distinguishes, named by the
# price paid rather than by distance from the centre, because the venue's
# tick and notional floors key on price.
TIERS = (("centre", 0.35, 1.01), ("middle", 0.15, 0.35), ("tail", 0.0, 0.15))
LEAD_BINS = ((36.0, 1e9, ">=36h"), (24.0, 36.0, "24-36h"),
             (12.0, 24.0, "12-24h"), (0.0, 12.0, "<12h"))


def _iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _bin(value: float, bins) -> str | None:
    for lo, hi, *label in bins:
        if lo <= value < hi:
            return label[0] if label else f"{lo:.2f}-{hi:.2f}"
    return None


def _tier(price: float) -> str:
    for name, lo, hi in TIERS:
        if lo <= price < hi:
            return name
    return "tail"


def _quotes(buckets_json: str) -> dict[str, tuple[float, float]]:
    """Best bid and best ask per bucket; only two-sided quotes survive.

    A one-sided bucket has no mid and no spread, so it cannot answer either
    question this script asks.  Dropping it biases the sample toward the
    liquid part of the ladder, which is stated in the output rather than
    hidden: ``quoted_hit_mass`` is how much of the settled truth the
    surviving buckets cover.
    """

    out: dict[str, tuple[float, float]] = {}
    for entry in json.loads(buckets_json):
        asks = entry.get("ask_levels") or []
        bids = entry.get("bid_levels") or []
        ask = float(asks[0]["price"]) if asks else None
        bid = float(bids[0]["price"]) if bids else None
        if bid is None and float(entry.get("bid_price") or 0.0) > 0.0:
            bid = float(entry["bid_price"])
        if bid is None or ask is None or ask <= bid:
            continue
        out[str(entry.get("bucket"))] = (bid, ask)
    return out


def _truth(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """The settled high per station-day, highest revision, ACCEPTED only.

    Same question the ledger's settlement asks.  285 station-days carry two
    revisions with different values, so a query that lets dict-overwrite
    ordering pick one is correct only by accident.
    """

    return {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth t"
            " WHERE qc_status='ACCEPTED' AND local_date>=? AND local_date<=?"
            "   AND revision_no = (SELECT MAX(revision_no)"
            "     FROM calibration_station_daily_high_truth x"
            "     WHERE x.station_id=t.station_id"
            "       AND x.local_date=t.local_date AND x.qc_status='ACCEPTED')",
            (since, until),
        )
    }


def _ladders(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """The bucket geometry per station-day, as the bridge committed it."""

    out: dict[tuple[str, str], list] = defaultdict(list)
    seen: dict[tuple[str, str], set] = defaultdict(set)
    for station, day, bucket, ordinal, low, high in conn.execute(
        "SELECT DISTINCT h.station_id, h.event_date, b.bucket, b.ordinal,"
        " b.lower_c, b.upper_c"
        " FROM basket_probability_bridge_history h"
        " JOIN basket_probability_bridge_bucket_probability b"
        "   ON b.history_id = h.history_id"
        " WHERE h.state='COMMITTED' AND h.event_date>=? AND h.event_date<=?"
        " ORDER BY h.station_id, h.event_date, b.ordinal",
        (since, until),
    ):
        key = (str(station), str(day))
        if str(bucket) in seen[key]:
            continue
        seen[key].add(str(bucket))
        # The production rule takes (bucket, ordinal, lower, upper, prob).
        out[key].append((str(bucket), int(ordinal), low, high, 0.0))
    return out


def measure_book(conn, since, until, per_day):
    """Part A: what a bucket at a given mid is really worth."""

    truth = _truth(conn, since, until)
    ladders = _ladders(conn, since, until)
    snapshots: dict[tuple[str, str], list] = defaultdict(list)
    for station, day, observed, buckets_json in conn.execute(
        "SELECT station_id, event_date, observed_at, buckets_json"
        " FROM basket_official_ws_snapshot"
        " WHERE state='COMMITTED' AND event_date>=? AND event_date<=?",
        (since, until),
    ):
        snapshots[(str(station), str(day))].append((str(observed), buckets_json))

    by_mid: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
    by_lead: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    overround: list[float] = []
    hit_mass = [0, 0]
    station_days = 0
    for (station, day), rows in snapshots.items():
        settled = truth.get((station, day))
        ladder = ladders.get((station, day))
        if settled is None or not ladder:
            continue
        landed = _settled_bucket(list(ladder), settled)
        if landed is None:
            continue
        winner = landed[0]
        try:
            timezone_name = conn.execute(
                "SELECT settlement_timezone FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? LIMIT 1", (station, day),
            ).fetchone()
            close = (datetime.fromisoformat(day)
                     .replace(tzinfo=ZoneInfo(str(timezone_name[0])))
                     + timedelta(days=1)).astimezone(timezone.utc)
        except Exception:
            close = None
        station_days += 1
        rows.sort()
        step = max(1, len(rows) // per_day)
        for observed, buckets_json in rows[::step][:per_day]:
            try:
                quotes = _quotes(buckets_json)
            except (ValueError, TypeError):
                continue
            if not quotes:
                continue
            mids = {b: (q[0] + q[1]) / 2.0 for b, q in quotes.items()}
            overround.append(sum(mids.values()))
            hit_mass[1] += 1
            if winner in quotes:
                hit_mass[0] += 1
            lead = None
            if close is not None:
                lead = (close - _iso(observed)).total_seconds() / 3600.0
            for bucket, (bid, ask) in quotes.items():
                mid = mids[bucket]
                won = 1.0 if bucket == winner else 0.0
                label = _bin(mid, MID_BINS)
                if label is not None:
                    cell = by_mid[label]
                    cell[0] += 1
                    cell[1] += bid
                    cell[2] += ask
                    cell[3] += mid
                    cell[4] += won
                if lead is not None:
                    lead_label = _bin(lead, LEAD_BINS)
                    if lead_label is not None:
                        cell = by_lead[lead_label]
                        cell[0] += 1
                        cell[1] += bid
                        cell[2] += ask
                        cell[3] += won
    def row(label, n, bid, ask, mid, won):
        return {
            "bin": label, "n": n,
            "mean_bid": round(bid / n, 6), "mean_ask": round(ask / n, 6),
            "mean_mid": round(mid / n, 6) if mid is not None else None,
            "realised": round(won / n, 6),
            "roi_at_bid": round((won / n - bid / n) / (bid / n), 6) if bid else None,
            "roi_at_mid": (round((won / n - mid / n) / (mid / n), 6)
                           if mid else None),
            "roi_at_ask": round((won / n - ask / n) / (ask / n), 6) if ask else None,
        }

    mid_rows = [row(k, *by_mid[k]) for k in sorted(by_mid)]
    lead_rows = [row(k, c[0], c[1], c[2], None, c[3]) for k, c in by_lead.items()]
    total = [0, 0.0, 0.0, 0.0, 0.0]
    for cell in by_mid.values():
        for index in range(5):
            total[index] += cell[index]
    return {
        "station_days": station_days,
        "observations": total[0],
        "mean_overround_sum_mid": (round(sum(overround) / len(overround), 6)
                                   if overround else None),
        "quoted_hit_mass": (round(hit_mass[0] / hit_mass[1], 6)
                            if hit_mass[1] else None),
        "total": row("ALL", *total),
        "by_mid": mid_rows,
        "by_lead": sorted(lead_rows, key=lambda r: r["bin"]),
    }


def measure_resting(conn):
    """Part B: what a resting order at each rung actually realises.

    Only filled shadow orders have a settlement row, so the fill rate is
    taken from the order table and the realised value from the join.  Both
    are needed: a rung that realises well because it almost never fills is
    not an execution policy.
    """

    posted: dict[tuple[str, str, int], list] = defaultdict(lambda: [0, 0])
    for rule, tier, horizon, n, filled in conn.execute(
        "SELECT price_rule,"
        " CASE WHEN (reference_bid+reference_ask)/2 < 0.15 THEN 'tail'"
        "      WHEN (reference_bid+reference_ask)/2 < 0.35 THEN 'middle'"
        "      ELSE 'centre' END,"
        " horizon_minutes, COUNT(*),"
        " SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END)"
        " FROM maker_shadow_order"
        " WHERE reference_ask>reference_bid AND reference_bid>0"
        " GROUP BY 1,2,3"
    ):
        posted[(str(rule), str(tier), int(horizon))] = [int(n), int(filled or 0)]

    out = []
    for rule, tier, horizon, n, mid, limit, hit in conn.execute(
        "SELECT o.price_rule,"
        " CASE WHEN (o.reference_bid+o.reference_ask)/2 < 0.15 THEN 'tail'"
        "      WHEN (o.reference_bid+o.reference_ask)/2 < 0.35 THEN 'middle'"
        "      ELSE 'centre' END,"
        " o.horizon_minutes, COUNT(*),"
        " AVG((o.reference_bid+o.reference_ask)/2.0),"
        " AVG(o.limit_price), AVG(s.hit)"
        " FROM maker_shadow_order o"
        " JOIN maker_shadow_settlement s ON s.order_id=o.order_id"
        " WHERE o.reference_ask>o.reference_bid AND o.reference_bid>0"
        "   AND o.limit_price>0"
        " GROUP BY 1,2,3"
    ):
        key = (str(rule), str(tier), int(horizon))
        n_posted, n_filled = posted.get(key, [0, 0])
        out.append({
            "price_rule": str(rule), "tier": str(tier),
            "horizon_minutes": int(horizon),
            "posted": n_posted,
            "settled_fills": int(n),
            "fill_rate": round(n_filled / n_posted, 6) if n_posted else None,
            "mid_at_post": round(float(mid), 6),
            "limit": round(float(limit), 6),
            "realised": round(float(hit), 6),
            # What one dollar of filled notional returns.
            "roi_per_fill": round(float(hit) / float(limit) - 1.0, 6),
            # How far the filled population sits below the book it was
            # posted against.  This is the adverse selection, before any
            # correction for the mid's own overround.
            "fill_discount_vs_mid": round(1.0 - float(hit) / float(mid), 6),
        })
    return sorted(out, key=lambda r: (r["tier"], r["horizon_minutes"],
                                      r["price_rule"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data"
                                            / "bayes_basket_target_ssot.db"))
    parser.add_argument("--since", default="2026-08-15")
    parser.add_argument("--until", default="2099-12-31")
    parser.add_argument("--snapshots-per-day", type=int, default=6,
                        help="books sampled per station-day (evenly spaced)")
    parser.add_argument("--skip-resting", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    attach_maker_shadow(conn, ssot_path=args.db)
    from paper_ledger_reader import attach_paper_ledger  # noqa: PLC0415
    attach_paper_ledger(conn)
    conn.execute("PRAGMA query_only=1")

    book = measure_book(conn, args.since, args.until, args.snapshots_per_day)
    print(f"== A. venue price calibration =="
          f"  station-days {book['station_days']}"
          f"  observations {book['observations']}")
    print(f"   mean sum-of-mids {book['mean_overround_sum_mid']}"
          f"   winner quoted two-sided {book['quoted_hit_mass']:.1%}")
    head = (f"{'mid bin':12}{'n':>8}{'bid':>9}{'ask':>9}{'realised':>10}"
            f"{'ROI@bid':>10}{'ROI@mid':>10}{'ROI@ask':>10}")
    print(head)
    for row in book["by_mid"] + [book["total"]]:
        print(f"{row['bin']:12}{row['n']:8d}{row['mean_bid']:9.4f}"
              f"{row['mean_ask']:9.4f}{row['realised']:10.4f}"
              f"{row['roi_at_bid']:+10.1%}"
              f"{(row['roi_at_mid'] if row['roi_at_mid'] is not None else 0):+10.1%}"
              f"{row['roi_at_ask']:+10.1%}")
    print()
    print("   by lead (all mids pooled)")
    for row in book["by_lead"]:
        print(f"{row['bin']:12}{row['n']:8d}{row['mean_bid']:9.4f}"
              f"{row['mean_ask']:9.4f}{row['realised']:10.4f}"
              f"{row['roi_at_bid']:+10.1%}{'':10}{row['roi_at_ask']:+10.1%}")

    resting = [] if args.skip_resting else measure_resting(conn)
    if resting:
        print()
        print("== B. what a resting rung realises (maker shadow book) ==")
        print(f"{'tier':8}{'rule':10}{'horizon':>8}{'posted':>9}{'fill':>8}"
              f"{'mid':>8}{'limit':>8}{'realised':>10}{'ROI/fill':>10}"
              f"{'AS vs mid':>11}")
        for row in resting:
            if row["price_rule"] not in {"bid", "bid+1t", "mid", "ask"}:
                continue
            if row["horizon_minutes"] != 60:
                continue
            print(f"{row['tier']:8}{row['price_rule']:10}"
                  f"{row['horizon_minutes']:8d}{row['posted']:9d}"
                  f"{(row['fill_rate'] or 0):8.1%}{row['mid_at_post']:8.3f}"
                  f"{row['limit']:8.3f}{row['realised']:10.3f}"
                  f"{row['roi_per_fill']:+10.1%}"
                  f"{row['fill_discount_vs_mid']:+11.1%}")

    if args.out:
        payload = {
            "schema": "venue_price_calibration_v1",
            "since": args.since, "until": args.until,
            "snapshots_per_station_day": args.snapshots_per_day,
            "book": book, "resting": resting,
        }
        Path(args.out).write_text(json.dumps(payload, indent=1) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
