"""Admission of the weekly refit of the board's composite ΔT card (user 2026-10-28 「加准入门」).

Until then every Monday's fit replaced the card unconditionally; the forward record scored a
version only after it had been shown for a week.  Now a new fit is written only when:

1. probe beats the day-ahead forecast as it stands: the same procedure fitted on the store
   less its newest ``HOLD_DAYS`` closed days, scored on those days, has a lower MAE than
   ΔT = 0 at the judged hours 9/11/13/15/17 (the procedure still adds something);
2. probe is not worse than the card in use: on the same days and hours, its mean Student-t
   log score is at most ``TOLERANCE_NAT`` above the card's.  The card in use was fitted on
   part of those days, so it scores them partly in-sample; the tolerance covers that
   optimism (measured on 2026-10-28, see ``RESULTS_GATE.md``) and nothing more;
3. the new rows did not blow the fit up: at every hour the full fit's fleet residual SD is at
   most ``RESID_SD_RATIO`` times the probe's.

The full fit (all rows) is what gets written; the probe only judges.  Every run, admitted or
not, writes ``data/runtime/tmax_composite/refit_gate.json``.  Display only, as the card.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from scipy import stats

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import forward  # noqa: E402

from bayes_basket_target import station_day_board as board  # noqa: E402

HOLD_DAYS = 14
JUDGED_HOURS = (9, 11, 13, 15, 17)
TOLERANCE_NAT = 0.02
RESID_SD_RATIO = 1.2
RECEIPT = forward.RUNTIME / "refit_gate.json"
INCUMBENT = forward.RUNTIME / "tmax_composite_shift_v1.json"


def t_logscore(y: float, mu: float, sd: float, df: float | None) -> float:
    """Negative log density of the card's Student-t (variance sd^2), nat."""

    if not df or df <= 2:
        return 0.5 * math.log(2 * math.pi * sd ** 2) + 0.5 * ((y - mu) / sd) ** 2
    scale = sd * math.sqrt((df - 2) / df)
    return -float(stats.t.logpdf((y - mu) / scale, df)) + math.log(scale)


def score(rows: list[dict], fit: dict[str, Any]) -> dict[tuple[str, str, int], tuple[float, float, float]]:
    """(station, date, hour) -> (y, ΔT, log score) for every hour the fit gives a value."""

    out = {}
    for r in rows:
        br = {int(x["hour"]): x for x in forward.board_rows(r)}
        y = r["tmax"] - r["tmax_fc"]
        for H in sorted(int(h) for h in fit["by_hour"]):
            p = board._composite_at(br, r["station"], fit, H)
            if p is None or not p.get("resid_sd_c"):
                continue
            out[(r["station"], r["date"], H)] = (y, p["total_c"], t_logscore(y, p["total_c"], p["resid_sd_c"], p.get("t_df")))
    return out


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def judge(rows: list[dict], full: dict[str, Any], build: Callable[[list[dict]], dict]) -> dict[str, Any]:
    dates = sorted({r["date"] for r in rows})
    held = set(dates[-HOLD_DAYS:])
    hold_rows = [r for r in rows if r["date"] in held]
    probe = build([r for r in rows if r["date"] not in held])
    ps = score(hold_rows, probe)
    try:
        incumbent = json.loads(INCUMBENT.read_text())
    except (OSError, ValueError):
        incumbent = None
    inc = score(hold_rows, incumbent) if incumbent else {}

    judged = [k for k in ps if k[2] in JUDGED_HOURS]
    mae_probe = _mean([abs(ps[k][0] - ps[k][1]) for k in judged])
    mae_raw = _mean([abs(ps[k][0]) for k in judged])
    paired = [k for k in ps if k in inc]
    ls_probe = _mean([ps[k][2] for k in paired])
    ls_inc = _mean([inc[k][2] for k in paired])
    diff = None if ls_probe is None or ls_inc is None else round(ls_probe - ls_inc, 4)
    sd_ratio = {h: round(full["by_hour"][h]["fleet_resid_sd_c"] / probe["by_hour"][h]["fleet_resid_sd_c"], 3)
                for h in full["by_hour"] if h in probe["by_hour"] and probe["by_hour"][h]["fleet_resid_sd_c"]}
    worst = max(sd_ratio.items(), key=lambda kv: kv[1]) if sd_ratio else None

    if not judged:
        reason = "no held-out station-hour to judge on"
    elif mae_probe >= mae_raw:
        reason = f"probe MAE {mae_probe} C does not beat the day-ahead forecast as it stands ({mae_raw} C) at {list(JUDGED_HOURS)} h"
    elif diff is not None and diff > TOLERANCE_NAT:
        reason = f"probe log score {ls_probe} is {diff:+.4f} nat worse than the card in use ({ls_inc}), tolerance {TOLERANCE_NAT}"
    elif worst and worst[1] > RESID_SD_RATIO:
        reason = f"full fit's {worst[0]} h residual SD is {worst[1]}x the probe's (limit {RESID_SD_RATIO}): the new rows blow the fit up"
    else:
        reason = None
    summary = {
        "judged_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "candidate_version": full.get("version"), "incumbent_version": (incumbent or {}).get("version"),
        "held_out": [min(held), max(held)] if held else None, "held_out_station_days": len(hold_rows),
        "judged_hours": list(JUDGED_HOURS), "mae_probe_c": mae_probe, "mae_day_ahead_c": mae_raw,
        "paired_station_hours": len(paired), "logscore_probe": ls_probe, "logscore_incumbent": ls_inc,
        "logscore_diff": diff, "tolerance_nat": TOLERANCE_NAT,
        "resid_sd_ratio_worst": list(worst) if worst else None, "resid_sd_ratio_limit": RESID_SD_RATIO,
    }
    verdict = {"admitted": reason is None, "reason": reason or "admitted", "summary": summary}
    forward.RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = RECEIPT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"schema": "tmax_composite_refit_gate_v1", **verdict}, ensure_ascii=False, indent=1))
    tmp.replace(RECEIPT)
    return verdict
