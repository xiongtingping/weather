"""Build the replay dataset: one row per (station, event day, hours-to-day-end).

Read-only against the SSOT; every statement is fetched to completion so no
read transaction outlives its query (a long reader pins the WAL).  Resumable:
station-days already in the output are skipped, so the build can be run in
several bounded passes.

    python3 build_dataset.py OUT.jsonl.gz [--limit N]
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402

DB = f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro"
GRID_H = (44, 36, 30, 24, 18, 14, 10, 7, 4)
FIRST_DAY, LAST_DAY = "2026-08-25", "2026-09-23"
LEVELS = 6
STAGE_ORDER = ("P1", "P0-M", "P0")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ladder(levels, reverse: bool) -> list[list[float]]:
    out = []
    for level in levels or []:
        price = float(level.get("price"))
        shares = float(level.get("shares") or level.get("size") or 0.0)
        if shares > 0:
            out.append([price, shares])
    out.sort(key=lambda t: t[0], reverse=reverse)
    return out[:LEVELS]


def main() -> None:
    out_path = Path(sys.argv[1])
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    done: set[tuple[str, str]] = set()
    if out_path.exists():
        with gzip.open(out_path, "rt") as fh:
            for line in fh:
                row = json.loads(line)
                done.add((row["st"], row["day"]))
    db = sqlite3.connect(DB, uri=True, timeout=60)
    tz: dict[str, str] = {}
    for st, zone in db.execute(
        "SELECT station_id, settlement_timezone FROM calibration_station_market_timezone_binding"
    ).fetchall():
        tz.setdefault(st, zone)
    labels = db.execute(
        "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l"
        " WHERE event_date BETWEEN ? AND ? AND truth_revision_no=("
        "  SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label l2"
        "  WHERE l2.station_id=l.station_id AND l2.event_date=l.event_date)"
        " ORDER BY event_date, station_id",
        (FIRST_DAY, LAST_DAY),
    ).fetchall()
    by_checkpoint: dict[tuple[str, str], dict | None] = {}

    def checkpoint(stage: str, checkpoint_id: str | None) -> dict | None:
        if not checkpoint_id:
            return None
        key = (stage, checkpoint_id)
        if key not in by_checkpoint:
            rows = db.execute(
                "SELECT probabilities_json FROM basket_probability_envelope"
                " WHERE checkpoint_id=? AND stage=? LIMIT 1",
                (checkpoint_id, stage),
            ).fetchall()
            by_checkpoint[key] = json.loads(rows[0][0]) if rows else None
        return by_checkpoint[key]

    started = time.monotonic()
    written = 0
    with gzip.open(out_path, "at") as fh:
        for st, day, truth in labels:
            if (st, day) in done or st not in tz:
                continue
            if limit is not None and written >= limit:
                break
            if time.monotonic() - started > 540:
                break
            envs = db.execute(
                "SELECT stage, decision_at_utc, fresh_until_utc, envelope_id FROM basket_probability_envelope"
                " WHERE station_id=? AND event_date=? AND stage IN ('P0','P0-M','P1') AND state='COMMITTED'",
                (st, day),
            ).fetchall()
            snaps = db.execute(
                "SELECT observed_at, snapshot_id FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? ORDER BY observed_at",
                (st, day),
            ).fetchall()
            day_end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz[st])) + timedelta(days=1)
            env_cache: dict[str, tuple[dict, dict]] = {}
            snap_cache: dict[str, list] = {}
            for hours in GRID_H:
                at = day_end - timedelta(hours=hours)
                at_s = _iso(at)
                chosen = None
                for stage in STAGE_ORDER:
                    cands = [e for e in envs if e[0] == stage and e[1] <= at_s and (e[2] or "") >= at_s]
                    if cands:
                        chosen = max(cands, key=lambda e: e[1])
                        break
                if chosen is None:
                    continue
                stage, decided, _, env_id = chosen
                # Amendment 1: the book must be no older than the belief it is
                # traded against, and at most 15 minutes past the grid point.
                hi_s = _iso(at + timedelta(minutes=15))
                after = [s for s in snaps if decided <= s[0] <= hi_s]
                snap = [s for s in after if s[0] <= at_s] or after[:1]
                if not snap:
                    continue
                if env_id not in env_cache:
                    p, lin = db.execute(
                        "SELECT probabilities_json, lineage_json FROM basket_probability_envelope WHERE envelope_id=?",
                        (env_id,),
                    ).fetchall()[0]
                    env_cache[env_id] = (json.loads(p), json.loads(lin))
                served, lineage = env_cache[env_id]
                root = taf_vec = pred_root = None
                mask: list[str] = []
                if stage == "P0":
                    root = served
                elif stage == "P0-M":
                    root = checkpoint("P0", lineage.get("p0_root_checkpoint_id"))
                    taf_vec = served
                else:
                    root = checkpoint("P0", lineage.get("p0_root_checkpoint_id"))
                    pred_stage = lineage.get("prediction_root_stage") or "P0"
                    pred_root = checkpoint(pred_stage, lineage.get("prediction_root_checkpoint_id"))
                    if pred_stage == "P0-M":
                        taf_vec = pred_root
                    raw_mask = lineage.get("hard_excluded_buckets") or ""
                    mask = [b for b in (raw_mask.split(",") if isinstance(raw_mask, str) else raw_mask) if b]
                pick = snap[-1] if snap[-1][0] <= at_s else snap[0]
                sid = pick[1]
                if sid not in snap_cache:
                    snap_cache[sid] = json.loads(db.execute(
                        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?", (sid,)
                    ).fetchall()[0][0])
                buckets = snap_cache[sid]
                ref = market_reference_v1(buckets)
                row = {
                    "st": st, "day": day, "H": hours, "at": at_s, "truth": truth,
                    "stage": stage, "decided": decided, "env": env_id,
                    "served": served, "root": root, "taf": taf_vec, "pred_root": pred_root, "mask": mask,
                    "m_state": ref.get("state"), "m": ref.get("vector") if ref.get("state") == "complete" else None,
                    "snap_at": pick[0],
                    "asks": {b["bucket"]: _ladder(b.get("ask_levels"), False) for b in buckets},
                    "bids": {b["bucket"]: _ladder(b.get("bid_levels"), True) for b in buckets},
                    "min_order": {b["bucket"]: float(b.get("min_order_size") or 5.0) for b in buckets},
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    remaining = sum(1 for st, day, _ in labels if (st, day) not in done and st in tz) - written
    print(f"station-days written this pass: {written}; remaining: {remaining}; {time.monotonic()-started:.0f}s")


if __name__ == "__main__":
    main()
