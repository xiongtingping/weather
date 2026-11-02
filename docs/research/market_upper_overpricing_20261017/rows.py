"""Rows for the market upper-bucket overpricing study (PROTOCOL.md).

One row per bucket per station-day per lead band: its rung offset from the
market mode, the market probability, the YES best bid/ask and the bid's
depth, and whether the truth landed in it.  Read-only on the SSOT; each
station-day is its own short read.

    uv run python docs/research/market_upper_overpricing_20261017/rows.py
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.station_day_board import bucket_of, ladder_axis  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = Path(__file__).with_name("rows.jsonl.gz")
SINCE = "2026-07-28"
BANDS = ((0, 12), (12, 24), (24, 36), (36, 48), (48, 10_000))


def _t(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    truth: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' AND local_date>=?"
        " ORDER BY revision_no", (SINCE,),
    ):
        truth[(str(station), str(day))] = float(value)
    scopes = conn.execute(
        "SELECT DISTINCT station_id, event_date, settlement_timezone FROM basket_official_ws_snapshot"
        " WHERE state='COMMITTED' AND event_date>=?", (SINCE,),
    ).fetchall()
    counts = {"scopes": len(scopes), "no_truth": 0, "no_axis": 0, "truth_off_axis": 0, "samples": 0,
              "incomplete_book": 0, "rows": 0}
    with gzip.open(OUT, "wt", encoding="utf-8") as out:
        for station, day, tz in sorted(scopes):
            value = truth.get((station, day))
            if value is None:
                counts["no_truth"] += 1
                continue
            env = conn.execute(
                "SELECT bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
                " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                " AND json_extract(lineage_json,'$.bucket_intervals_c_json') IS NOT NULL LIMIT 1",
                (station, day),
            ).fetchone()
            if env is None:
                counts["no_axis"] += 1
                continue
            axis = ladder_axis(json.loads(env[0]), json.loads(env[1]))
            truth_bucket = bucket_of(axis, value)
            if truth_bucket is None:
                counts["truth_off_axis"] += 1
                continue
            end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), datetime.min.time(),
                                   ZoneInfo(tz)).astimezone(timezone.utc)
            stamps = [(str(a), int(s)) for a, s in conn.execute(
                "SELECT observed_at, sequence FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED' ORDER BY observed_at, sequence",
                (station, day),
            )]
            for lo, hi in BANDS:
                inside = [x for x in stamps if lo <= (end - _t(x[0])).total_seconds() / 3600.0 < hi]
                # The band's last complete book, walking back from its end.
                for at, seq in reversed(inside[-40:]):
                    raw = conn.execute(
                        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                        " AND state='COMMITTED' AND observed_at=? AND sequence=?", (station, day, at, seq),
                    ).fetchone()
                    buckets = json.loads(raw[0]) if raw else []
                    ref = market_reference_v1(buckets)
                    if ref.get("state") != "complete":
                        counts["incomplete_book"] += 1
                        continue
                    vec = {str(k): float(v) for k, v in ref["vector"].items()}
                    order = sorted((b for b in buckets), key=lambda b: int(b["ordinal"]))
                    labels = [str(b["bucket"]) for b in order]
                    if set(labels) != set(vec) or truth_bucket not in vec:
                        counts["incomplete_book"] += 1
                        continue
                    mode = max(labels, key=lambda b: (vec[b], -labels.index(b)))
                    m = labels.index(mode)
                    lead = (end - _t(at)).total_seconds() / 3600.0
                    for i, b in enumerate(order):
                        bids = [(float(x["price"]), float(x["shares"])) for x in b.get("bid_levels") or []
                                if float(x.get("shares") or 0) > 0]
                        asks = [float(x["price"]) for x in b.get("ask_levels") or [] if float(x.get("shares") or 0) > 0]
                        best_bid = max(bids, default=None)
                        out.write(json.dumps({
                            "station_id": station, "event_date": day, "band": lo, "lead_h": round(lead, 2),
                            "observed_at": at, "unit": "F" if "°F" in labels[i] else "C",
                            "bucket": labels[i], "k": i - m, "p": round(vec[labels[i]], 6),
                            "bid": best_bid[0] if best_bid else None, "bid_shares": best_bid[1] if best_bid else None,
                            "ask": min(asks) if asks else None, "hit": int(labels[i] == truth_bucket),
                        }, ensure_ascii=False) + "\n")
                        counts["rows"] += 1
                    counts["samples"] += 1
                    break
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
