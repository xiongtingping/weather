"""The board's one-station panel: shape statistics on a ladder (user 2026-10-05)."""

from __future__ import annotations

import math

from bayes_basket_target.station_day_board import bucket_of, ladder_axis, vector_stats


def _axis():
    ordinals = {"20°C or below": 0, "21°C": 1, "22°C": 2, "23°C or higher": 3}
    intervals = {"20°C or below": [None, 20.5], "21°C": [20.5, 21.5],
                 "22°C": [21.5, 22.5], "23°C or higher": [22.5, None]}
    return ladder_axis(ordinals, intervals)


def test_open_edges_sit_where_production_places_them() -> None:
    # User 2026-10-09「统一」: production's axis, an open edge 1°C inside.
    axis = _axis()
    assert [r["centre_c"] for r in axis] == [19.5, 21.0, 22.0, 23.5]
    assert bucket_of(axis, 21.49) == "21°C"
    assert bucket_of(axis, 30.0) == "23°C or higher"


def test_board_mean_is_the_served_stage_anchor() -> None:
    """The panel's 分布均值 and the P0-M / P1 served centre are one number on
    one vector (ZUUU 2026-10-10 P0-M read 21.67 beside 21.66 before)."""

    from bayes_basket_target.materialization import _bucket_centers_c, _stage_anchor

    ordinals = {"20°C or below": 0, "21°C": 1, "22°C": 2, "23°C or higher": 3}
    intervals = {"20°C or below": [None, 20.5], "21°C": [20.5, 21.5],
                 "22°C": [21.5, 22.5], "23°C or higher": [22.5, None]}
    vector = {"20°C or below": 0.2, "21°C": 0.35, "22°C": 0.3, "23°C or higher": 0.15}
    import json
    keys, centres = _bucket_centers_c({"bucket_intervals_c_json": json.dumps(intervals)}, bucket_ordinals=ordinals)
    served = _stage_anchor(vector, bucket_ordinals=ordinals, centers_c=dict(zip(keys, centres)),
                           upper_bounds_c={b: v[1] for b, v in intervals.items()}, excluded=set())
    assert math.isclose(vector_stats(vector, ladder_axis(ordinals, intervals))["mean_c"], round(served[1], 3))


def test_mean_variance_mode_and_spread() -> None:
    axis = _axis()
    s = vector_stats({"20°C or below": 0.1, "21°C": 0.4, "22°C": 0.4, "23°C or higher": 0.1}, axis)
    assert math.isclose(s["mean_c"], 21.5)
    assert math.isclose(s["var_c2"], 1.0, abs_tol=1e-9)
    # A tie goes to the lower rung, as the market mode does (charter 249).
    # The mean ordinal 1.5 sits between two equal rungs: production's centre
    # bucket takes the heavier, then the lower (charter 75).
    assert s["mode"] == "21°C" and s["mean_bucket"] == "21°C"
    assert s["skew_sign"] == 1 and math.isclose(s["skew_gap_buckets"], 0.5)
    assert s["interval80"] == ["20°C or below", "22°C"]
    assert math.isclose(s["effective_rungs"], 2 ** s["entropy_bits"], rel_tol=1e-2)


def test_skew_is_read_by_probability_as_production_reads_it() -> None:
    """User 2026-10-11 「不要按照均值桶和众数桶为一个桶统计，而是按照概率来区分」:
    the mean on the mode's rung can still lean past 0.35 sigma."""

    from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1

    axis = _axis()
    leaning = {"20°C or below": 0.2, "21°C": 0.1, "22°C": 0.6, "23°C or higher": 0.1}
    s = vector_stats(leaning, axis)
    assert s["mode"] == s["mean_bucket"] == "22°C" and s["skew_sign"] == -1
    reading = mean_mode_skew_reading_v1(leaning, {r["bucket"]: r["ordinal"] for r in axis})
    assert (s["skew_gap_buckets"], s["skew_threshold_buckets"]) == (reading["gap_buckets"], reading["threshold_buckets"])
    level = vector_stats({"20°C or below": 0.05, "21°C": 0.25, "22°C": 0.45, "23°C or higher": 0.25}, axis)
    assert level["skew_sign"] == 0


def test_unnormalised_vectors_are_normalised() -> None:
    s = vector_stats({"21°C": 2.0, "22°C": 2.0}, _axis())
    assert math.isclose(s["mode_p"], 0.5)


def test_today_is_past_its_peak_only_once_the_reading_turns_down(monkeypatch) -> None:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    import bayes_basket_target.station_day_board as B

    zone = ZoneInfo("Asia/Shanghai")
    monkeypatch.setattr(B, "_model_peak", lambda *a: {"peak_at_utc": "2026-10-05T06:00:00Z"})

    def reports(*temps):
        return lambda *a: [{"valid_at_utc": at, "temp_c": t, "local": ""} for at, t in temps]

    now = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(B, "_metar", reports(("2026-10-05T06:00:00Z", 21.0), ("2026-10-05T10:00:00Z", 20.0)))
    assert B.past_peak(None, "ZUUU", "2026-10-05", zone, now)["past"] is True
    # Still at the high after the peak hour: the day may yet warm.
    monkeypatch.setattr(B, "_metar", reports(("2026-10-05T06:00:00Z", 20.0), ("2026-10-05T10:00:00Z", 21.0)))
    assert B.past_peak(None, "ZUUU", "2026-10-05", zone, now)["past"] is False
    # Before the peak hour nothing is hidden.
    early = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(B, "_metar", reports(("2026-10-05T01:00:00Z", 19.0), ("2026-10-05T03:00:00Z", 18.0)))
    assert B.past_peak(None, "ZUUU", "2026-10-05", zone, early)["past"] is False


def test_clear_sky_radiation_follows_the_sun() -> None:
    from datetime import datetime, timezone

    import bayes_basket_target.station_day_board as B

    noon = B._clear_sky_ghi(30.576, 103.95, datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc))   # 13:00 local
    night = B._clear_sky_ghi(30.576, 103.95, datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc))
    assert 700 < noon < 900 and night == 0.0


