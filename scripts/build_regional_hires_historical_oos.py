"""Phase 1: do the four regional candidates survive a strict out-of-sample gate?

Phase 0 asked whether a regional model's day-ahead forecast is tighter about
its own mean than the distribution production serves, and four station-model
pairs said yes.  Dispersion is a necessary condition and not a sufficient
one: it says the source carries information, not that a distribution built
from it scores better than the one already being served.  That is what the
admission gate asks, in CRPS, NLL and Brier, and it is what this measures.

Nothing is written to the SSOT.  The output is an evidence file, the way the
RKPK historical bootstrap writes one, because the decision this informs is
whether to build a capture lane at all -- and a source that cannot clear the
gate on history will not clear it on live capture either.

Strict as-of throughout.  Each holdout day is scored by parameters fitted
only on days strictly before it, and the fit is production's own
``_fit_parameters`` rather than a second copy: an arm that fits differently
from serving earns a certificate serving cannot honour, which is the failure
the affine-form incident is on record for.
"""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as OOS  # noqa: E402
from bayes_basket_target.center_width_store import (  # noqa: E402
    _truth, serving_cells_by_day,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
OUT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_hires_historical_oos.json"
)
PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"

# The four that cleared Phase 0's dispersion line, and only those.  Widening
# this list is a ruling, not an edit: the eleven that did not clear were
# measured and refused.
CANDIDATES = [
    ("EDDM", "dwd_icon_d2", "ICON-D2 2km"),
    ("LIMC", "italia_meteo_arpae_icon_2i", "ICON-2I 2km"),
    ("EHAM", "dwd_icon_d2", "ICON-D2 2km"),
    ("RCSS", "jma_msm", "JMA MSM 5km"),
]
MIN_TRAIN_DAYS = 20
PAST_DAYS = 90


