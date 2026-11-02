"""The member-trajectory Bayes estimate on the station board (user 2026-10-16
「在站点看板里：前端增加贝叶斯更新的温度预估显示」): members that matched the
morning's reports carry the weight, the high is the running high or what is
still to come, and the board only names the stored reading."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bayes_basket_target import member_bayes_readings as MB
from bayes_basket_target.station_day_board import ladder_axis


def _axis():
    keys = [f"{t}°C" for t in range(18, 25)]
    return ladder_axis({k: i for i, k in enumerate(keys)}, {k: [17.5 + i, 18.5 + i] for i, k in enumerate(keys)})


def _row(members, times, kind="t2m_instant"):
    return {"valid_times_json": json.dumps(times), "members_json": json.dumps(members), "value_kind": kind}


def test_the_member_that_matched_the_reports_carries_the_weight():
    times = ["2026-10-16T00:00:00Z", "2026-10-16T03:00:00Z", "2026-10-16T06:00:00Z", "2026-10-16T09:00:00Z"]
    # a cold member that matches the 18-19 C morning and peaks at 20, a warm one that peaks at 23
    members = {"cold": [18.0, 19.0, 20.0, 19.0], "warm": [20.5, 21.5, 23.0, 21.0]}
    t = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    reports = [(t("2026-10-16T01:00:00Z"), 18.0), (t("2026-10-16T02:00:00Z"), 19.0)]
    now = t("2026-10-16T02:30:00Z")
    up = MB.system_update(_row(members, times), 0.0, reports, now, 19.0)
    w = dict(zip(sorted(members), up["weights"]))
    # misfit of ~2.5 C per report against tau 1.5: the warm member keeps about 6 % of the cold one's weight
    assert w["cold"] == 1.0 and 0.03 < w["warm"] < 0.1
    assert sorted(up["highs"]) == [20.0, 23.0]            # the steps still to come
    assert up["reports_compared"] == 2 and 1.0 < up["ess"] < 1.2
    # bias shifts the trajectories before comparing
    up2 = MB.system_update(_row(members, times), -2.5, reports, now, 19.0)
    w2 = dict(zip(sorted(members), up2["weights"]))
    assert w2["warm"] > w2["cold"]


def test_dress_puts_nothing_below_the_running_high_and_sums_to_one():
    axis = _axis()
    vec = MB.dress([20.0, 22.0], [0.75, 0.25], axis, 0.5, "20°C")
    assert sum(vec.values()) == pytest.approx(1.0)
    assert vec["18°C"] == vec["19°C"] == 0.0
    s = MB.summary(vec, axis)
    assert s["mode"] == "20°C" and 20.0 < s["mean_c"] < 21.0


def test_card_names_the_stored_reading():
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE
    start = source.index("const SD_MB_STATE")
    fn = source.index("function sdMemberBayes(")
    depth = 0
    for index in range(source.index("{", fn), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    day = {"member_bayes": {
        "state": "ok", "tau_c": 1.5, "as_of_utc": "x", "reports_in_window": 6, "latest_report_utc": "y",
        "belief_stage": "P1", "posterior": {"mean_c": 21.4, "mode": "21°C", "mode_p": 0.52},
        "prior": {"mean_c": 22.6}, "belief": {"mean_c": 22.3}, "belief_x_members": {"mode": "21°C", "mode_p": 0.41},
        "systems": [{"system": "NOAA_GEFS_05", "run_init_utc": "2026-10-16T00:00:00Z", "members": 31, "ess": 6.2,
                     "bias_c": -0.4, "bias_from_p0": True, "reports_compared": 6, "best_rmse_c": 0.4,
                     "pool_weight": 0.5, "value_kind": "t2m_instant", "prior_mean_c": 22.5, "post_mean_c": 21.3}],
        "history": [{"as_of_utc": "2026-10-16T02:00:00Z", "post_mean_c": 22.0, "prior_mean_c": 22.6},
                    {"as_of_utc": "2026-10-16T03:00:00Z", "post_mean_c": 21.4, "prior_mean_c": 22.6}]}}
    chart = source.index("function sdAxisOpts(")
    chart_end = source.index("function sdD0Trend(")
    script = ("const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n"
              "const sdAgeSpan = (at, cls) => at ? `<span class='${cls}'>${at}</span>` : '—';\n"
              "const SD_COL = {P0: 'a', 'P0-M': 'b', P1: 'c', MKT: 'd', DET: 'e', MB: 'f', D0: 'g'};\n"
              "const sdN = (v, d) => v == null ? '—' : Number(v).toFixed(d);\n"
              "const sdSg = (v, d) => (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(d);\n"
              "const sdPc = (v, d) => (100 * v).toFixed(d) + '%';\n"
              + source[chart:chart_end] + source[start:fn] + source[fn:end] + f"\nconsole.log(sdMemberBayes({json.dumps(day, ensure_ascii=False)}));"
              + "\nconsole.log(sdMemberBayes({member_bayes: {state: 'no_trajectories'}}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "21.40°C" in out and "众数 <b>21°C</b>" in out and "比先验 −1.20°C" in out and "比 P1 <b>−0.90°C</b>" in out
    assert "P1×成员 21°C" in out and ">GEFS<" in out and "有效 6/31" in out and "<polyline" in out
    assert "还没有成员轨迹" in out
