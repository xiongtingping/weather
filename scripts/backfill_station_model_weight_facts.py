#!/usr/bin/env python3
"""Charter 242, one-off: backfill ``station_model_weight_fact`` for event days
2026-01-01..2026-09-30 from the pre-registered replay capture.

Source: docs/research/station_model_weights_20261001/pass1_all/<STATION>.jsonl.gz
(``capture_long.py`` with SMW_ALL=1: production's single-system rule walked
forward, six systems, modelled arrivals; truth before 2026-07-01 is IEM under
the 2026-09-24 ruling).  The representative decisions are the study's own
(``score.py``): per station-day the last decision in each of five horizon bins
before the local day's end; each system's probability of the settled
integer-degree bucket.  Rows carry ``probability_source=REPLAY_216``,
``probability_grid=INTEGER_C`` and the truth's source; formal rows carry the
truth revision that was newest at the capture cutoff, IEM rows -1.

Reads the SSOT once (mode=ro) for the formal revisions; writes only
data/fusion_blend_training.db.  Idempotent (INSERT OR IGNORE).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import fusion_blend as FB  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402

CAPTURE = ROOT / "docs" / "research" / "station_model_weights_20261001" / "pass1_all"
CAPTURE_CUTOFF = "2026-10-01T09:00:00Z"
LAST_DAY = "2026-09-30"
SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def formal_revisions(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for station, day, revision in conn.execute(
        "SELECT station_id, local_date, MAX(revision_no) FROM calibration_station_daily_high_truth"
        " WHERE source_lane IN " + FORMAL_TRUTH_LANES_SQL + " AND qc_status='ACCEPTED'"
        " AND authority_scope IN ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')"
        " AND canonical_committed_at_utc<=? GROUP BY 1, 2", (CAPTURE_CUTOFF,),
    ):
        out[(str(station), str(day))] = int(revision)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(FB.FUSION_BLEND_TRAINING_DB))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    reader = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=30)
    revisions = formal_revisions(reader)
    reader.close()
    out = sqlite3.connect(args.db, timeout=30)
    out.execute("PRAGMA journal_mode=WAL")
    for statement in FB.TRAINING_DDL + FB.STATION_WEIGHT_DDL:
        out.execute(statement)
    counts: dict[str, int] = defaultdict(int)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    files = sorted(CAPTURE.glob("????.jsonl.gz"))
    if len(files) != 45:
        raise SystemExit(f"capture incomplete: {len(files)} station files under {CAPTURE}")
    for path in files:
        file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        rows = [json.loads(line) for line in gzip.open(path, "rt")]
        by_day: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            if row["event_date"] <= LAST_DAY:
                by_day[row["event_date"]].append(row)
        for day, day_rows in sorted(by_day.items()):
            end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(day_rows[0]["tz"])) + timedelta(days=1)
            for lo, hi in FB.HORIZON_BINS_H:
                in_bin = [r for r in day_rows if lo <= (end - utc(r["at"])).total_seconds() / 3600 < hi]
                if not in_bin:
                    continue
                r = max(in_bin, key=lambda x: x["at"])
                station = r["station"]
                pool = sorted(r["sys"])
                if r.get("truth_source") == "IEM_AGGREGATOR":
                    revision, truth_source = -1, "IEM_AGGREGATOR"
                else:
                    if (station, day) not in revisions:
                        counts["skip_no_formal_revision"] += 1
                        continue
                    revision, truth_source = revisions[(station, day)], "FORMAL"
                bucket = oos._temperature_bucket(float(r["actual"]))
                truth_probability = {s: float(r["sys"][s]["p"].get(bucket, 0.0)) for s in pool}
                seat = {s: float(r["seat"].get(s, 0.0)) for s in pool}
                identity = {
                    "station_id": station, "event_date": day, "horizon_bin": int(lo),
                    "truth_revision": revision, "decision_at_utc": r["at"],
                    "truth_c": float(r["actual"]), "truth_known_at_utc": r["truth_known_at"],
                    "truth_source": truth_source, "probability_source": "REPLAY_216",
                    "probability_grid": "INTEGER_C", "systems_json": json.dumps(pool),
                    "truth_probability_json": json.dumps(truth_probability, sort_keys=True),
                    "seat_weights_json": json.dumps(seat, sort_keys=True),
                    "served_weights_json": json.dumps(seat, sort_keys=True),
                    "fusion_rule_version": "REPLAY_SEAT", "fusion_state": "REPLAY",
                    "source_ref": f"{path.relative_to(ROOT)}@sha256:{file_sha[:16]}",
                    "schema": FB.STATION_WEIGHT_FACT_SCHEMA,
                }
                content = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                record = {**identity, "created_at_utc": created, "content_sha256": content}
                if not args.dry_run:
                    out.execute(
                        "INSERT OR IGNORE INTO station_model_weight_fact (" + ",".join(record)
                        + ") VALUES (" + ",".join("?" * len(record)) + ")", tuple(record.values()),
                    )
                counts["rows_" + truth_source.lower()] += 1
        if not args.dry_run:
            out.commit()
    counts["table_rows"] = out.execute("SELECT count(*) FROM station_model_weight_fact").fetchone()[0]
    out.close()
    print(json.dumps(dict(counts), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
