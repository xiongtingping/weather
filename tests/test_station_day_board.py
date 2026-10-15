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

    day = {"head_stage": "P0-M", "stages": {
        "P0": {"stats": {"mean_bucket": "24°C", "mode": "24°C", "skew_sign": 0,
                         "skew_gap_buckets": 0.1, "skew_threshold_buckets": 0.38}},
        "P0-M": {"stats": {"mean_bucket": "24°C", "mode": "24°C", "skew_sign": -1,
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
    # The P0-M mean sits on the mode's rung and still reads left (by probability).
    assert '<b class="sd-evidence-primary">左偏</b><b style="color:var(--text)">P0-M · 均值离众数 −0.42 档</b>' in out
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
    assert day_word({k: stages[k] for k in ("P0", "P0-M")}) == "P0-M"
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
    assert p["truth_side"] == -1
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
    doc = {"since": "2026-08-27", "until": "2026-10-10", "event_days": 45, "computed_at_utc": "x", "stages": [{
        "stage": "P0", "pick": "last_before_next_stage", "station_days": 2001,
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


def test_mode_agreement_reads_the_picked_episodes() -> None:
    """board_readings.mode_agreement: the first, second and last agreement of
    our mode with the market's, each scored on its own bucket (user
    2026-10-15 「首次…第二次、最后一次」)."""

    from unittest import mock

    from bayes_basket_target import board_readings as br

    order = {"20°C": 0, "21°C": 1, "22°C": 2}
    assert br._ladder_mode({"20°C": 0.4, "21°C": 0.4, "22°C": 0.2}, order) == "20°C"  # a tie: the lower rung
    ep = lambda bucket, hours, stage, hit: {"bucket": bucket, "start": "t", "end": "t", "instants": 1, "stage": stage,
                                            "hours": hours, "p": 0.4, "q": 0.5, "hit": hit}
    days = [
        {"station_id": "AAAA", "event_date": "2026-10-01", "settled": "21°C", "unit": "C", "paired": 9,
         "first_paired": {"ours_hit": False, "market_hit": True}, "episodes": 3,
         "picked": {"first": ep("20°C", 50.0, "P0", False), "second": ep("21°C", 20.0, "P0-M", True),
                    "last": ep("21°C", 5.0, "P1", True)}},
        {"station_id": "BBBB", "event_date": "2026-10-02", "settled": "22°C", "unit": "C", "paired": 4,
         "first_paired": {"ours_hit": True, "market_hit": True}, "episodes": 1,
         "picked": {"first": ep("22°C", 40.0, "P0", True), "last": ep("22°C", 40.0, "P0", True)}},
    ]
    with mock.patch.object(br, "mode_agreement_days", return_value={"days": days, "counts": {}}):
        out = br.mode_agreement(None)
    picks = {k["pick"]: k for k in out["picks"]}
    assert picks["first"]["all"]["hit"] == 0.5 and picks["first"]["station_days"] == 2
    assert picks["second"]["station_days"] == 1 and picks["second"]["all"]["hit"] == 1.0
    assert picks["last"]["all"]["hit"] == 1.0
    lead = {x["slice"]: x["n"] for x in picks["first"]["slices"] if x["group"] == "提前量"}
    assert lead == {"距结算 36–60h": 2}
    assert {x["slice"] for x in picks["last"]["slices"] if x["group"] == "同桶次数"} == {"只同桶过一次", "同桶过 ≥ 2 次"}
    assert out["first_paired"] == {"n": 2, "ours_hit": 0.5, "market_hit": 1.0}


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
    start = source.index("function sdModeAgreement(")
    depth, end = 0, None
    for index in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            end = index + 1
            break
    pick = lambda name, hit: {"pick": name, "station_days": 10, "share_of_days": 0.9,
                              "all": {"n": 10, "hit": hit, "hit_ci": [0.2, 0.7], "said_ours": 0.43, "said_market": 0.42,
                                      "median_hours": 49.5, "event_days": 3},
                              "slices": [{"group": "提前量", "slice": "距结算 36–60h", "n": 7, "hit": hit,
                                          "said_ours": 0.4, "said_market": 0.4}],
                              "daily": [{"event_date": "2026-10-01", "n": 4, "hit": hit}]}
    doc = {"since": "2026-09-06", "through": "2026-10-14", "event_days": 39, "book_window_sec": 1800,
           "computed_at_utc": "x", "first_paired": {"n": 10, "ours_hit": 0.358, "market_hit": 0.44},
           "picks": [pick("first", 0.45), pick("second", 0.5), pick("last", 0.983)]}
    script = ("const SD_COL = {\"P0\":\"b\",\"P0-M\":\"o\",\"P1\":\"g\",\"MKT\":\"p\"};\nconst SD = {};\n"
              "const sdEsc = s => String(s == null ? '' : s);\nconst sdLocal = s => s;\n" + source[start:end]
              + f"\nconsole.log(sdModeAgreement({json.dumps(doc, ensure_ascii=False)}));\n")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "首次同桶" in out and "第二次同桶" in out and "最后一次同桶" in out
    assert "45.0%" in out and "50.0%" in out and "98.3%" in out
    assert "我方众数命中 35.8%" in out and "市场众数命中 44.0%" in out
    assert "距结算 36–60h" in out and "39 个事件日" in out


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