def test_goes_hours_zero_the_night_skip_the_unpublished_and_place_each_reading(monkeypatch) -> None:
    """User 2026-10-09「全部都用卫星实测日照」: the Americas read NOAA GOES DSR.
    Hour h is the scan nearest h-1:30 local; night hours are zero without a
    read; a scan not public yet is not asked for; readings land on their hour."""

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bayes_basket_target import goes_surface_radiation as goes
    from bayes_basket_target import station_day_board as board

    asked: list[datetime] = []

    def fake_measured(station, lat, lon, instants):
        asked.extend(instants)
        # 12:00 local (EDT) is the scan nearest 11:30 local = 15:30Z.
        return {datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc).isoformat(): 640.0}

    monkeypatch.setattr(goes, "measured", fake_measured)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc)  # 13:00 EDT

    monkeypatch.setattr(board, "datetime", Clock)
    out = board._goes_hours("KLGA", 40.77945, -73.88027, "2026-10-08", ZoneInfo("America/New_York"))
    values = out["values"]
    assert values[2] == 0.0 and values[22] is None  # night zero; the evening not reached
    assert values[12] == 640.0 and values[11] is None  # placed on its hour; one still pending
    assert all(instant <= datetime(2026, 10, 8, 16, 35, tzinfo=timezone.utc) for instant in asked)
    assert out["pending_hours"] >= 1


def test_madis_five_minute_rain_lands_on_the_hour_its_accumulation_closes(monkeypatch) -> None:
    """MADIS precipAccum is the rain since the last routine report (reset just
    after ~:53): a record before :53 belongs to the hour ending at the next
    top of the hour, one from :53 on to the hour after; VC is not the station."""

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bayes_basket_target import madis_precip_board as madis

    utc = lambda h, m: datetime(2026, 10, 8, h, m, tzinfo=timezone.utc)  # noqa: E731
    rows = [(utc(7, 40), "-RA BR", 0.25), (utc(7, 50), "-RA", 0.51), (utc(7, 55), "-RA", 0.0),
            (utc(8, 20), "VCSH", 0.0), (utc(8, 45), "", 0.0)]
    fresh = madis.time.monotonic()
    for hour in range(0, 24):
        name = f"20261008_{hour:02d}00"
        madis._OBJECTS[name] = (fresh, {"KLGA": [r for r in rows if r[0].hour == hour]})
    monkeypatch.setitem(madis.__dict__, "_PENDING", set())

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(madis, "datetime", Clock)
    monkeypatch.setattr(madis, "_WORKER", type("Alive", (), {"is_alive": lambda self: True})())
    madis._STATIONS.add("KLGA")
    out = madis.station_day("KLGA", "2026-10-08", ZoneInfo("America/New_York"))  # EDT: 07:40Z is 03:40
    assert out["available"]
    assert out["hourly"]["04"]["mm"] == 0.51 and out["hourly"]["04"]["wet"]   # 03:40, 03:50 -> hour ending 04:00
    assert out["hourly"]["05"]["mm"] == 0.0 and out["hourly"]["05"]["wet"]    # 03:55 opens the next window
    assert out["hourly"]["05"]["wx"] == ["-RA"]                                # 04:20 VCSH is not rain here
    assert out["latest"] == {"local": "04:45", "wx": "", "accum_mm": 0.0, "wet": False}
    for hour in range(24):
        madis._OBJECTS.pop(f"20261008_{hour:02d}00", None)


def test_the_picker_groups_by_settlement_timezone() -> None:
    """User 2026-10-10「增加所有站，按照区域分区，比如欧洲、美国、亚洲等」."""

    from bayes_basket_target import station_day_board as B

    assert B._region("ZUUU", "Asia/Shanghai") == "中国"
    assert B._region("RPLL", "Asia/Manila") == "亚洲"
    assert B._region("LTFM", "Europe/Istanbul") == "欧洲"
    assert B._region("KLGA", "America/New_York") == "美国"
    assert B._region("CYYZ", "America/Toronto") == "美洲"
    assert B._region("FACT", "Africa/Johannesburg") == "非洲·大洋洲"
    assert B._region("NZWN", "Pacific/Auckland") == "非洲·大洋洲"


def test_every_contract_city_has_a_display_name() -> None:
    import json

    from bayes_basket_target import station_day_board as B

    cities = json.loads(B.CITY_CONTRACT_PATH.read_text(encoding="utf-8"))["settlement_source_contract"]["cities"]
    names = json.loads(B.CITY_NAMES_PATH.read_text(encoding="utf-8"))["names"]
    assert set(cities) == set(names)


def _formatters(source: str) -> str:
    return "\n".join(line for line in source.splitlines() if line.startswith(("const sdN = ", "const sdSg = "))) + "\n"


def test_mean_mode_card_names_the_side() -> None:
    """The card beside the radiation card (user 2026-10-10): 对称 / 左偏 / 右偏
    from production's skew reading, the head stage in the large type."""

    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE
    formatters = _formatters(source)

    def function(name: str) -> str:
        start = source.index(f"function {name}(")
        depth = 0
        for index in range(source.index("{", start), len(source)):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            if depth == 0:
                return source[start:index + 1]
        raise AssertionError(name)

    day = {"head_stage": "P1", "stages": {
        "P0": {"stats": {"mean_bucket": "24°C", "mode": "24°C", "skew_sign": 0,
                         "skew_gap_buckets": 0.1, "skew_threshold_buckets": 0.38}},
        "P1": {"stats": {"mean_bucket": "24°C", "mode": "24°C", "skew_sign": -1,
                           "skew_gap_buckets": -0.42, "skew_threshold_buckets": 0.4}}},
        "market": {"available": True, "stats": {"mean_bucket": "24°C", "mode": "23°C", "skew_sign": 1,
                                                 "skew_gap_buckets": 0.6, "skew_threshold_buckets": 0.45}}}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\n"
              "const sdEsc = s => String(s == null ? '' : s);\n" + formatters
              + "\n".join(function(n) for n in ("sdSeries", "sdEvidenceCard", "sdShape", "sdShapeRule", "sdMeanModeCard"))
              + f"\nconsole.log(sdMeanModeCard({json.dumps(day, ensure_ascii=False)}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    # The P1 mean sits on the mode's rung and still reads left (by probability).
    assert '<b class="sd-evidence-primary">左偏</b><b style="color:var(--text)">P1 · 均值离众数 −0.42 档</b>' in out
    assert "门槛 ±0.40" in out and "均 24°C · 众 24°C" in out
    assert ">对称</b>" in out and ">右偏</b>" in out and "+0.60 档 / ±0.45" in out


