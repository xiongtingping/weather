"""D0 skew x direction backtest (PROTOCOL.md): one row per station-day with
every 30-minute checkpoint's market skew, mode, direction side and the price
of the three candidate buckets, so judge.py can apply each arm.

    uv run python docs/research/d0_skew_direction_backtest_20261017/backtest.py
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import run_change  # noqa: E402
from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.station_day_board import bucket_of, ladder_axis  # noqa: E402

HERE = Path(__file__).parent
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
SINCE = "2026-08-20"
STEP = timedelta(minutes=30)
MAX_AGE = timedelta(minutes=30)
WINDOW_PAST = 16


def _t(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def momentum_at(runs: dict, moment: datetime) -> float | None:
    """direction_signal.momentum on runs already read (the same loop)."""

    parts = []
    for _name, system, back, spread in ds.MOMENTUM_COMPONENTS:
        held = [row for row in runs.get(system) or () if row[0] <= moment]
        if len(held) >= back + 1:
            parts.append((float(held[-1][2]) - float(held[-1 - back][2])) / spread)
    return round(sum(parts), 4) if parts else None


def physics_doc(full: dict | None, day: str) -> dict | None:
    """The station's series cut to the d-16..d window production would hold."""

    if not full:
        return None
    lo = (date.fromisoformat(day) - timedelta(days=WINDOW_PAST)).isoformat()
    daily = {k: {d: v for d, v in s.items() if lo <= d <= day} for k, s in full["daily"].items()}
    return {"daily": daily, "fetched_at_utc": None}


def main() -> int:
    physics = json.loads(gzip.open(HERE / "physics.json.gz", "rt").read())
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
    counts = {"scopes": len(scopes), "no_truth": 0, "no_axis": 0, "no_checkpoint": 0, "station_days": 0}
    with gzip.open(HERE / "days.jsonl.gz", "wt", encoding="utf-8") as out:
        for station, day, tz in sorted(scopes):
            value = truth.get((station, day))
            if value is None:
                counts["no_truth"] += 1
                continue
            env = conn.execute(
                "SELECT bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
                " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                " AND json_extract(lineage_json,'$.bucket_intervals_c_json') IS NOT NULL LIMIT 1", (station, day),
            ).fetchone()
            if env is None:
                counts["no_axis"] += 1
                continue
            truth_bucket = bucket_of(ladder_axis(json.loads(env[0]), json.loads(env[1])), value)
            zone = ZoneInfo(tz)
            start = datetime.combine(date.fromisoformat(day), time(0), zone).astimezone(timezone.utc)
            stamps = [(_t(a), str(a), int(s)) for a, s in conn.execute(
                "SELECT observed_at, sequence FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                " AND state='COMMITTED' AND observed_at>=? ORDER BY observed_at, sequence",
                (station, day, (start - MAX_AGE).isoformat().replace("+00:00", "Z")),
            )]
            runs = run_change.family_runs(conn, station_id=station, event_date=day, systems=ds.MOMENTUM_SYSTEMS)
            phys = physics_doc(physics.get(station), day)
            checkpoints = []
            j = 0
            for n in range(48):
                at = start + n * STEP
                while j < len(stamps) and stamps[j][0] <= at:
                    j += 1
                if j == 0 or at - stamps[j - 1][0] > MAX_AGE:
                    continue
                _, raw_at, seq = stamps[j - 1]
                row = conn.execute(
                    "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                    " AND state='COMMITTED' AND observed_at=? AND sequence=?", (station, day, raw_at, seq),
                ).fetchone()
                buckets = json.loads(row[0]) if row else []
                ref = market_reference_v1(buckets)
                if ref.get("state") != "complete":
                    continue
                vec = {str(k): float(v) for k, v in ref["vector"].items()}
                order = sorted(buckets, key=lambda b: int(b["ordinal"]))
                labels = [str(b["bucket"]) for b in order]
                if set(labels) != set(vec):
                    continue
                ords = {str(b["bucket"]): int(b["ordinal"]) for b in order}
                reading = mean_mode_skew_reading_v1(vec, ords)
                if not reading.get("readable"):
                    continue
                mode = max(labels, key=lambda b: (vec[b], -labels.index(b)))
                m = labels.index(mode)
                mom = momentum_at(runs, at)
                feats = ds.physics_features(phys, event_date=day, at_utc=at)
                side = ds.combined_side(ds._sign(mom, quiet_below=ds.MOMENTUM_QUIET_BELOW),
                                        ds._sign(ds.model_score(mom, feats)))
                cand = {}
                for off in (-1, 0, 1):
                    i = m + off
                    if 0 <= i < len(order):
                        asks = [(float(x["price"]), float(x["shares"])) for x in order[i].get("ask_levels") or []
                                if float(x.get("shares") or 0) > 0]
                        best = min(asks, default=None)
                        cand[str(off)] = {"bucket": labels[i], "ask": best[0] if best else None,
                                          "ask_shares": best[1] if best else None, "p": round(vec[labels[i]], 5),
                                          "hit": int(labels[i] == truth_bucket)}
                checkpoints.append({"at": at.isoformat().replace("+00:00", "Z"),
                                    "local_hour": round((at - start).total_seconds() / 3600, 1),
                                    "skew": int(reading["sign"]), "momentum": mom, "side": side,
                                    "physics": feats.get("state"), "cand": cand})
            if not checkpoints:
                counts["no_checkpoint"] += 1
                continue
            counts["station_days"] += 1
            out.write(json.dumps({"station_id": station, "event_date": day, "unit": "F" if "°F" in labels[0] else "C",
                                  "truth_bucket": truth_bucket, "has_physics": station in physics,
                                  "checkpoints": checkpoints}, ensure_ascii=False) + "\n")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
