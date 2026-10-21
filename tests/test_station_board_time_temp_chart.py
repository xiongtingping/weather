"""The board's trend charts read time across and temperature up (user
2026-10-18 「两个曲线加时间」「横坐标：时间，纵坐标：温度」「确定性预报：增加
曲线」「P1 概率也是」「P0 概率和P0-M 概率也是」「统一风格」): one function
draws them all."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def _render(body: str) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    script = ("const SD = {data: {timezone: 'Asia/Shanghai'}};\n"
              "const sdEsc = s => String(s == null ? '' : s).replace(/[&<>\"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;'})[c]);\n"
              "const SD_COL = {P0: 'a', 'P0-M': 'b', P1: 'c'};\n"
              "const sdN = (v, d) => Number(v).toFixed(d); const sdSg = (v, d) => (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v).toFixed(d);\n"
              + _function("sdLocal") + _function("sdUnit") + _function("sdAxisOpts") + _function("sdLocalParts") + _function("sdDayStartMs") + _function("sdTimeTempChart") + _function("sdStageTrend") + _function("sdStageLine") + "\n" + body)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_chart_labels_time_across_and_temperature_up() -> None:
    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-18T00:00:00Z', 19.2],"
                  " ['2026-10-18T04:00:00Z', 20.1], ['2026-10-18T08:00:00Z', 19.4]], stroke: 'x'}]));")
    assert re.findall(r">(\d\d:\d\d)<", svg) == [f"{h:02d}:00" for h in range(8, 17)]   # local, hourly at this width
    temps = re.findall(r">(\d+\.\d)°<", svg)
    assert temps[0] == "19.0" and temps[-1] == "20.5" and len(temps) <= 5
    assert "<polyline" in svg
    # fewer than two points: no chart at all
    assert _render("console.log(JSON.stringify(sdTimeTempChart([{pts: [['2026-10-18T00:00:00Z', 19]]}])));").strip() == '""'


def test_each_stage_card_draws_its_own_means() -> None:
    timeline = [{"stage": "P1", "at_utc": f"2026-10-18T0{h}:00:00Z", "stats": {"mean_c": 20 + h / 10}} for h in range(3)]
    timeline.append({"stage": "P0", "at_utc": "2026-10-18T00:00:00Z", "stats": {"mean_c": 30}})
    out = _render(f"const day = {{timeline: {json.dumps(timeline)}}};\n"
                  "console.log(sdStageTrend(day, 'P1')); console.log('|' + sdStageTrend(day, 'P0') + '|');")
    p1, p0 = out.strip().split("\n")
    assert 'stroke="c"' in p1 and "30.0°" not in p1 and p0 == "||"


def test_only_a_bucket_change_gets_a_dot_and_it_says_why() -> None:
    """User 2026-10-18 「曲线上增加曲线变化原因」→「只要涉及温度桶的关键变化，
    不是所有变化」."""

    def e(h, mean, mode, mb, cause):
        return {"stage": "P0", "at_utc": f"2026-10-18T0{h}:00:00Z",
                "stats": {"mean_c": mean, "mode": mode, "mean_bucket": mb}, "cause": cause}
    timeline = [e(0, 21.2, "21°C", "21°C", ["当日首次决策"]),
                e(1, 21.0, "21°C", "21°C", ["GEFS 18Z→00Z"]),        # a run, but no bucket moved
                e(2, 20.68, "21°C", "21°C", ["同一批 run 重算"]),
                e(3, 20.4, "20°C", "20°C", ["AIFS 18Z→00Z"])]
    out = _render(f"const day = {{timeline: {json.dumps(timeline, ensure_ascii=False)}}};\n"
                  "console.log(JSON.stringify(sdStageLine(day, 'P0').marks.map(m => m[2])));")
    marks = json.loads(out.strip())
    assert len(marks) == 1
    assert "众数 21°C→20°C，均值桶 21°C→20°C" in marks[0] and "AIFS 18Z→00Z" in marks[0] and "（−0.28）" in marks[0]


def test_the_f_median_marks_only_a_bucket_crossing_and_survives_one_system() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    day = {"deterministic": {"systems": [
        {"in_f": True, "product": "GFS", "runs": [["2026-10-18T01:00:00Z", 20.2], ["2026-10-18T06:00:00Z", 21.0]]},
        {"in_f": True, "product": "AIFS", "runs": [["2026-10-18T02:00:00Z", 20.4], ["2026-10-18T05:00:00Z", 21.4]]},
        {"in_f": True, "product": "IFS_HRES", "runs": [["2026-10-18T03:00:00Z", 20.3]]}]}}
    script = ("const SD = {data: {timezone: 'UTC'}}; const SD_DET_LABEL = {}; const sdN = (v, d) => Number(v).toFixed(d);\n"
              + _function("sdLocal") + _function("sdDetMedian")
              + f"\nconsole.log(JSON.stringify(sdDetMedian({json.dumps(day)})));"
              + "\nconsole.log(JSON.stringify(sdDetMedian({deterministic: {systems: [{in_f: true, product: 'GFS', runs: [['2026-10-18T01:00:00Z', 20]]}]}})));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    full, lone = (json.loads(x) for x in out.stdout.strip().splitlines())
    assert [round(p[1], 2) for p in full] == [20.3, 20.3, 20.3, 21.0] and lone == []
    assert [p[2] is not None for p in full] == [False, False, False, True] and "中位 20°C→21°C 桶" in full[-1][2]


def test_events_are_numbered_from_one_and_a_flip_back_cancels() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")

    def e(stage, hm, mode, mb, cause):
        return {"stage": stage, "at_utc": f"2026-10-18T{hm}:00Z", "stats": {"mean_c": 20, "mode": mode, "mean_bucket": mb}, "cause": cause}
    timeline = [e("P1", "00:00", "21°C", "21°C", []), e("P1", "01:00", "21°C", "22°C", ["新报文"]),
                e("P1", "06:04", "21°C", "21°C", ["新报文"]), e("P1", "06:04:30".replace(":30", ""), "21°C", "21°C", []),
                e("P0", "07:36", "21°C", "21°C", []), e("P0", "07:40", "20°C", "20°C", ["AIFS 18Z→00Z"]),
                e("P1", "07:40", "20°C", "20°C", ["AIFS 18Z→00Z"])]
    # a same-minute flip and flip-back on P1 at 08:10
    timeline += [e("P1", "08:10", "21°C", "20°C", ["新报文"]), {**e("P1", "08:10", "20°C", "20°C", ["新报文"]), "at_utc": "2026-10-18T08:10:30Z"}]
    script = ("const SD = {data: {timezone: 'UTC'}}; const SD_COL = {P0: 'a', 'P0-M': 'b', P1: 'c'};\n"
              "const sdN = (v, d) => Number(v).toFixed(d); const sdSg = (v, d) => String(v);\n"
              + _function("sdLocal") + _function("sdStageLine") + _function("sdCurveEvents")
              + f"\nconsole.log(JSON.stringify(sdCurveEvents({{timeline: {json.dumps(timeline, ensure_ascii=False)}}}, Date.parse('2026-10-18T02:00:00Z'))));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    ev = json.loads(out.stdout)
    assert [x[3] for x in ev] == [1, 2]
    assert ev[0][1] == "P1 均值桶 22°C→21°C（新报文）"
    assert ev[1][1] == "P0 众数 21°C→20°C，均值桶 21°C→20°C；P1 众数 21°C→20°C，均值桶 21°C→20°C（AIFS 18Z→00Z）"


def test_the_chart_carries_each_labelled_line_for_the_hover() -> None:
    """User 2026-10-18 「鼠标放上去的时候可以看到该时间节点各个曲线的明细对比」."""

    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-18T00:00:00Z', 19.234], ['2026-10-18T04:00:00Z', 20.1]], stroke: 'x', label: 'P0 均值'},"
                  " {pts: [['2026-10-18T01:00:00Z', 19.8], ['2026-10-18T03:00:00Z', 19.9]], stroke: 'y'}]));")
    raw = re.search(r'data-tt="([^"]+)"', svg).group(1).replace("&quot;", '"')
    hover = json.loads(raw)
    assert [s["n"] for s in hover["s"]] == ["P0 均值"]                      # unlabelled lines are not read out
    assert hover["s"][0]["p"] == [[1792281600000, 19.23], [1792296000000, 20.1]]
    assert 'class="sd-tt-x"' in svg


def test_ticks_sit_on_whole_local_hours() -> None:
    """User 2026-10-18 「曲线横坐标按照整点来」: Shanghai, 08:17..15:41 local;
    Kolkata (+05:30) lands on its own whole hours; across midnight the date."""

    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-18T00:17:00Z', 19], ['2026-10-18T07:41:00Z', 20]], stroke: 'x'}]));")
    assert re.findall(r">(\d\d:\d\d)<", svg) == [f"{h:02d}:00" for h in range(9, 16)]
    svg = _render("SD.data.timezone = 'Asia/Kolkata';"
                  "console.log(sdTimeTempChart([{pts: [['2026-10-18T00:00:00Z', 19], ['2026-10-18T04:00:00Z', 20]], stroke: 'x'}]));")
    assert re.findall(r">(\d\d:\d\d)<", svg) == ["06:00", "07:00", "08:00", "09:00"]
    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-17T12:00:00Z', 19], ['2026-10-18T06:00:00Z', 20]], stroke: 'x'}]));")
    labels = re.findall(r'fill="var\(--muted\)"(?: font-weight="700")?>([^<]+)</text>', svg)
    assert "10-18" in labels and all(re.fullmatch(r"\d\d:00|10-18", x) for x in labels if not x.endswith("°"))


def test_a_day_chart_spans_the_local_day_with_every_afternoon_hour() -> None:
    """User 2026-10-18 「当天下午每个整点都要有：12:00-20:00」: Shanghai 10-18;
    a value from the evening before enters at the left edge."""

    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-17T10:00:00Z', 21], ['2026-10-18T03:00:00Z', 20.5],"
                  " ['2026-10-18T09:00:00Z', 19.4]], stroke: 'x', label: 'P1'}], null, null, {day: '2026-10-18'}));")
    labels = re.findall(r'fill="var\(--muted\)">([^<]+)</text>', svg)
    hours = [x for x in labels if not x.endswith("°")]
    # 下午段拉宽, and not stopping at 20:00 (「6卡片的时间也不要固定为20:00」)
    assert hours == ["00:00", "06:00"] + [f"{h}:00" for h in range(12, 24)]
    xs = {lab: float(x) for x, lab in re.findall(r'<text x="([\d.]+)" y="[\d.]+" font-size="[\d.]+" text-anchor="\w+" fill="var\(--muted\)">(\d\d:00)</text>', svg)}
    assert (xs["12:00"] - xs["00:00"]) / (480 - 34 - 8) < 0.25 and (xs["13:00"] - xs["12:00"]) >= 28
    hover = json.loads(re.search(r'data-tt="([^"]+)"', svg).group(1).replace("&quot;", '"'))
    assert hover["t0"] == 1792252800000 and hover["t1"] == 1792339200000        # 10-18 00:00..24:00 +08
    assert hover["s"][0]["p"][0] == [1792252800000, 21]                        # carried in at midnight
    # a day not begun keeps its own span
    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-17T00:00:00Z', 21], ['2026-10-17T06:00:00Z', 20]], stroke: 'x'}],"
                  " null, null, {day: '2026-10-19'}));")
    assert "12</text>" not in svg and "08:00" in svg


def test_the_temperature_grid_is_half_a_degree_c_or_one_degree_f() -> None:
    """User 2026-10-18 「曲线纵坐标0.5度一个刻度。华氏1f为一个刻度」."""

    pts = "[['2026-10-18T00:00:00Z', 15.08], ['2026-10-18T04:00:00Z', 16.4]]"
    svg = _render(f"console.log(sdTimeTempChart([{{pts: {pts}, stroke: 'x'}}]));")
    assert re.findall(r">(\d+\.\d)°<", svg) == ["15.0", "15.5", "16.0", "16.5"]
    svg = _render(f"console.log(sdTimeTempChart([{{pts: {pts}, stroke: 'x', label: 'P1'}}], null, null, {{unit: 'F'}}));")
    assert re.findall(r">(\d+)°F<", svg) == ["59", "60", "61", "62"]      # 15.08 °C = 59.1 °F, 16.4 °C = 61.5 °F
    hover = json.loads(re.search(r'data-tt="([^"]+)"', svg).group(1).replace("&quot;", '"'))
    assert hover["f"] == 1


def test_the_summary_chart_scrolls_hour_by_hour_into_the_past() -> None:
    """User 2026-10-18 「曲线汇总：20:00后的也增加，连续24小时。可左右滑动看到
    过去的曲线走势」: every hour of the day and the days before, at a fixed
    width per hour, at most three days back."""

    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-13T00:00:00Z', 21], ['2026-10-18T09:00:00Z', 19.4]], stroke: 'x', label: 'P1'}],"
                  " null, null, {day: '2026-10-18', scroll: {back: 3, pph: 40}}));")
    width = int(re.search(r'<svg[^>]* width="(\d+)"', svg).group(1))
    assert width == 34 + 8 + 4 * 24 * 40                                         # 10-15 00:00 .. 10-19 00:00 local
    labels = [x for x in re.findall(r'fill="var\(--muted\)"(?: font-weight="700")?>([^<]+)</text>', svg) if not x.endswith("°")]
    assert labels[:3] == ["10-15", "01:00", "02:00"] and labels.count("10-18") == 1
    assert [x for x in labels if x.endswith(":00")][-3:] == ["21:00", "22:00", "23:00"]   # after 20:00 too
    assert "sd-tt-scroll" in svg


def test_a_legend_click_hides_and_shows_its_curve() -> None:
    """User 2026-10-18 「点击对应曲线名字可以显示/隐藏对应曲线」."""

    src = DASHBOARD[DASHBOARD.index("function sdCompareChart("):]
    assert 'data-sd-cmpk="${sdEsc(x.k)}"' in src and "const shown = series.filter(x => !hid[x.k]);" in src
    click = _function("sdOnClick")
    assert "SD.cmpHidden[ds.sdCmpk] = !SD.cmpHidden[ds.sdCmpk];" in click and "box.outerHTML = sdCompareChart(day)" in click


def test_the_trend_is_the_median_and_middle_half_of_the_curves_standing() -> None:
    """User 2026-10-18 「把7个曲线提炼为一个趋势背景」."""

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    series = [{"pts": [["2026-10-18T00:00:00Z", v], ["2026-10-18T06:00:00Z", v + 1]]} for v in (19, 20, 21, 22)]
    series.append({"pts": [["2026-10-18T03:00:00Z", 30], ["2026-10-18T06:00:00Z", 30]]})      # joins later, an outlier
    script = _function("sdTrendOf") + f"\nconsole.log(JSON.stringify(sdTrendOf({json.dumps(series)})));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    tr = json.loads(out.stdout)
    at = {p[0][11:16]: p[1] for p in tr["mid"]}
    assert abs(at["00:00"] - 20.5) < 1e-9                     # four curves: median of 19..22 (smoothing sees the next hour too)
    assert 20.5 <= at["04:30"] <= 21.0                        # the outlier moves the median by one place, not to 30
    lo, hi = tr["band"][0][1], tr["band"][0][2]
    assert lo < at["00:00"] < hi
    assert sdTrend_needs_three(node)


def sdTrend_needs_three(node) -> bool:
    script = _function("sdTrendOf") + "\nconsole.log(JSON.stringify(sdTrendOf([{pts: [['2026-10-18T00:00:00Z', 1], ['2026-10-18T01:00:00Z', 2]]}, {pts: [['2026-10-18T00:00:00Z', 1], ['2026-10-18T01:00:00Z', 2]]}])));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() == "null"


def test_the_chart_has_one_hover_box_not_native_tooltips_on_top() -> None:
    """User 2026-10-18 「悬浮双文字说明有折叠，去掉前面这层」: the dots and the
    numbered moments carry no <title>; the dot's note rides in the hover data."""

    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-18T00:00:00Z', 21], ['2026-10-18T04:00:00Z', 20]], stroke: 'x', label: 'P0',"
                  " marks: [['2026-10-18T04:00:00Z', 20, 'P0 众数 21°C→20°C\\n原因：AIFS 18Z→00Z']]}], null, null,"
                  " {events: [['2026-10-18T04:00:00Z', 'P0 众数 21°C→20°C（AIFS 18Z→00Z）', 'c', 1]]}));")
    assert "<title>" not in svg and "<circle" in svg
    hover = json.loads(re.search(r'data-tt="([^"]+)"', svg).group(1).replace("&quot;", '"'))
    assert hover["s"][0]["m"] == [[1792296000000, "P0 众数 21°C→20°C\n原因：AIFS 18Z→00Z"]]
    assert hover["e"][0][1].startswith("P0 众数")