def test_fleet_tally_card_counts_each_side() -> None:
    """The fleet's tally (user 2026-10-10 「加一个总的统计」): big numbers on the
    day's word, a row per series, the skewed stations folded underneath."""

    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from bayes_basket_target.station_day_board import shape_word

    assert [shape_word(x) for x in (0, -2, 1, None)] == ["symmetric", "left", "right", "none"]
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE
    formatters = _formatters(source)
    start = source.index("function sdFleetShape(")
    depth, end = 0, None
    for index in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    tally = lambda a, b, c: {"symmetric": a, "left": b, "right": c, "none": 0}  # noqa: E731
    doc = {"stations": 48, "built_at_utc": "2026-10-10T14:40:00Z", "days": [{
        "event_date": "2026-10-11", "station_days": 48,
        "counts": {"HEAD": tally(43, 4, 1), "P0": tally(44, 3, 1), "P0-M": tally(40, 4, 1),
                   "P1": tally(0, 0, 0), "MKT": tally(41, 3, 4)},
        "rows": [{"station_id": "ZUUU", "name": "成都", "head_stage": "P0-M",
                  "signs": {"P0-M": -1}, "gaps": {"P0-M": -0.42}}]}]}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\n"
              "const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n" + formatters
              + "function sdShapeRule(){ return 'rule'; }\n" + source[start:end] + f"\nconsole.log(sdFleetShape({json.dumps(doc, ensure_ascii=False)}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "全队 48 站" in out and "10-11" in out
    assert "<b>43</b><span>对称</span>" in out and "<b>4</b><span>左偏</span>" in out and "<b>1</b><span>右偏</span>" in out
    assert '<b style="color:p">市场</b><span>对称 41</span><span>左偏 3</span><span>右偏 4</span>' in out
    assert ">P1</b>" not in out  # no P1 yet on that day: no row
    assert "成都 <span" in out and "P0-M −0.42档" in out


def test_day_word_follows_charter_260_precedence() -> None:
    # A P0-M decided after the day's P1 does not take the word back (charter 260).
    from bayes_basket_target.station_day_board import day_word

    stages = {"P0": {"decided_at_utc": "2026-10-10T12:00:00Z"},
              "P0-M": {"decided_at_utc": "2026-10-10T12:30:00Z"},
              "P1": {"decided_at_utc": "2026-10-10T12:10:00Z"}}
    assert day_word(stages) == "P1"
    # Charter 283: a P0-M row (history) is never the day's word.
    assert day_word({k: stages[k] for k in ("P0", "P0-M")}) == "P0"
    assert day_word({}) is None


def test_mean_bucket_is_production_centre_bucket() -> None:
    """User 2026-10-11 「均值桶也按照生产口径来计算」: the board's 均值桶 is
    mean_center_bucket_v1, even where the °C mean falls in the next rung."""

    from bayes_basket_target.basket_kernel.support import mean_center_bucket_v1

    axis = _axis()
    vector = {"20°C or below": 0.1, "21°C": 0.4, "22°C": 0.4, "23°C or higher": 0.1}
    ordinals = {r["bucket"]: r["ordinal"] for r in axis}
    s = vector_stats(vector, axis)
    # The °C mean 21.5 is the 22°C rung's lower edge; production's mean ordinal
    # 1.5 ties two equal rungs and takes the lower.
    assert bucket_of(axis, s["mean_c"]) == "22°C"
    assert s["mean_bucket"] == mean_center_bucket_v1(vector, ordinals) == "21°C"


def test_skew_point_reads_production_rules() -> None:
    """board_readings.skew_truth_side reads each vector with production's
    skew sign and centre bucket (user 2026-10-11 「按照生产现有口径」)."""

    from bayes_basket_target.board_readings import _skew_point

    axis = _axis()
    p = _skew_point({"20°C or below": 0.2, "21°C": 0.1, "22°C": 0.6, "23°C or higher": 0.1}, axis, "21°C")
    assert p["sign"] == -1 and p["mode"] == "22°C" and p["mean_bucket"] == "22°C"
    assert p["truth_side"] == -1 and p["truth_step"] == -1
    assert _skew_point({"20°C or below": 0.2, "21°C": 0.1, "22°C": 0.6, "23°C or higher": 0.1}, axis,
                       "20°C or below")["truth_step"] == -2
    assert [round(x, 6) for x in p["claim"]] == [0.3, 0.6, 0.1]
    assert _skew_point({"21°C": 1.0}, axis, "no such rung") is None


def test_skew_history_card_draws_sides_and_hits() -> None:
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE
    start = source.index("function sdSkewHistory(")
    depth, end = 0, None
    for index in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    doc = {"since": "2026-08-27", "until": "2026-10-10", "event_days": 45, "computed_at_utc": "x",
           "threshold_sigma": 0.2, "stages": [{
        "stage": "P0", "pick": "last_before_next_stage", "station_days": 2001,
        "calls": [{"source": "model", "off_mode": 1200, "called": 720, "called_share": 0.6, "hit_rate": 0.715,
                   "hit_ci": [0.68, 0.75], "mi_bits": 0.0814, "event_days": 45}],
        "sides": [{"source": "model", "skew": "left", "n": 381, "share": 0.19, "truth": [0.467, 0.404, 0.129],
                   "claim": [0.487, 0.383, 0.13], "mode_hit": 0.404, "mean_hit": 0.394}],
        "hits": [{"group": "all", "n": 1235, "mode": {"model": 0.372, "market": 0.47, "diff": -0.098, "ci": [-0.134, -0.067]},
                  "mean": {"model": 0.37, "market": 0.445, "diff": -0.075, "ci": [-0.108, 0.01]}}]}]}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\nconst SD = {};\n"
              "const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n"
              "function sdShapeRule(){ return 'rule'; }\n" + source[start:end]
              + f"\nconsole.log(sdSkewHistory({json.dumps(doc, ensure_ascii=False)}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "45 个事件日" in out and "接手前末份 · 2001 站日" in out
    assert "左 47%" in out and "众 40%" in out and "右 13%" in out  # the truth bar
    assert '<td class="mono down"' in out and ">−9.8</td>" in out   # mode: interval below zero
    assert ">−7.5 ·</td>" in out                                      # centre bucket: crosses zero
    assert "按生产偏斜分组的真值命中率" in out                          # each source by its own skew
    assert "<td>左偏</td>" in out and "<b>40.4%</b>" in out and "<b>39.4%</b>" in out
    # the lean's own record (user 2026-10-16): called share, hit rate, bits
    assert "门槛 0.2σ" in out and "<b>60.0%</b> <span class=\"muted\">720/1200</span>" in out
    assert "<b>71.5%</b>" in out and ">0.081</td>" in out
    # 2026-10-17 「极简」: the stage cards lead, the tables sit under 明细
    top = out[:out.index("<summary class=\"muted\">明细</summary>")]
    assert "P0 众数命中" in top and ">37%</span>" in top and ">47%</span>" in top and "−9.8" in top
    assert "按生产偏斜分组的真值命中率" not in top