def _fetch(url: str) -> dict | None:
    for attempt in range(3):
        result = subprocess.run(
            ["curl", "-s", "--max-time", "90", url],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except ValueError:
                pass
        time.sleep(2.0 * (attempt + 1))
    return None


def _distribution(
    centre: float, sigma: float, buckets: dict[str, tuple]
) -> dict[str, float]:
    """A bucket distribution from a point forecast and its residual scale.

    Normal through the bucket edges, which is the shape a single-member
    system is served as: it has no members to place, so its width is the
    width its own record earns it.  Open-ended edge buckets take the whole
    remaining tail, which is what they are.
    """

    scale = max(float(sigma), 0.25)

    def cdf(value: float) -> float:
        return 0.5 * (1.0 + math.erf((value - centre) / (scale * math.sqrt(2.0))))

    out: dict[str, float] = {}
    for key, (lower, upper) in buckets.items():
        low = 0.0 if lower is None else cdf(float(lower))
        high = 1.0 if upper is None else cdf(float(upper))
        out[key] = max(high - low, 0.0)
    total = sum(out.values())
    if total <= 0:
        return {key: 1.0 / len(out) for key in out}
    return {key: value / total for key, value in out.items()}


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=120.0)
    coords = {
        str(r[0]): (float(r[1]), float(r[2]))
        for r in conn.execute(
            "SELECT DISTINCT station_id, latitude, longitude"
            " FROM calibration_station_forecast_physics"
        )
    }
    zones = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT station_id, timezone_name"
            " FROM calibration_station_registry"
        )
    }
    truth = _truth(conn)

    # The bar: the served P0 posterior, scored with the gate's own metrics on
    # the same days.  Bucket geometry comes from the served cell too, so both
    # arms are scored on one axis.
    cells_by_day = serving_cells_by_day(
        conn, truth, stage="P0",
        min_decision_at_utc="2026-08-01T00:00:00Z", pre_band=False,
    )
    served: dict[tuple[str, str], dict] = {}
    for day, cells in cells_by_day.items():
        for cell in cells:
            key = (str(cell["station_id"]), str(day))
            if key not in served:
                served[key] = cell

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat()
        .replace("+00:00", "Z"),
        "lead": "previous_day1",
        "min_train_days": MIN_TRAIN_DAYS,
        "candidates": [],
    }

    for station, model, label in CANDIDATES:
        if station not in coords:
            continue
        lat, lon = coords[station]
        zone = zones.get(station, "UTC")
        data = _fetch(
            f"{PREV}?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m_previous_day1&models={model}"
            f"&past_days={PAST_DAYS}&forecast_days=1&timezone={quote(zone)}"
        )
        time.sleep(1.0)
        if not data or "hourly" not in data:
            print(f"  {station}: 拉取失败")
            continue
        hourly = data["hourly"]
        series = (
            hourly.get("temperature_2m_previous_day1")
            or hourly.get(f"temperature_2m_previous_day1_{model}")
        )
        if not series:
            print(f"  {station}: 无 previous_day1")
            continue
        daily: dict[str, float] = {}
        for stamp, value in zip(hourly.get("time") or [], series):
            if value is None:
                continue
            day = str(stamp)[:10]
            daily[day] = max(daily.get(day, -99.0), float(value))

        # Cases in the shape ``_fit_parameters`` consumes: a single-member
        # system contributes one value, which is what it is.
        cases = [
            {
                "event_date": day,
                "values": [value],
                "actual_tmax_c": truth[(station, day)],
            }
            for day, value in sorted(daily.items())
            if (station, day) in truth
        ]
        scored = []
        for index, case in enumerate(cases):
            history = cases[:index]
            if len(history) < MIN_TRAIN_DAYS:
                continue
            day = case["event_date"]
            cell = served.get((station, day))
            if cell is None:
                continue
            intervals = {}
            try:
                keys = list(cell["ordered_keys"])
                lineage_intervals = cell.get("bucket_intervals")
                if not lineage_intervals:
                    centres = list(cell["key_centers_c"])
                    step = (
                        (centres[-1] - centres[0]) / (len(centres) - 1)
                        if len(centres) > 1 else 1.0
                    )
                    for position, key in enumerate(keys):
                        low = centres[position] - step / 2.0
                        high = centres[position] + step / 2.0
                        intervals[key] = (
                            None if position == 0 else low,
                            None if position == len(keys) - 1 else high,
                        )
                else:
                    intervals = {
                        k: (v[0], v[1])
                        for k, v in lineage_intervals.items()
                    }
            except (KeyError, IndexError, TypeError):
                continue
            try:
                fitted = OOS._fit_parameters(history)
            except Exception:
                continue
            centre = case["values"][0] + float(fitted["bias_c"])
            sigma = float(
                fitted.get("train_residual_sigma_c")
                or fitted.get("train_rmse_c") or 1.0
            )
            candidate = _distribution(centre, sigma, intervals)
            incumbent = {
                str(k): float(v)
                for k, v in cell["probabilities"].items()
            }
            actual = truth[(station, day)]
            landed = cell["settled_bucket"]

            def score(dist):
                p = float(dist.get(landed, 0.0))
                brier = sum(
                    (float(dist.get(k, 0.0)) - (1.0 if k == landed else 0.0))
                    ** 2 for k in set(dist) | {landed}
                )
                order = sorted(
                    intervals, key=lambda k: keys.index(k)
                )
                crps = 0.0
                seen = 0.0
                hit = False
                for k in order:
                    seen += float(dist.get(k, 0.0))
                    if k == landed:
                        hit = True
                    crps += (seen - (1.0 if hit else 0.0)) ** 2
                return {
                    "nll": -math.log(max(p, 1e-12)),
                    "brier": brier, "crps": crps,
                }

            scored.append({
                "event_date": day,
                "candidate": score(candidate),
                "incumbent": score(incumbent),
                "residual_c": actual - centre,
                "train_days": len(history),
            })

        if not scored:
            print(f"  {station}: 无可评分的留出日")
            continue
        summary = {}
        for arm in ("candidate", "incumbent"):
            for metric in ("nll", "brier", "crps"):
                summary[f"{arm}_{metric}"] = mean(
                    row[arm][metric] for row in scored
                )
        wins = {
            metric: sum(
                1 for row in scored
                if row["candidate"][metric] < row["incumbent"][metric]
            )
            for metric in ("nll", "brier", "crps")
        }
        gates = {
            metric: summary[f"candidate_{metric}"]
            < summary[f"incumbent_{metric}"]
            for metric in ("nll", "brier", "crps")
        }
        entry = {
            "station_id": station, "model": model, "label": label,
            "holdout_days": len(scored),
            "residual_dispersion_c": pstdev(
                [row["residual_c"] for row in scored]
            ),
            "metrics": summary, "daily_wins": wins, "gates": gates,
            "passes_all_three": all(gates.values()),
            # Kept per day so the gate can be re-asked with the best days
            # removed.  A mean difference this large beside a daily win rate
            # this close to a coin flip is the signature of a handful of days
            # carrying it, and this repository does not accept a result that
            # has not survived dropping them.
            "daily": scored,
        }
        report["candidates"].append(entry)
        print(
            f"  {station:6s} {label:16s} 留出{len(scored):>3d}日"
            f"  CRPS {summary['candidate_crps']:.3f}"
            f"/{summary['incumbent_crps']:.3f}"
            f"  NLL {summary['candidate_nll']:.3f}"
            f"/{summary['incumbent_nll']:.3f}"
            f"  Brier {summary['candidate_brier']:.3f}"
            f"/{summary['incumbent_brier']:.3f}"
            f"  逐日胜 {wins['crps']}/{len(scored)}"
            f"  {'✅三门全过' if entry['passes_all_three'] else '✗'}"
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    winners = [c for c in report["candidates"] if c["passes_all_three"]]
    print(f"\n三门全过: {len(winners)} / {len(report['candidates'])}")
    print(f"写入 {OUT}（未落 SSOT）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