def test_a_day_ahead_runs_on_to_the_end_of_that_day() -> None:
    """User 2026-10-18 「明日视角的横坐标时间为什么停留在18:00？」: D+1's readings
    all lie before it; the summary still runs through 23:00 of that day."""

    svg = _render("console.log(sdTimeTempChart([{pts: [['2026-10-17T00:00:00Z', 23], ['2026-10-18T10:00:00Z', 23.4]], stroke: 'x', label: 'P0'}],"
                  " null, null, {day: '2026-10-19', scroll: {back: 3, pph: 40}}));")
    hover = json.loads(re.search(r'data-tt="([^"]+)"', svg).group(1).replace("&quot;", '"'))
    assert hover["t1"] == 1792425600000                       # 10-20 00:00 +08, the end of 10-19
    labels = [x for x in re.findall(r'fill="var\(--muted\)"(?: font-weight="700")?>([^<]+)</text>', svg) if not x.endswith("°")]
    assert "10-19" in labels and labels[-1] == "23:00"


def test_bucket_edges_are_bold_and_buckets_named() -> None:
    """User 2026-10-19 「纵坐标的温度，把关键节点的温度标粗，比如23.5，突破23.5是24,
    低于23.5是23」: the ladder's edges drawn and labelled in bold, the bucket
    names between them."""

    axis = [{"bucket": "22°C or below", "lo_c": None, "hi_c": 22.5}, {"bucket": "23°C", "lo_c": 22.5, "hi_c": 23.5},
            {"bucket": "24°C", "lo_c": 23.5, "hi_c": 24.5}, {"bucket": "25°C or higher", "lo_c": 24.5, "hi_c": None}]
    svg = _render(f"console.log(sdTimeTempChart([{{pts: [['2026-10-18T00:00:00Z', 22.8], ['2026-10-18T04:00:00Z', 23.9]], stroke: 'x'}}],"
                  f" null, null, {{axis: {json.dumps(axis)}}}));")
    bold = re.findall(r'font-weight="800"[^>]*>([^<]+)</text>', svg)
    assert bold == ["22.5°", "23.5°"]                           # the range rounds out to 22.5..24.0; 24.5 is outside
    names = re.findall(r'font-weight="700" text-anchor="end" fill="var\(--muted\)" opacity=".75">([^<]+)</text>', svg)
    assert names == ["23°C", "24°C"]
    plain = re.findall(r'fill="var\(--muted\)">(\d+\.\d)°</text>', svg)
    assert "23.5" not in plain and "22.5" not in plain         # the bold label replaces the plain one