def test_skew_side_group_counts_the_buckets_beside_the_mode() -> None:
    """User 2026-10-17 「分别统计左偏（众数-1桶）、对称（众数）、右偏（众数+1桶）的
    真值命中率」: the truth one rung below, on, one rung above the mode; two
    rungs away counts in none of the three."""

    from bayes_basket_target.board_readings import _skew_side_group

    def pt(step):
        return {"truth_side": (step > 0) - (step < 0), "truth_step": step, "claim": [0, 1, 0],
                "mode": "m", "mean_bucket": "m"}
    g = _skew_side_group([pt(-1), pt(-1), pt(0), pt(1), pt(-2)], ["x"] * 5)
    assert g["adjacent_hit"] == [0.4, 0.2, 0.2]


def test_skew_history_draws_one_card_per_skew_with_the_aimed_bucket() -> None:
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE
    start = source.index("function sdSkewHistory(")
    depth, end = 0, None
    for index in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    side = lambda src, skew, adj: {"source": src, "skew": skew, "n": 100, "share": 0.3, "truth": [0.3, 0.4, 0.3],
                                   "claim": [0.3, 0.4, 0.3], "mode_hit": adj[1], "mean_hit": 0.4, "adjacent_hit": adj}
    doc = {"since": "2026-09-03", "until": "2026-10-17", "event_days": 45, "computed_at_utc": "x", "stages": [{
        "stage": "P0", "pick": "last_before_next_stage", "station_days": 300, "calls": [], "hits": [],
        "sides": [side("model", "left", [0.311, 0.402, 0.153]), side("model", "symmetric", [0.2, 0.444, 0.2]),
                  side("model", "right", [0.12, 0.38, 0.333]), side("market", "left", [0.29, 0.41, 0.14])]}]}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\nconst SD = {};\n"
              "const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n"
              "function sdShapeRule(){ return 'rule'; }\n" + source[start:end]
              + f"\nconsole.log(sdSkewHistory({json.dumps(doc, ensure_ascii=False)}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    top = out[:out.index("<summary class=\"muted\">明细</summary>")]   # shown, not folded
    # 「不要图表，要简明的卡片」: no table, one card per skew, the aimed bucket's rate
    assert "众数−1 / 众数 / 众数+1 桶的比例" not in out
    cards = top[top.index("左偏 · 真值在众数−1"):top.index("真值落点对所给概率")]
    assert "<table" not in cards
    # 「把这个表放在前端显示」: truth beside the mode against the vector's own mass
    claim = top[top.index("真值落点对所给概率"):]
    assert "P0 模型</th><th>真值在左</th><th>真值在众数</th><th>真值在右</th>" in claim
    assert "<td>左偏 <span class=\"muted\">100</span></td>" in claim
    assert "<b>30.0%</b> <span class=\"muted\">（30.0%）</span>" in claim and "<b>40.0%</b>" in claim
    # 「同时增加一组」: the market's own table under the model's, by its own skew
    assert claim.index("P0 模型</th>") < claim.index("P0 市场</th>")
    market = claim[claim.index("P0 市场</th>"):]
    assert "<td>左偏 <span class=\"muted\">100</span></td>" in market and "<td>对称" not in market
    assert "左偏 · 真值在众数−1" in top and "对称 · 真值在众数" in top and "右偏 · 真值在众数+1" in top
    left = top[top.index("左偏 · 真值在众数−1"):top.index("对称 · 真值在众数")]
    assert ">31%</b>" in left and ">29%</b>" in left                  # model 31.1%, market 29.0%
    assert "众数−1 31.1% / 众数 40.2% / 众数+1 15.3%" in left          # all three on hover
    assert ">44%</b>" in top[top.index("对称 · 真值在众数"):top.index("右偏 · 真值在众数+1")]
    right = top[top.index("右偏 · 真值在众数+1"):]
    assert ">33%</b>" in right and "市场 无" in right


def test_skew_call_stats_scores_the_lean_off_the_mode() -> None:
    """board_readings._skew_call_stats (user 2026-10-16): only off-mode days;
    a call is right when it names the truth's side; mutual information in bits."""

    from bayes_basket_target.board_readings import _skew_call_stats

    def row(day, sign, side):
        return {"event_date": day, "model": {"sign": sign, "truth_side": side}}
    rows = [row("2026-10-01", 1, 1), row("2026-10-01", 1, -1), row("2026-10-02", -1, -1),
            row("2026-10-02", 0, 1), row("2026-10-03", 0, -1), row("2026-10-03", 1, 0)]
    s = _skew_call_stats(rows, "model")
    assert s["off_mode"] == 5 and s["called"] == 3
    assert abs(s["called_share"] - 0.6) < 1e-12 and abs(s["hit_rate"] - 2 / 3) < 1e-12
    assert s["mi_bits"] > 0 and s["event_days"] == 3
    perfect = _skew_call_stats([row("d", 1, 1), row("d", -1, -1)], "model")
    assert abs(perfect["mi_bits"] - 1.0) < 1e-12 and perfect["hit_rate"] == 1.0
    assert _skew_call_stats([row("d", 1, 0)], "model") is None


