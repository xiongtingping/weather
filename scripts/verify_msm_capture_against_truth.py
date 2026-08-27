"""Does the ranged MSM capture agree with settled truth?

The capture was proven to decode: a real file, a real grid cell, the same
number from two independent paths.  Decoding correctly and forecasting
correctly are different claims, and only the second one matters to whether
this source should be seated.

So each already-settled day is fetched the way production would fetch it --
the previous day's 00Z run, the station's own local day, the maximum over
those hours -- and compared with what the day actually settled at.  Days
still open are reported as waiting rather than skipped, because "not yet" and
"no data" are different answers.

Read-only against the SSOT.  Nothing is registered and nothing is written.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.jma_msm_archive import (  # noqa: E402
    close_client, fetch_day_maximum,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "msm_capture_verification.json"
)
STATION = "RCSS"
LATITUDE, LONGITUDE = 25.0697, 121.5525
UTC_OFFSET_HOURS = 8


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=6)
    parser.add_argument(
        "--event-date", action="append", default=[],
        help="verify these days specifically",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60.0)
    truth = {
        str(row[0]): float(row[1])
        for row in conn.execute(
            "SELECT local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth t"
            " WHERE station_id=? AND revision_no = ("
            "  SELECT MAX(revision_no)"
            "  FROM calibration_station_daily_high_truth x"
            "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date)",
            (STATION,),
        )
    }
    conn.close()

    if args.event_date:
        wanted = [str(day)[:10] for day in args.event_date]
    else:
        today = datetime.now(timezone.utc).date()
        wanted = [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(1, args.days + 1)
        ]

    rows = []
    waiting = []
    try:
        for day in sorted(wanted):
            actual = truth.get(day)
            if actual is None:
                waiting.append(day)
                print(f"  {day}: 尚未结算，等待中")
                continue
            target = date.fromisoformat(day)
            # Day-ahead, the way P0 would have it: the previous day's 00Z run.
            run = datetime(
                target.year, target.month, target.day,
                tzinfo=timezone.utc,
            ) - timedelta(days=1)
            try:
                captured = fetch_day_maximum(
                    run_init=run, event_date=target,
                    utc_offset_hours=UTC_OFFSET_HOURS,
                    latitude=LATITUDE, longitude=LONGITUDE,
                )
            except ContractError as exc:
                print(f"  {day}: 采集失败 {exc}")
                continue
            error = actual - captured["forecast_tmax_c"]
            rows.append({
                "event_date": day,
                "run_init_utc": captured["run_init_utc"],
                "forecast_tmax_c": captured["forecast_tmax_c"],
                "official_tmax_c": actual,
                "error_c": error,
                "hours_complete": captured["complete"],
                "hours_covered": len(captured["hours_covered"]),
                "bytes_downloaded": captured["bytes_downloaded"],
            })
            print(
                f"  {day}: 预报 {captured['forecast_tmax_c']:.2f}°C"
                f"  真值 {actual:.2f}°C"
                f"  误差 {error:+.2f}°C"
                f"  时次 {len(captured['hours_covered'])}"
                f"/{len(captured['hours_wanted'])}"
                f"{'' if captured['complete'] else ' ⚠️不完整'}"
            )
    finally:
        close_client()

    report = {
        "station_id": STATION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat()
        .replace("+00:00", "Z"),
        "verified": rows,
        "waiting_for_settlement": waiting,
    }
    if rows:
        errors = [row["error_c"] for row in rows]
        report["summary"] = {
            "n": len(errors),
            "bias_c": mean(errors),
            "dispersion_c": pstdev(errors) if len(errors) > 1 else 0.0,
            "mae_c": mean(abs(value) for value in errors),
            "max_abs_error_c": max(abs(value) for value in errors),
        }
        print(
            f"\n{len(errors)} 日: 偏置 {mean(errors):+.2f}°C"
            f"  散度 {report['summary']['dispersion_c']:.2f}°C"
            f"  MAE {report['summary']['mae_c']:.2f}°C"
            f"  最大 {report['summary']['max_abs_error_c']:.2f}°C"
        )
        # A capture that decodes the wrong cell, or the wrong hours, does not
        # look like a forecast -- it looks like noise or like another place.
        # Phase 1 measured this source's day-ahead dispersion at 1.56C, so a
        # capture agreeing with that is capturing what was scored.
        expected = 1.56
        ratio = report["summary"]["dispersion_c"] / expected
        report["summary"]["phase1_dispersion_c"] = expected
        report["summary"]["dispersion_ratio"] = ratio
        verdict = (
            "✅ 与 Phase 1 量到的散度一致，采集的是被打分的那个东西"
            if 0.6 <= ratio <= 1.6
            else "⚠️ 与 Phase 1 散度不符，采集口径可能有别"
        )
        print(f"  Phase 1 散度 {expected:.2f}°C，比值 {ratio:.2f}  {verdict}")
    if waiting:
        print(f"\n等待结算: {', '.join(waiting)}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(f"写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