def test_the_summary_chart_carries_metar_and_the_taf_tx_as_steps() -> None:
    """User 2026-10-21 「增加metar、TAF TX，去掉：均值 ± 1σ 走势」."""

    caps = [{"available_at_utc": "2026-10-21T05:10:00Z", "issued_at_utc": "2026-10-21T05:00:00Z", "tx_available": True, "tx_c": 22},
            {"available_at_utc": "2026-10-21T02:10:00Z", "issued_at_utc": "2026-10-21T02:00:00Z", "tx_available": True, "tx_c": 21},
            {"available_at_utc": "2026-10-21T03:10:00Z", "issued_at_utc": "2026-10-21T03:00:00Z", "tx_available": False, "tx_c": None}]
    out = json.loads(_render(_function("sdTxSteps") + f"\nconst day = {{taf: {{captures: {json.dumps(caps)}}}}};"
                             "\nconsole.log(JSON.stringify([sdTxSteps(day), sdTxSteps(day, true)]));"))
    steps, marks = out
    assert [p[1] for p in steps] == [21, 21, 22, 22]               # 21 until the TAF without TX, then 22 on to now
    assert steps[1][0] == "2026-10-21T03:10:00Z" and steps[2][0] == "2026-10-21T05:10:00Z"
    caps.append({"available_at_utc": "2026-10-21T08:10:00Z", "issued_at_utc": "2026-10-21T08:00:00Z", "tx_available": False, "tx_c": None})
    ended = json.loads(_render(_function("sdTxSteps") + f"\nconsole.log(JSON.stringify(sdTxSteps({{taf: {{captures: {json.dumps(caps)}}}}})));"))
    assert ended[-1] == ["2026-10-21T08:10:00Z", 22]                # the newest TAF has no TX: the line stops there
    assert [m[1] for m in marks] == [21, 22] and "TX 22°C" in marks[1][2]
    src = _function("sdCompareChart")
    assert '"METAR 实测"' in src and '"TAF TX"' in src
    assert "sdTrendOf(shown.filter(x => !x.obs))" in src           # evidence, not an estimate: out of the trend
    assert "均值 ± 1σ" not in DASHBOARD and "function sdMeanTrend" not in DASHBOARD