def test_mode_agreement_reads_the_picked_episodes() -> None:
    """board_readings.mode_agreement: the first, second and last agreement of
    our mode with the market's, each scored on its own bucket (user
    2026-10-15 「首次…第二次、最后一次」)."""

    from unittest import mock

    from bayes_basket_target import board_readings as br

    order = {"20°C": 0, "21°C": 1, "22°C": 2}
    assert br._ladder_mode({"20°C": 0.4, "21°C": 0.4, "22°C": 0.2}, order) == "20°C"  # a tie: the lower rung
    ep = lambda bucket, hours, stage, hit, mover="model": {"bucket": bucket, "start": "t", "end": "t", "instants": 1,
                                                           "stage": stage, "hours": hours, "p": 0.4, "q": 0.5,
                                                           "hit": hit, "mover": mover, "hit3": True, "p3": 0.8,
                                                           "q3": 0.85, "ask": 0.45, "ask3": None,
                                                           "mid": 0.42, "mid3": 0.8}
    days = [
        {"station_id": "AAAA", "event_date": "2026-10-01", "settled": "21°C", "unit": "C", "paired": 9,
         "first_paired": {"ours_hit": False, "market_hit": True}, "episodes": 3,
         "picked": {"first": ep("20°C", 50.0, "P0", False), "second": ep("21°C", 20.0, "P0-M", True),
                    "last": ep("21°C", 5.0, "P1", True)},
         "starts": [["model", 50.0, "P0", 0, 0.4, 0.5, 1, 0.8, 0.85, 0.45, 0.9, 0.42, 0.8],
                    ["market", 20.0, "P0-M", 1, 0.4, 0.5, 1, 0.8, 0.85, 0.6, 0.9, 0.55, 0.85],
                    ["market", 5.0, "P1", 1, 0.4, 0.5, 0, 0.8, 0.85, None, None, None, None]]},
        {"station_id": "BBBB", "event_date": "2026-10-02", "settled": "22°C", "unit": "C", "paired": 4,
         "first_paired": {"ours_hit": True, "market_hit": True}, "episodes": 1,
         "picked": {"first": ep("22°C", 40.0, "P0", True, "start"), "last": ep("22°C", 40.0, "P0", True, "start")},
         "starts": [["start", 40.0, "P0", 1, 0.4, 0.5, 1, 0.8, 0.85, 0.45, 0.9, 0.42, 0.8]]},
    ]
    with mock.patch.object(br, "mode_agreement_days", return_value={"days": days, "counts": {}}):
        out = br.mode_agreement(None)
    picks = {k["pick"]: k for k in out["picks"]}
    assert picks["first"]["all"]["hit"] == 0.5 and picks["first"]["station_days"] == 2
    assert picks["second"]["station_days"] == 1 and picks["second"]["all"]["hit"] == 1.0
    # 「最后一次」 is gone (2026-10-21): only settlement says which episode was
    # last.  ``every`` is each episode as it began, knowable at the time.
    assert "last" not in picks
    assert picks["every"]["episodes"] == 4 and picks["every"]["station_days"] == 2
    assert picks["every"]["all"]["hit"] == 0.75
    by_stage = {x["slice"]: (x["n"], x.get("n_ask")) for x in picks["every"]["slices"] if x["group"] == "阶段"}
    assert by_stage == {"P0": (2, 2), "P0-M": (1, 1), "P1": (1, 0)}
    lead = {x["slice"]: x["n"] for x in picks["first"]["slices"] if x["group"] == "提前量"}
    assert lead == {"距结算 36–60h": 2}
    assert out["first_paired"] == {"n": 2, "ours_hit": 0.5, "market_hit": 1.0}
    assert picks["first"]["all"]["hit3"] == 1.0 and picks["first"]["all"]["said_market3"] == 0.85
    moved = {c["mover"]: c for c in out["converge"]}
    assert moved["model"]["every"]["n"] == 1 and moved["model"]["every"]["hit"] == 0.0
    assert moved["model"]["first"]["n"] == 1
    assert moved["market"]["every"]["n"] == 2 and moved["market"]["every"]["station_days"] == 1
    assert moved["market"]["every"]["hit3"] == 0.5 and moved["model"]["every"]["hit3"] == 1.0
    assert moved["market"]["every"]["n_ask"] == 1 and moved["market"]["every"]["ask"] == 0.6
    assert picks["first"]["all"]["ask"] == 0.45 and picks["first"]["all"]["n_ask3"] == 0
    assert picks["first"]["all"]["mid"] == 0.42 and picks["first"]["all"]["mid3"] == 0.8
    assert moved["market"]["every"]["hit_mid"] == 1.0 and moved["market"]["every"]["mid3"] == 0.85
    led = picks["first"]["ledger"]["mid"]          # one share each at its own mid: 0.42 lost, 0.42 won
    assert led["trades"] == 2 and led["hits"] == 1 and led["paid"] == 0.84 and led["profit"] == 0.16
    assert led["bands"] == [{"lo": 0.4, "hi": 0.5, "trades": 2, "hits": 1, "paid": 0.84, "profit": 0.16}]
    assert moved["model"]["ledger_first"]["ask"]["trades"] == 1
    assert moved["market"]["first"] is None and moved["both"]["every"] is None
    lead = {(x["slice"], x["scope"]): x["n"] for x in moved["market"]["slices"] if x["group"] == "提前量"}
    assert lead == {("距结算 < 12h", "every"): 1, ("距结算 12–24h", "every"): 1}


