"""Event-time dataset (PROTOCOL_EVENT.md): one row per P1 report event.

    python3 build_events.py OUT.jsonl.gz

Read-only; every statement is fetched to completion.  Resumable by
station-day; each pass stops after ~9 minutes.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

SSOT = f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro"
ARCHIVE = f"file:{ROOT / 'data' / 'archive.db'}?mode=ro"
FIRST_DAY, LAST_DAY = "2026-08-25", "2026-09-23"
LAGS = (2, 5, 15)
LEVELS = 6


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ladder(levels, reverse):
    out = [[float(l["price"]), float(l.get("shares") or l.get("size") or 0.0)] for l in (levels or [])]
    out = [x for x in out if x[1] > 0]
    out.sort(key=lambda t: t[0], reverse=reverse)
    return out[:LEVELS]


def main() -> None:
    out_path = Path(sys.argv[1])
    done = set()
    if out_path.exists():
        with gzip.open(out_path, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                done.add((r["st"], r["day"]))
    done_marker = out_path.with_suffix(".done.json")
    if done_marker.exists():
        done |= {tuple(x) for x in json.loads(done_marker.read_text())}
    db = sqlite3.connect(SSOT, uri=True, timeout=60)
    arch = sqlite3.connect(ARCHIVE, uri=True, timeout=60)
    tz = {}
    for st, zone in db.execute("SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding").fetchall():
        tz.setdefault(st, zone)
    labels = db.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN ? AND ? AND truth_revision_no=(SELECT MAX(truth_revision_no)"
        " FROM basket_station_day_settlement_label l2 WHERE l2.station_id=l.station_id AND l2.event_date=l.event_date)"
        " ORDER BY event_date, station_id", (FIRST_DAY, LAST_DAY)).fetchall()
    roots: dict = {}
    started = time.monotonic()
    processed = []
    with gzip.open(out_path, "at") as fh:
        for st, day, truth in labels:
            if (st, day) in done or st not in tz:
                continue
            if time.monotonic() - started > 540:
                break
            p1 = db.execute(
                "SELECT decision_at_utc, probabilities_json,"
                " json_extract(lineage_json,'$.canonical_observation_event_id'),"
                " json_extract(lineage_json,'$.hard_excluded_buckets'),"
                " json_extract(lineage_json,'$.prediction_root_checkpoint_id'),"
                " json_extract(lineage_json,'$.prediction_root_stage')"
                " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND stage='P1' AND state='COMMITTED'"
                " ORDER BY decision_at_utc", (st, day)).fetchall()
            snaps = db.execute(
                "SELECT observed_at, snapshot_id FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                " ORDER BY observed_at", (st, day)).fetchall()
            processed.append([st, day])
            if not p1 or not snaps:
                continue
            snap_t = [s[0] for s in snaps]
            cache: dict = {}

            def book(sid):
                if sid not in cache:
                    bj = json.loads(db.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?", (sid,)).fetchall()[0][0])
                    ref = market_reference_v1(bj)
                    cache[sid] = {
                        "m": ref.get("vector") if ref.get("state") == "complete" else None,
                        "asks": {b["bucket"]: ladder(b.get("ask_levels"), False) for b in bj},
                        "bids": {b["bucket"]: ladder(b.get("bid_levels"), True) for b in bj},
                        "min_order": {b["bucket"]: float(b.get("min_order_size") or 5.0) for b in bj},
                    }
                return cache[sid]

            def latest_le(t_s):
                i = bisect_right(snap_t, t_s)
                return snaps[i - 1] if i > 0 else None

            day_end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz[st])) + timedelta(days=1)
            prev_vec, prev_obs, prev_mask = None, None, set()
            for decided, probs, obs_id, mask_s, root_id, root_stage in p1:
                vec = json.loads(probs)
                mask = {b for b in (mask_s or "").split(",") if b}
                new_obs = obs_id and obs_id != prev_obs
                changed = prev_vec is None or vec != prev_vec
                if new_obs and changed:
                    row_obs = arch.execute(
                        "SELECT valid_at, first_seen_at FROM reported_trajectory_observations WHERE observation_id=?",
                        (obs_id,)).fetchall()
                    fresh = False
                    if row_obs:
                        t_pub, first_seen = row_obs[0]
                        # Amendment 1: a report we saw and answered promptly,
                        # not a replay of an old one under a new stamp.
                        fresh = (
                            (ts(decided) - ts(first_seen)).total_seconds() <= 600
                            and (ts(first_seen) - ts(t_pub)).total_seconds() <= 5400
                        )
                    if row_obs and fresh:
                        pre = latest_le(t_pub)
                        now = latest_le(decided)
                        lag_books = {}
                        for lag in LAGS:
                            hi = iso(ts(decided) + timedelta(minutes=lag))
                            cand = latest_le(hi)
                            if cand and cand[0] >= decided:
                                b = book(cand[1])
                                lag_books[str(lag)] = {"at": cand[0], "m": b["m"], "asks": b["asks"], "bids": b["bids"], "min_order": b["min_order"]}
                        if prev_vec is None and root_id:
                            key = (root_stage or "P0", root_id)
                            if key not in roots:
                                rr = db.execute("SELECT probabilities_json FROM basket_probability_envelope WHERE checkpoint_id=? AND stage=? LIMIT 1", key).fetchall()
                                roots[key] = json.loads(rr[0][0]) if rr else None
                            base = roots[key]
                        else:
                            base = prev_vec
                        row = {
                            "st": st, "day": day, "truth": truth, "t_pub": t_pub, "first_seen": first_seen, "t_ours": decided,
                            "hours_to_end": (day_end - ts(decided)).total_seconds() / 3600.0,
                            "p1": vec, "base": base, "base_is_root": prev_vec is None,
                            "mask": sorted(mask), "prev_mask": sorted(prev_mask),
                            "m_pre": book(pre[1])["m"] if pre else None, "pre_at": pre[0] if pre else None,
                            "m_now": book(now[1])["m"] if now else None, "now_at": now[0] if now else None,
                            "books": lag_books,
                        }
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                prev_vec, prev_obs, prev_mask = vec, obs_id or prev_obs, mask
    old = json.loads(done_marker.read_text()) if done_marker.exists() else []
    done_marker.write_text(json.dumps(old + processed))
    remaining = sum(1 for st, day, _ in labels if st in tz and (st, day) not in done) - len(processed)
    print(f"station-days this pass: {len(processed)}; remaining: {remaining}; {time.monotonic()-started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
