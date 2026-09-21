#!/usr/bin/env python3
"""Research only: production's stable-pool replay with and without AIFS ENS.

Two arms over the same window, the same code and the same read-only SSOT:

  baseline    ``replay_p0_stable_pool.replay_station`` exactly as production
              has it -- the four official native families;
  with_aifs   the same call, with AIFS ENS added as one more native member:
              the research database built by ``research_aifs_ens_history.py``
              is ATTACHed and a TEMP VIEW of the same name as the member
              table unions its rows with the SSOT's, and AIFS's scope
              contract (four cycles x three calendar leads, its own
              extraction versions) is registered in this process only.

SQLite resolves an unqualified table name in ``temp`` before ``main``, so the
production code reads the view without knowing it is there -- and nothing is
ever written to the SSOT, which stays ``mode=ro`` with ``query_only``.

The AIFS rows carry PF50's own arrival stamp for the same run and station-day,
so every update that changes one system changes both, and the arms can be
paired record for record on (station, event date, update time).  Outputs are
one gzip JSONL per station per arm, resumable exactly like the replay itself.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import replay_p0_stable_pool as replay  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
RESEARCH_DB = ROOT / "data" / "aifs_ens_research" / "aifs_ens_members.db"
OUT = ROOT / "data" / "aifs_ens_research" / "replay"
AIFS = "ECMWF_AIFS_ENS_GLOBAL"
EXTRACTION_TEMPLATE = "research_ecmwf_open_data_aifs_ens_0p25_2t6_{cycle}z_d{lead}_v1"
MEMBER_TABLE = "calibration_native_ensemble_historical_member_run"


def aifs_scope_contract() -> dict:
    return {
        "transport_provider_id": "ECMWF_OPEN_DATA_S3",
        "leads": {
            f"d{lead}": [
                {
                    "run_cycle": f"{cycle}Z",
                    "extraction_version": EXTRACTION_TEMPLATE.format(cycle=cycle, lead=lead),
                }
                for cycle in ("00", "06", "12", "18")
            ]
            for lead in (0, 1, 2)
        },
    }


def connect(arm: str, research_db: Path = RESEARCH_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    if arm == "with_aifs":
        conn.execute(
            "ATTACH DATABASE ? AS aifs",
            (f"file:{research_db}?mode=ro",),
        )
        conn.execute(
            f"CREATE TEMP VIEW {MEMBER_TABLE} AS "
            f"SELECT * FROM main.{MEMBER_TABLE} "
            f"UNION ALL SELECT * FROM aifs.{MEMBER_TABLE}"
        )
    # After the view: query_only also refuses the temp schema.  Both files
    # are opened mode=ro, so this is the second lock, not the first.
    conn.execute("PRAGMA query_only=1")
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("baseline", "with_aifs"), required=True)
    parser.add_argument("--first-event-date", default="2026-07-20")
    parser.add_argument("--last-event-date", default="2026-09-19")
    parser.add_argument("--cutoff-utc", default="2026-09-21T00:00:00Z")
    parser.add_argument("--budget-seconds", type=float, default=540.0)
    parser.add_argument("--stations", nargs="*", default=None)
    parser.add_argument("--out-name", default="",
                        help="output directory name (default: the arm), e.g. for an interim window")
    parser.add_argument(
        "--research-db", type=Path, default=RESEARCH_DB,
        help="a snapshot of the research database, while the backfill still writes it",
    )
    args = parser.parse_args()

    replay.FIRST_EVENT_DATE = args.first_event_date
    replay.LAST_EVENT_DATE = args.last_event_date
    replay.CUTOFF_UTC = args.cutoff_utc
    if args.arm == "with_aifs":
        oos.OFFICIAL_ARCHIVE_SCOPE_CONTRACTS[AIFS] = aifs_scope_contract()
    out = OUT / (args.out_name or args.arm)
    out.mkdir(parents=True, exist_ok=True)
    conn = connect(args.arm, args.research_db)
    stations = args.stations or sorted({
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT station_id FROM calibration_station_daily_high_truth "
            "WHERE qc_status='ACCEPTED' AND local_date>=?",
            (args.first_event_date,),
        )
    })
    started = time.monotonic()
    for station in stations:
        target = out / f"{station}.jsonl.gz"
        if target.exists():
            continue
        if time.monotonic() - started > args.budget_seconds:
            break
        t0 = time.monotonic()
        records = replay.replay_station(conn, station)
        partial = target.with_suffix(".partial")
        with gzip.open(partial, "wt") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        partial.rename(target)
        pools = sum(1 for r in records if AIFS in r.get("pool", ()))
        print(f"{args.arm} {station} updates={len(records)} with_aifs_seated={pools} "
              f"{time.monotonic() - t0:.1f}s", flush=True)
    remaining = [s for s in stations if not (out / f"{s}.jsonl.gz").exists()]
    print(json.dumps({"arm": args.arm, "remaining": len(remaining), "stations": len(stations)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