def test_mode_agreement_card_draws_three_picks() -> None:
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE

    def function(name: str) -> str:
        start = source.index(f"function {name}(")
        depth = 0
        for index in range(source.index("{", start), len(source)):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            if depth == 0:
                return source[start:index + 1]
        raise AssertionError(name)

    pick = lambda name, hit: {"pick": name, "station_days": 10, "share_of_days": 0.9,
                              "all": {"n": 10, "hit": hit, "hit_ci": [0.2, 0.7], "said_ours": 0.43, "said_market": 0.42,
                                      "median_hours": 49.5, "event_days": 3, "n_ask": 6, "hit_ask": 0.449, "ask": 0.466,
                                      "over_ask_ci": [-0.047, 0.014], "hit3": 0.888, "said_market3": 0.82, "n_ask3": 6,
                                      "hit_ask3": 0.881, "ask3": 0.949, "over_ask3_ci": [-0.093, -0.043],
                                      "n_mid": 6, "hit_mid": 0.448, "mid": 0.446, "over_mid_ci": [-0.03, 0.035],
                                      "n_mid3": 6, "hit_mid3": 0.878, "mid3": 0.882, "over_mid3_ci": [-0.027, 0.02]},
                              "slices": [{"group": "提前量", "slice": "距结算 36–60h", "n": 7, "hit": hit,
                                          "said_ours": 0.4, "said_market": 0.4}],
                              "daily": [{"event_date": "2026-10-01", "n": 4, "hit": hit}]}
    doc = {"since": "2026-09-06", "through": "2026-10-14", "event_days": 39, "book_window_sec": 1800,
           "computed_at_utc": "x", "first_paired": {"n": 10, "ours_hit": 0.358, "market_hit": 0.44},
           "picks": [{**pick("first", 0.45), "ledger": {
                         "mid": {"trades": 634, "hits": 284, "paid": 282.8565, "profit": 1.1435,
                                 "bands": [{"lo": 0.4, "hi": 0.5, "trades": 240, "hits": 102, "paid": 106.55, "profit": -4.55}]},
                         "ask": {"trades": 634, "hits": 284, "paid": 295.871, "profit": -11.871, "bands": []}}},
                     pick("second", 0.5),
                     {**pick("every", 0.598), "episodes": 9566,
                      "slices": [{"group": "阶段", "slice": "P1", "n": 8171, "n_ask": 8171, "hit_ask": 0.631, "ask": 0.65},
                                 {"group": "提前量", "slice": "距结算 6–12h", "n": 4176, "n_ask": 4176,
                                  "hit_ask": 0.745, "ask": 0.755}]}],
           "converge": [{"mover": "model", "every": {"n": 7089, "station_days": 1288, "hit": 0.656, "said_market": 0.634,
                                                     "n_ask": 5607, "hit_ask": 0.625, "ask": 0.641,
                                                     "over_ask_ci": [-0.037, 0.008], "n_mid3": 3673, "hit_mid3": 0.947,
                                                     "mid3": 0.933, "over_mid3_ci": [0.003, 0.025]},
                         "first": {"n": 420, "hit": 0.502, "said_market": 0.446, "over_market_ci": [0.006, 0.106]},
                         "slices": [{"group": "提前量", "slice": "距结算 36–60h", "scope": "every", "n": 631, "hit": 0.45,
                                     "said_market": 0.407, "over_market_ci": [-0.001, 0.086]}], "daily": []},
                        {"mover": "market", "every": {"n": 2068, "station_days": 1001, "hit": 0.49, "said_market": 0.48,
                                                      "n_ask": 1618, "hit_ask": 0.512, "ask": 0.544,
                                                      "over_ask_ci": [-0.057, -0.007]},
                         "first": None, "slices": [], "daily": []}]}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\nconst SD = {};\n"
              "const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n"
              + function("sdAgreeEdge") + "\n" + function("sdModeAgreement") + "\n" + function("sdModeConverge")
              + "\n" + function("sdAgreeLedger")
              + f"\nconsole.log(sdModeAgreement({json.dumps(doc, ensure_ascii=False)}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "首次同桶" in out and "第二次同桶" in out and "全部同桶段" in out and "最后一次" not in out
    assert "45.0%" in out and "50.0%" in out and "59.8%" in out and "9566 段" in out
    # every episode by stage and hours, landed against the ask (user 2026-10-21)
    assert "63.1%" in out and "65.0%" in out and "−1.9 点" in out and "−1.0 点" in out
    assert "我方众数命中 35.8%" in out and "市场众数命中 44.0%" in out
    assert "距结算 36–60h" in out and "39 个事件日" in out
    assert "模型 → 市场" in out and "市场 → 模型" in out and "65.6%" in out
    assert ">−1.6 ·</span>" in out                                    # model onto the market, against the ask: crosses zero
    assert '<span class="mono down"' in out and ">−3.2</span>" in out  # market onto the model: below the ask
    assert ">−1.7 ·</span>" in out and ">−6.8</span>" in out           # first agreement: one bucket / mode ±1 against the asks
    assert ">+0.2 ·</span>" in out and ">−0.4 ·</span>" in out         # ... and against the mid: even
    assert ">+1.4</span>" in out                                       # model onto the market, mode ±1 at the mid
    assert "−中价" in out
    assert "逐笔记账" in out and "0.40–0.50" in out and ">−4.55</span>" in out  # one band at the mid
    assert ">+1.14</span>" in out and ">−11.87</span>" in out                  # the totals, mid and ask
    assert "当时就能知道" in out
    top = out[:out.index("<summary class=\"muted\">明细</summary>")]
    assert "首次同桶" in top and "45.0%" in top and "模型 → 市场" in top and "65.6%" in top
    assert "逐笔记账" not in top and "−中价" not in top, "the ledger and edge tables are folded"


def test_a_local_day_ahead_of_utc_reads_the_utc_day_folded_onto_local_hours(monkeypatch):
    """2026-10-13: the satellite archive ends at UTC today, so an East Asian
    local day past midnight answered 400 until 08/09 local; it now reads the
    UTC day and folds it back.  A half-hour zone says when it opens."""

    from zoneinfo import ZoneInfo

    from bayes_basket_target import station_day_board as board

    asked = []

    def fake(lat, lon, tz, day, model):
        asked.append((tz, day))
        return {"available": True, "values": [float(i) for i in range(24)]}

    monkeypatch.setattr(board, "_open_meteo_satellite", fake)
    out = board._satellite_ahead_of_utc(37.5, 126.5, "2026-10-14", ZoneInfo("Asia/Seoul"), "jma_jaxa_himawari")
    assert asked == [("GMT", "2026-10-13")]
    # Local 00:00 is UTC 15:00 the day before; local 09:00 is UTC 00:00 the day itself.
    assert out["values"][:9] == [15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0]
    assert out["values"][9:] == [None] * 15
    half = board._satellite_ahead_of_utc(26.8, 80.9, "2026-10-14", ZoneInfo("Asia/Kolkata"), "eumetsat_lsa_saf_iodc")
    assert half["available"] is False and "05:30" in half["reason"]


