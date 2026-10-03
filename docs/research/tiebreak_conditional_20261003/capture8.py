#!/usr/bin/env python3
"""Read-only capture for PROTOCOL_7.md.

甲: every YES BUY leg the live book holds, with what the venue said about it
(order events), how it was meant to execute (the intent), and the book it
was decided on -- whether its bucket was the market's mode and where its
limit sat against the bid.  -> ``data8.json``

乙: the retired maker probe's frozen book on the cold disk -- the ``bid`` and
``bid+1t`` rules at the 60 minute horizon, on the market's mode bucket of
each placement.  -> ``<scratch>/probe_mode_rows.json`` (not committed).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.maker_shadow_store import MAKER_SHADOW_DB  # noqa: E402
import calibration_dashboard as board  # noqa: E402
import capture3  # noqa: E402

SCRATCH = Path(os.environ.get("TIEBREAK_SCRATCH", "/tmp"))
PROBE_RULES = ("bid", "bid+1t")
PROBE_HORIZON = 60


def lane_of(decision_id: str) -> str:
    for prefix, name in (("tiebreak_decision_", "秤砣线"), ("p1_consensus", "同心线"),
                         ("proper", "Proper线"), ("p1_tail", "P1尾段线"),
                         ("tail_short", "远尾做空线"), ("decision_", "篮子")):
        if decision_id.startswith(prefix) or prefix in decision_id[:24]:
            return name
    return "其他"


def live_legs(conn, labels) -> list[dict]:
    legs = [leg for leg in board._render_live_book(conn).get("legs") or []
            if str(leg.get("side") or "YES") == "YES"]
    ids = [str(leg["order_intent_id"]) for leg in legs]
    intents = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        for intent_id, action, payload in conn.execute(
            "SELECT order_intent_id,order_action,intent_json FROM basket_target_order_intents"
            f" WHERE order_intent_id IN ({','.join('?' * len(chunk))})", chunk,
        ).fetchall():
            intents[str(intent_id)] = (str(action), json.loads(payload or "{}"))
    decisions = sorted({str(leg["decision_id"]) for leg in legs if lane_of(str(leg["decision_id"])) == "篮子"})
    snapshot_of = {}
    for i in range(0, len(decisions), 400):
        chunk = decisions[i:i + 400]
        for decision_id, snapshot_id in conn.execute(
            "SELECT decision_id,ws_snapshot_id FROM basket_policy_training_fact"
            f" WHERE decision_id IN ({','.join('?' * len(chunk))})", chunk,
        ).fetchall():
            snapshot_of[str(decision_id)] = str(snapshot_id)
    tiebreak_snapshot = {}
    for station, day, stage, snapshot_id in conn.execute(
        "SELECT station_id,event_date,stage,snapshot_id FROM tiebreak_lane_evaluation WHERE fired=1"
    ).fetchall():
        tiebreak_snapshot.setdefault((str(station), str(day), str(stage)), str(snapshot_id))
    wanted = []
    for leg in legs:
        name = lane_of(str(leg["decision_id"]))
        snap = (snapshot_of.get(str(leg["decision_id"])) if name == "篮子" else
                tiebreak_snapshot.get((leg["station_id"], leg["event_date"], leg.get("stage")))
                if name == "秤砣线" else None)
        leg["_snapshot_id"] = snap
        if snap and lane._snapshot(conn, snap) is None:
            wanted.append((snap, leg["event_date"]))
    capture3.load_cold_snapshots(wanted)
    out = []
    cache: dict[str, dict | None] = {}
    for leg in legs:
        intent_id = str(leg["order_intent_id"])
        action, intent = intents.get(intent_id, ("", {}))
        if action and action != "BUY":
            continue
        events = [str(r[0]) for r in conn.execute(
            "SELECT event_type FROM basket_target_order_events WHERE order_intent_id=?"
            " ORDER BY observed_at", (intent_id,),
        ).fetchall()]
        snap = leg["_snapshot_id"]
        book = None
        if snap:
            if snap not in cache:
                snapshot = lane._snapshot(conn, snap) or capture3.COLD_SNAPSHOTS.get(snap)
                market = market_reference_v1(snapshot["buckets"]) if snapshot and snapshot.get("buckets") else {}
                cache[snap] = (
                    {"vector": market["vector"],
                     "books": {str(r.get("bucket")): lane.book_features(r) for r in snapshot["buckets"]}}
                    if market.get("state") == "complete" else None
                )
            seen = cache[snap]
            if seen and leg["bucket"] in seen["vector"]:
                mode = max(seen["vector"], key=lambda b: seen["vector"][b])
                quote = seen["books"].get(leg["bucket"]) or {}
                book = {"is_market_mode": leg["bucket"] == mode,
                        "market_share": seen["vector"][leg["bucket"]],
                        "best_bid": quote.get("best_bid"), "best_ask": quote.get("best_ask")}
        plan = intent.get("plan") or {}
        out.append({
            "order_intent_id": intent_id, "lane": lane_of(str(leg["decision_id"])),
            "station_id": leg["station_id"], "event_date": leg["event_date"],
            "stage": leg.get("stage"), "bucket": leg["bucket"],
            "limit_price": leg.get("limit_price"),
            "filled_size": float(leg.get("filled_size") or 0.0),
            "filled_notional_usdc": float(leg.get("filled_notional_usdc") or 0.0),
            "state": leg.get("state"), "created_at": leg.get("created_at"), "events": events,
            "execution_candidate": intent.get("execution_candidate") or plan.get("execution_candidate"),
            "post_only": intent.get("post_only") if intent.get("post_only") is not None else plan.get("post_only"),
            "settled_bucket": labels.get((leg["station_id"], leg["event_date"])),
            "book": book,
        })
    return out


def probe_rows(labels) -> dict:
    conn = sqlite3.connect(f"file:{MAKER_SHADOW_DB}?mode=ro", uri=True, timeout=60.0)
    groups: dict[tuple, list] = defaultdict(list)
    try:
        for row in conn.execute(
            "SELECT station_id,event_date,bucket,price_rule,placed_at_utc,lead_hours,limit_price,"
            "reference_bid,reference_ask,state FROM maker_shadow_order"
            f" WHERE horizon_minutes=? AND price_rule IN ({','.join('?' * len(PROBE_RULES))})"
            " AND state IN ('FILLED','EXPIRED')", (PROBE_HORIZON, *PROBE_RULES),
        ):
            groups[(str(row[0]), str(row[1]), str(row[4]))].append(row)
    finally:
        conn.close()
    out, placements, all_buckets = [], 0, defaultdict(lambda: [0, 0])
    for (station, day, placed), rows in groups.items():
        settled = labels.get((station, day))
        if settled is None:
            continue
        mids = {str(r[2]): (float(r[7]) + float(r[8])) / 2.0 for r in rows if str(r[3]) == "bid"}
        if not mids:
            continue
        placements += 1
        mode = max(mids, key=lambda b: mids[b])
        total = sum(mids.values())
        for r in rows:
            rule = str(r[3])
            filled = int(str(r[9]) == "FILLED")
            all_buckets[rule][0] += 1
            all_buckets[rule][1] += filled
            if str(r[2]) != mode:
                continue
            out.append({
                "station_id": station, "event_date": day, "placed_at_utc": placed, "rule": rule,
                "lead_hours": float(r[5]), "limit_price": float(r[6]),
                "reference_bid": float(r[7]), "reference_ask": float(r[8]),
                "mid": mids[mode], "share": mids[mode] / total if total > 0 else None,
                "filled": filled, "hit": int(settled == mode),
            })
    return {"rows": out, "placements": placements,
            "all_buckets": {k: {"orders": v[0], "filled": v[1]} for k, v in all_buckets.items()}}


def main() -> int:
    started = time.monotonic()
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)
    labels = shadow.settled_buckets(conn)
    legs = live_legs(conn, labels)
    conn.close()
    (HERE / "data8.json").write_text(json.dumps({
        "schema": "tiebreak_conditional_capture8_v1", "captured_at_utc": shadow.utc_now(), "legs": legs,
    }, ensure_ascii=False))
    print(f"[{time.monotonic() - started:6.1f}s] live YES BUY legs: {len(legs)}", flush=True)
    probe = probe_rows(labels)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "probe_mode_rows.json").write_text(json.dumps(probe))
    print(f"[{time.monotonic() - started:6.1f}s] probe placements {probe['placements']},"
          f" mode-bucket orders {len(probe['rows'])}, all buckets {probe['all_buckets']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
