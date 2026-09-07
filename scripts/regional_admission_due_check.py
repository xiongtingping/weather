#!/usr/bin/env python3
"""Ask the serving gate about a regional lane the moment it has enough to answer.

Two lanes are refused and two have never been asked.  ICON-EU and CMA GRAPES
keep no history and accumulate forward only, so on 2026-09-07 they carried
seven and eight settled station-days against the forty-six the MSM was
admitted on -- not a verdict, an empty docket.  A question that becomes
answerable in a week is a question nobody remembers to ask, so this asks it.

It is deliberately not a reminder.  Each run counts the material a lane
actually has, and either records that it is still short or runs
``measure_regional_serving_value.py`` and writes down what came back.  The
receipt is a JSONL file rather than a log line because the point is to be able
to read, later, what the gate said on the day it first had enough -- including
the days it said nothing, which is how a lane that never accumulates gets
noticed instead of silently waiting forever.

Admission stays a ruling.  This writes a reading; it changes no serving list.

    python3 scripts/regional_admission_due_check.py
    python3 scripts/regional_admission_due_check.py --system CMA_GRAPES_GLOBAL_POINT
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
RECEIPT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_admission_due_check.jsonl"
)
#: The MSM was admitted on 46 station-days.  Thirty is the point at which the
#: station-day bootstrap in the gate has something to resample; below it the
#: interval is wide enough that "straddles zero" says nothing either way.
DEFAULT_MIN_STATION_DAYS = 30
#: Asked about by default: the two lanes that have never been measured at all.
#: HRRR and the NBM are refused on readings, which is a different state -- a
#: refusal is re-opened by a ruling, not by a cron job.
DEFAULT_SYSTEMS = ("CMA_GRAPES_GLOBAL_POINT", "DWD_ICON_EU_POINT")


def material(conn: sqlite3.Connection, system_id: str) -> dict:
    """Station-days this lane has that also have a settled truth."""

    lane = {
        (str(r[0]), str(r[1])) for r in conn.execute(
            "SELECT DISTINCT station_id,target_local_date"
            " FROM calibration_regional_point_run WHERE ensemble_system_id=?",
            (system_id,),
        )
    }
    truth = {
        (str(r[0]), str(r[1])) for r in conn.execute(
            "SELECT station_id,local_date FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND source_lane='AWC'"
        )
    }
    both = sorted(lane & truth)
    return {
        "station_days_with_truth": len(both),
        "stations": len({station for station, _ in both}),
        "from": both[0][1] if both else None,
        "through": both[-1][1] if both else None,
    }


def classify_verdict(stdout: str, returncode: int) -> str:
    """What the gate said, in one word.

    The gate says "nothing to measure" in two different sentences: one per
    system when other systems had rows, and one for the whole run when none
    did.  Reading only the first recorded a REFUSED verdict for a lane that
    had simply not accumulated yet -- a false verdict is worse here than no
    verdict, because the whole point of this receipt is to be believed later.
    The gate also exits non-zero on the empty case, which is the belt to that
    pair of braces.
    """

    if (
        "无可比前缀" in stdout
        or "没有可比前缀" in stdout
        or returncode != 0
    ):
        return "NO_COMPARABLE_PREFIX"
    return "ADMISSIBLE" if "有增益，可接" in stdout else "REFUSED"


def ask_the_gate(system_id: str, start: str, through: str) -> dict:
    """Run the gate and keep what it said, verbatim enough to act on."""

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "measure_regional_serving_value.py"),
            "--system", system_id, "--from", start, "--through", through,
        ],
        capture_output=True, text=True, timeout=3600,
    )
    out = completed.stdout
    verdict = classify_verdict(out, completed.returncode)
    clustered = re.search(r"站日均值差 ([+-][\d.]+) · 95% 区间 \[([^\]]+)\]", out)
    return {
        "verdict": verdict,
        "exit_code": completed.returncode,
        "station_day_mean_delta": clustered.group(1) if clustered else None,
        "interval": clustered.group(2) if clustered else None,
        "stdout_tail": out.strip().splitlines()[-14:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--system", action="append", default=None)
    parser.add_argument(
        "--min-station-days", type=int, default=DEFAULT_MIN_STATION_DAYS
    )
    parser.add_argument("--receipt", type=Path, default=RECEIPT)
    parser.add_argument("--now-utc", default=None, help="for replay")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=300)
    now = args.now_utc or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    for system_id in (args.system or list(DEFAULT_SYSTEMS)):
        have = material(conn, system_id)
        row = {
            "schema": "regional_admission_due_check_v1",
            "checked_at_utc": now,
            "ensemble_system_id": system_id,
            "minimum_station_days": args.min_station_days,
            **have,
        }
        if have["station_days_with_truth"] < args.min_station_days:
            row["state"] = "NOT_DUE"
            print(
                f"{system_id}: 还差素材 "
                f"{have['station_days_with_truth']}/{args.min_station_days} 站日"
                f"（{have['stations']} 站，{have['from']}..{have['through']}）"
            )
        else:
            row["state"] = "ASKED"
            row["gate"] = ask_the_gate(
                system_id, str(have["from"]), str(have["through"])
            )
            print(
                f"{system_id}: 素材够了（{have['station_days_with_truth']} 站日），"
                f"闸的判定＝{row['gate']['verdict']}"
                + (
                    f"，站日均值差 {row['gate']['station_day_mean_delta']}"
                    f" 区间 [{row['gate']['interval']}]"
                    if row["gate"]["station_day_mean_delta"] else ""
                )
            )
            for line in row["gate"]["stdout_tail"]:
                print("   ", line)
        with args.receipt.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