def test_skew_call_trend_counts_per_day() -> None:
    from bayes_basket_target.board_readings import _skew_call_trend

    def row(day, sign, side, market=None):
        return {"event_date": day, "model": {"sign": sign, "truth_side": side}, "market": market}
    t = _skew_call_trend([row("2026-10-01", 1, 1), row("2026-10-01", -1, 1), row("2026-10-01", 0, -1),
                          row("2026-10-02", 1, 0, {"sign": -1, "truth_side": -1})])
    assert [d["event_date"] for d in t] == ["2026-10-01", "2026-10-02"]
    assert t[0]["model"] == {"off": 3, "called": 2, "right": 1, "floor": 0}
    assert t[1]["model"] == {"off": 0, "called": 0, "right": 0, "floor": 0}
    assert t[1]["market"] == {"off": 1, "called": 1, "right": 1, "floor": 0}
    floored = _skew_call_trend([{"event_date": "d", "model": {"sign": 1, "truth_side": 1, "floor": True}, "market": None}])
    assert floored[0]["model"] == {"off": 0, "called": 0, "right": 0, "floor": 1}


def test_floor_day_card_word_and_fleet_tally() -> None:
    """The card names a floor day 下界·待涨, and the fleet tally counts it apart."""

    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE

    def function(name: str) -> str:
        start = source.index(f"function {name}(")
        depth = 0
        for index in range(source.index("{", start), len(source)):
            depth += {"{": 1, "}": -1}.get(source[index], 0)
            if depth == 0:
                return source[start:index + 1]
        raise AssertionError(name)

    st = {"skew_sign": 1, "skew_floor": True, "skew_gap_buckets": 0.32, "skew_threshold_buckets": 0.14}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\n"
              "const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n" + _formatters(source)
              + "function sdShapeRule(){ return 'rule'; }\n" + function("sdShape") + "\n" + function("sdFleetShape")
              + f"\nconsole.log(sdShape({json.dumps(st)}).word);"
              + "\nconsole.log(sdFleetShape(" + json.dumps({"stations": 2, "built_at_utc": "x", "days": [{
                  "event_date": "2026-10-16", "station_days": 2,
                  "counts": {"HEAD": {"symmetric": 1, "left": 0, "right": 0, "floor": 1}},
                  "rows": [{"station_id": "ZUUU", "name": "成都", "head_stage": "P1", "signs": {"P1": 1}, "floors": {"P1": True}, "gaps": {"P1": 0.32}}]}]},
                  ensure_ascii=False) + "));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.splitlines()[0] == "下界·待涨"
    assert "<b>1</b><span>下界·待涨</span>" in out
    assert "右偏</b> 成都" not in out and "下界·待涨</b> 成都" in out


def test_floor_day_is_the_mode_holding_the_running_high() -> None:
    """User 2026-10-16: a 下界日 -- the mode is the rung holding the running
    observed high -- is labelled 下界·待涨 and left out of the lean's hit rate."""

    from bayes_basket_target.board_readings import _skew_call_stats, _skew_point
    from bayes_basket_target.station_day_board import skew_floor

    axis = _axis()
    vector = {"20°C or below": 0.0, "21°C": 0.77, "22°C": 0.18, "23°C or higher": 0.05}
    assert skew_floor("21°C", axis, 21.0) is True
    assert skew_floor("21°C", axis, 20.0) is False and skew_floor("21°C", axis, None) is False
    on = _skew_point(vector, axis, "22°C", 21.0)
    assert on["floor"] is True and on["sign"] == 1          # still reads right, but flagged
    assert _skew_point(vector, axis, "22°C")["floor"] is False
    rows = [{"event_date": "d", "model": on},
            {"event_date": "d", "model": {**on, "floor": False, "sign": 1, "truth_side": -1}}]
    s = _skew_call_stats(rows, "model")
    assert s["floor_days"] == 1 and s["off_mode"] == 1 and s["hit_rate"] == 0.0


def test_skew_trend_chart_pools_running_and_seven_day_rates() -> None:
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the board's renderer")
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import calibration_dashboard as C  # noqa: PLC0415

    source = C.HTML_TEMPLATE
    start = source.index("function sdSkewTrend(")
    depth, end = 0, None
    for index in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    days = [{"event_date": f"2026-10-{d:02d}", "model": {"off": 20, "called": 12, "right": 9},
             "market": {"off": 20, "called": 10, "right": 8}} for d in range(1, 11)]
    st = {"stage": "P0", "trend": days}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\n"
              "const sdEsc = s => String(s == null ? '' : s);\n" + source[start:end]
              + f"\nconsole.log(sdSkewTrend({json.dumps(st)}, '2026-09-01'));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.count("<polyline") == 4                       # running + 7-day, model + market
    assert "75.0%</b>（120）" in out and "80.0%</b>（100）" in out  # 90/120, 80/100
    assert "10-01" in out and "10-10" in out
    assert "累计判对 模型 75.0%" in out                       # hover text
    assert subprocess.run([node, "-e", script.replace(json.dumps(st), json.dumps({"stage": "P0", "trend": days[:1]}))],
                          capture_output=True, text=True, timeout=60).stdout.strip() == ""


def test_mode_agreement_keeps_a_day_whose_books_left_the_hot_store(monkeypatch) -> None:
    """2026-10-21: a re-walk lost 171 station-days whose books had moved to the
    cold tier.  A day the hot store can no longer walk keeps its stored record."""

    import sqlite3

    from bayes_basket_target import board_readings as br

    old = {"station_id": "AAAA", "event_date": "2026-09-08", "settled": "21°C", "unit": "C", "paired": 9,
           "first_paired": {}, "episodes": 1, "picked": {}, "starts": []}
    monkeypatch.setattr(br, "load_reading", lambda name: {"value": {"days": [old], "walk_sha256": "older-code"}})
    monkeypatch.setattr("bayes_basket_target.label_knowability.knowable_labels",
                        lambda conn: {("AAAA", "2026-09-08"): {"bucket": "21°C"}})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_bridge_history(station_id TEXT, event_date TEXT, state TEXT,"
                 " decision_at_utc TEXT, settlement_timezone TEXT)")
    out = br.mode_agreement_days(conn)
    assert out["days"] == [old] and out["counts"]["kept_days"] == 1


def test_the_reported_high_leaves_out_a_speci_that_does_not_settle() -> None:
    """Charter 276: from a city's switch date a SPECI does not settle, and the
    board's reported high follows the reports that do."""

    from bayes_basket_target import station_day_board as b

    rows = [{"temp_c": 24.0, "settles": True}, {"temp_c": 26.0, "settles": False}, {"temp_c": None}]
    assert b._settled_high(rows) == 24.0
    assert b._settled_high([{"temp_c": 26.0}]) == 26.0  # rows without the flag (older payloads) count


