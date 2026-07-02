#!/usr/bin/env python3
import argparse
import json
import math
import sqlite3
import os
from collections import Counter, defaultdict
from pathlib import Path


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latest_run_date(conn):
    cur = conn.cursor()
    cur.execute("SELECT run_date FROM event_runs ORDER BY run_date DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row else None


def load_rows(conn, run_date):
    cur = conn.cursor()
    cur.execute(
        "SELECT payload_json FROM event_runs WHERE run_date = ?", (run_date,)
    )
    for (payload_json,) in cur:
        if not payload_json:
            continue
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        for row in payload.get("rows") or []:
            yield row


def _default_min_edge_score():
    return float(os.environ.get("PM_AUTO_TRADE_MIN_EDGE_SCORE", "4"))


def summarize(args):
    db_path = Path(__file__).resolve().parents[1] / "data" / "archive.db"
    if not db_path.exists():
        raise SystemExit(f"database not found at {db_path}")
    conn = sqlite3.connect(db_path)
    run_date = args.run_date or latest_run_date(conn)
    if not run_date:
        raise SystemExit("no run_date found")

    total = 0
    thresholds = args.thresholds or [_default_min_edge_score()]
    counts = {t: 0 for t in thresholds}
    time_counts = Counter()
    structure_counts = Counter()
    high_edge_points_low_time = []
    recorded = []

    for row in load_rows(conn, run_date):
        total += 1
        edge_score = to_float(row.get("edge_score"))
        for t in thresholds:
            if edge_score is not None and edge_score >= t:
                counts[t] += 1
        time_factor = to_float(row.get("time_factor"))
        structure_factor = to_float(row.get("structure_factor"))
        time_bucket = f"{int((time_factor or 0) * 100):02d}" if time_factor is not None else "null"
        struct_bucket = f"{int((structure_factor or 0) * 100):02d}" if structure_factor is not None else "null"
        time_counts[time_bucket] += 1
        structure_counts[struct_bucket] += 1
        edge_points = to_float(row.get("edge_points"))
        if edge_points is None:
            q_round = row.get("q_raw") or row.get("q_round") or 0
            ask = to_float(row.get("best_ask"))
            edge_points = max((to_float(q_round) or 0) - (ask or 0), 0) * 100
        if edge_points >= args.high_edge_threshold and (time_factor or 0) <= args.low_time_threshold:
            high_edge_points_low_time.append(
                {
                    "outcome": row.get("outcome"),
                    "side": row.get("side"),
                    "edge_points": edge_points,
                    "time_factor": time_factor,
                    "structure_factor": structure_factor,
                    "edge_score": edge_score,
                }
            )
        recorded.append(
            {
                "edge_score": edge_score,
                "edge_points": edge_points,
                "time_factor": time_factor,
                "structure_factor": structure_factor,
            }
        )
    conn.close()

    print(f"run_date: {run_date}")
    print(f"total rows: {total}")
    for t in thresholds:
        count = counts.get(t, 0)
        print(
            f"edge_score >= {t}: {count} "
            f"({count / total * 100 if total else 0:.1f}%)"
        )
    print("time_factor distribution (bucketed by percentage):")
    for bucket, count in sorted(time_counts.items()):
        print(f"  {bucket}% -> {count}")
    print("structure_factor distribution (bucketed by percentage):")
    for bucket, count in sorted(structure_counts.items()):
        print(f"  {bucket}% -> {count}")
    print("high edge_points but low time_factor (sample up to 5):")
    for sample in high_edge_points_low_time[:5]:
        print(
            f"  {sample['outcome']} {sample['side']} "
            f"edge_points={sample['edge_points']:.2f} "
            f"time_factor={sample['time_factor']:.3f} "
            f"structure_factor={sample['structure_factor'] or 0:.3f} "
            f"edge_score={sample['edge_score']}"
        )
    return recorded


def main():
    parser = argparse.ArgumentParser(
        description="Summarize EdgeScore / time/structure distribution for auto-trade candidates"
    )
    parser.add_argument("--run-date", type=str, default="")
    parser.add_argument(
        "--min-edge-score",
        type=float,
        default=None,
        help="Single min edge score to compare (falls back to PM_AUTO_TRADE_MIN_EDGE_SCORE)",
    )
    parser.add_argument(
        "--min-edge-scores",
        type=str,
        default="",
        help="Comma-separated min edge score values to compare",
    )
    parser.add_argument(
        "--high-edge-threshold",
        type=float,
        default=2.0,
        help="Edge points threshold to flag for low time_factor",
    )
    parser.add_argument(
        "--low-time-threshold",
        type=float,
        default=0.2,
        help="time_factor ceiling to consider signal stale",
    )
    args = parser.parse_args()
    if args.min_edge_scores:
        parsed = []
        for part in args.min_edge_scores.split(","):
            try:
                parsed.append(float(part.strip()))
            except (TypeError, ValueError):
                continue
        args.thresholds = parsed
    else:
        args.thresholds = []
    if args.min_edge_score is not None:
        try:
            val = float(args.min_edge_score)
            args.thresholds.append(val)
        except (TypeError, ValueError):
            pass
    summarize(args)


if __name__ == "__main__":
    main()