def test_goes_skin_hours_read_the_scan_at_each_hour_and_only_in_the_americas(monkeypatch) -> None:
    """User 2026-10-24「先接地表皮温」: GOES LST, hour h is the scan at h:00
    local; a scan under 20 minutes old is not asked for; outside GOES's view
    (the rest of the fleet) nothing is asked at all."""

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bayes_basket_target import goes_surface_radiation as goes
    from bayes_basket_target import station_day_board as board

    asked: list[tuple[datetime, str]] = []

    def fake_measured(station, lat, lon, instants, kind="dsr"):
        asked.extend((i, kind) for i in instants)
        return {datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc).isoformat(): 18.4}  # 10:00 EDT

    monkeypatch.setattr(goes, "measured", fake_measured)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 8, 17, 10, tzinfo=timezone.utc)  # 13:10 EDT

    monkeypatch.setattr(board, "datetime", Clock)
    out = board._goes_skin_hours("KLGA", 40.77945, -73.88027, "2026-10-08", ZoneInfo("America/New_York"))
    assert out["values"][10] == 18.4 and out["values"][12] is None and out["values"][13] is None
    assert {k for _, k in asked} == {"lst"} and max(i for i, _ in asked) == datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)
    asked.clear()
    far = board._goes_skin_hours("LEMD", 40.47, -3.56, "2026-10-08", ZoneInfo("Europe/Madrid"))
    assert far["available"] is False and asked == []


def test_the_soil_card_sets_satellite_skin_against_the_forecast_surface() -> None:
    from pathlib import Path

    dash = (Path(__file__).resolve().parents[1] / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert 'sdSunCard("soil", "地表皮温 · 实况对比预报"' in dash
    assert "skinRow.sat_skin_c - skinRow.forecast.soil0_c" in dash
    assert "skinRaw - skinN.offset_c" in dash and "toned(soilCard, toneOf([], skinD, true, 2))" in dash


def test_skin_normal_is_the_same_hour_over_the_seven_days_before(monkeypatch) -> None:
    """User 2026-10-24「卡片按本站近 7 天同一整点的「卫星皮温−预报地表」常态差
    扣掉后再判冷暖」: hour h on each of the 7 days before; cloudy days drop out;
    under 3 clear pairs there is no normal."""

    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from bayes_basket_target import goes_surface_radiation as goes
    from bayes_basket_target import station_day_board as board

    zone = ZoneInfo("America/New_York")
    asked: list = []
    soil = [None] * (7 * 24)
    for i in range(7):
        soil[i * 24 + 14] = 20.0 + i          # 10-17 .. 10-23 at 14:00
    monkeypatch.setattr(board, "_open_meteo_soil_history",
                        lambda lat, lon, tz, first, last: asked.append((first, last)) or soil)

    def fake(station, lat, lon, instants, kind="dsr"):
        assert kind == "lst" and len(instants) == 7
        # Clear on four days, skin 5 °C above the forecast plus 0..3.
        return {at.isoformat(): 20.0 + i + 5.0 + j for j, (i, at) in enumerate(list(enumerate(instants))[:4])}

    monkeypatch.setattr(goes, "measured", fake)
    out = board._skin_normal("KATL", 33.64, -84.43, "2026-10-24", zone, 14)
    assert asked == [("2026-10-17", "2026-10-23")]
    assert out["pairs"] == 4 and out["offset_c"] == 6.5 and out["unread"] == 3
    monkeypatch.setattr(goes, "measured", lambda *a, **k: {})
    assert board._skin_normal("KATL", 33.64, -84.43, "2026-10-24", zone, 14)["offset_c"] is None


def test_mos_mode_side_reads_its_rung_against_the_market_mode_and_its_newest_change() -> None:
    """User 2026-10-25「MOS增加判断真值在市场众数哪侧和方向的变化（突出）」."""

    from bayes_basket_target import station_day_board as board

    axis = [{"bucket": "18°C or below", "lo_c": None, "hi_c": 18.5}, {"bucket": "19°C", "lo_c": 18.5, "hi_c": 19.5},
            {"bucket": "20°C", "lo_c": 19.5, "hi_c": 20.5}, {"bucket": "21°C or higher", "lo_c": 20.5, "hi_c": None}]
    cx = {"forecast_tmax_d1_c": 19.0, "total_c": 1.2,
          "series": [{"hour": 8, "total_c": 0.9}, {"hour": 9, "total_c": 0.2}, {"hour": 10, "total_c": 0.3},
                     {"hour": 11, "total_c": 1.2}]}
    out = board.composite_mode_side(cx, axis, "19°C")
    assert out["bucket"] == "20°C" and out["side"] == 1 and out["changes"] == 2
    assert out["change"] == {"hour": 11, "from": "19°C", "to": "20°C", "dir": 1, "from_mode": True}
    assert board.composite_mode_side({**cx, "total_c": 0.0}, axis, "19°C")["side"] == 0
    assert board.composite_mode_side(cx, axis, None)["side"] is None
    assert board._rung_holding(axis, 25.0) == "21°C or higher" and board._rung_holding(axis, 10.0) == "18°C or below"


def test_both_mos_cards_show_the_mode_side_boxes() -> None:
    from pathlib import Path

    dash = (Path(__file__).resolve().parents[1] / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "function sdMosSide(X)" in dash
    assert "body: sdMosSide(X) +" in dash and "+ sdMosSide(CX)" in dash
    assert "<small>真值在市场众数哪侧</small>" in dash and "MOS 方向变化" in dash


def test_a_past_projection_reads_only_what_was_known_at_its_hour() -> None:
    """User 2026-10-26 「曲线增加虚线和实线对比」: the dashed what-if drawn from an
    earlier hour must not see the reports and satellite readings that came later."""

    from bayes_basket_target.station_day_board import _as_of

    rows = {h: {"hour": h, "obs": {"temp_c": 20.0 + h}, "sat_sw_wm2": 100.0 * h,
                "forecast": {"temp_c_d1": 20.0}} for h in range(6, 16)}
    past = _as_of(rows, 10)
    assert past[10]["obs"]["temp_c"] == 30.0 and past[10]["sat_sw_wm2"] == 1000.0
    for h in range(11, 16):
        assert "obs" not in past[h] and "sat_sw_wm2" not in past[h]
        assert past[h]["forecast"] == {"temp_c_d1": 20.0}
