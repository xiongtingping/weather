"""The station board shows every connected deterministic model's newest run
(user 2026-10-13 「增加目前已接入的"确定性预报模型"的数据」): the stored point
runs and charter 267's listing-time F, read, never computed."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from bayes_basket_target import deterministic_forecast_family as fam
from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")


def _store(rows: list[tuple]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_regional_point_run (station_id TEXT, target_local_date TEXT,"
        " ensemble_system_id TEXT, run_init_utc TEXT, knowable_at_utc TEXT, forecast_tmax_c REAL)"
    )
    conn.executemany("INSERT INTO calibration_regional_point_run VALUES ('ZUUU','2026-10-13',?,?,?,?)", rows)
    return conn


def test_newest_and_previous_run_per_model_and_the_f_entry(tmp_path, monkeypatch) -> None:
    icon, arpege = fam.SYSTEM_IDS["ICON_GLOBAL"], fam.SYSTEM_IDS["ARPEGE"]
    conn = _store([
        (icon, "2026-10-12T12:00:00Z", "2026-10-12T16:00:00Z", 25.05),
        (icon, "2026-10-13T00:00:00Z", "2026-10-13T04:05:00Z", 26.19),
        # A re-capture of the same run keeps the first one.
        (icon, "2026-10-13T00:00:00Z", "2026-10-13T05:00:00Z", 30.0),
        (arpege, "2026-10-13T00:00:00Z", "2026-10-13T04:28:00Z", 28.09),
        ("SOME_ENSEMBLE", "2026-10-13T00:00:00Z", "2026-10-13T04:00:00Z", 20.0),
    ])
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"computed_at_utc": "2026-10-13T04:53:29Z", "station_days": {
        "ZUUU|2026-10-13": {"f_c": 27.537, "raw_median_c": 26.273, "n_systems": 6, "state": "READY",
                            "v3": {"f_c": 27.738}}}}), encoding="utf-8")
    monkeypatch.setattr(board, "LISTING_F_PATH", path)
    monkeypatch.setattr(board, "_LISTING_F_CACHE", {})
    out = board._deterministic(conn, "ZUUU", "2026-10-13")
    assert [r["product"] for r in out["systems"]] == ["ICON_GLOBAL", "ARPEGE"]   # F's first
    icon_row, arpege_row = out["systems"]
    assert icon_row["in_f"] and icon_row["tmax_c"] == 26.19 and icon_row["prev_tmax_c"] == 25.05
    assert not arpege_row["in_f"] and arpege_row["prev_tmax_c"] is None
    assert out["f"] == {"f_c": 27.537, "raw_median_c": 26.273, "n_systems": 6, "state": "READY",
                        "computed_at_utc": "2026-10-13T04:53:29Z", "f_v3_c": 27.738,
                        "pick": None}   # charter 267 补记七's pick, absent in this file


def _function(name: str) -> str:
    start = DASHBOARD.index(f"function {name}(")
    depth = 0
    for index in range(DASHBOARD.index("{", start), len(DASHBOARD)):
        depth += {"{": 1, "}": -1}.get(DASHBOARD[index], 0)
        if depth == 0:
            return DASHBOARD[start:index + 1]
    raise AssertionError(name)


def test_the_card_draws_f_and_one_chip_per_model() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the card")
    day = {"stages": {"P0": {"model_center_c": 27.55}}, "deterministic": {
        "f": {"f_c": 27.537, "raw_median_c": 26.273, "n_systems": 6, "computed_at_utc": "2026-10-13T04:53:29Z"},
        "systems": [
            {"product": "ICON_GLOBAL", "system_id": "X", "in_f": True, "relay": False, "tmax_c": 26.19,
             "run_init_utc": "2026-10-13T00:00:00Z", "knowable_at_utc": "2026-10-13T04:05:00Z",
             "prev_run_init_utc": "2026-10-12T12:00:00Z", "prev_tmax_c": 25.05,
             "runs": [["2026-10-12T16:00:00Z", 25.05, "2026-10-12T12:00:00Z"],
                      ["2026-10-13T04:05:00Z", 26.19, "2026-10-13T00:00:00Z"]]},
            {"product": "ARPEGE", "system_id": "Y", "in_f": False, "relay": False, "tmax_c": 28.09,
             "run_init_utc": "2026-10-13T00:00:00Z", "knowable_at_utc": "2026-10-13T04:28:00Z",
             "prev_run_init_utc": None, "prev_tmax_c": None}]}}
    start = DASHBOARD.index("const SD_DET_LABEL")
    script = (
        'const SD = {data: {timezone: "Asia/Shanghai"}}; const SD_COL = {P0: "#58a6ff"};\n'
        'const sdEsc = s => String(s == null ? "" : s);\n'
        'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
        'const sdSg = (v, d) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d);\n'
        + _function("sdLocal") + "\n" + "".join(_function(n) + "\n" for n in ("sdSpan", "sdAgeText", "sdAgeSpan", "sdMv", "sdRecentTable", "sdWhyRow"))
        + "const SD_RECENT_OPEN = new Set();\n"
        + DASHBOARD[start:DASHBOARD.index("function sdDeterministic(")] + _function("sdDeterministic")
        + f"\nconsole.log(sdDeterministic({json.dumps(day, ensure_ascii=False)}).replace(/<[^>]+>/g, '|'));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = out.stdout
    assert "F 27.54°C" in text and "比 P0 −0.01°C" in text and "中位 26.27°C" in text
    assert "ICON 全球" in text and "26.2" in text and "↑+1.1" in text and "进 F" in text
    assert "ARPEGE" in text and "28.1" in text and "首轮" in text and "只采集" in text
    # The last arrivals, newest first (user 2026-11-02 「最近10次的变化」).
    assert "最近 2 次" in text and "|原因：ICON 全球 00Z 到货|" in text and "|原因：ICON 全球 12Z 到货|" in text
    assert "${sdDeterministic(day)}" in DASHBOARD


def test_p0_change_cause_names_its_ngr_inputs() -> None:
    """User 2026-11-02 「模型没有run更新，为什么p0变化了呢」「加上」: charter 281's
    P0 also moves on a deterministic run, the lead hour and the radiation score;
    the cause names them instead of 「同一批 run 重算」."""

    runs = json.dumps({"NOAA_GEFS_05": "2026-11-01T18:00:00Z"})
    det = lambda **kw: json.dumps({k: {"day_max_c": 20.0, "run_init_utc": v} for k, v in kw.items()})  # noqa: E731
    head = (runs, None, None, None, "root-1")
    before = head + (det(GFS="2026-11-01T12:00:00Z", CMA="2026-10-31T18:00:00Z"), -0.8333, -0.68)
    now = head + (det(GFS="2026-11-01T18:00:00Z", CMA="2026-11-01T12:00:00Z"), -0.875, -0.48)
    assert board._timeline_cause("P0", now, before) == [
        "确定性 CMA 10-31 18Z→11-01 12Z、GFS 12Z→18Z", "提前量 16→15 小时", "辐射分 -0.68→-0.48"]
    assert board._timeline_cause("P0", before, before) == ["同一批 run 重算"]
    assert board._timeline_cause("P0", head, head) == ["同一批 run 重算"]
    # The station's ensemble weights refit daily (charter 199/242).
    assert board._timeline_cause("P0", before + ("2026-11-02T00:13:28Z",), before + ("2026-11-01T00:20:29Z",)) \
        == ["集合权重日拟合更新"]


def test_recent_tables_show_ten_and_fold_the_older_five() -> None:
    """User 2026-11-02 「最近10次的变化（隐藏前5次）」: ten rows at most, the five
    older in a folded body whose open state survives a redraw."""

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to run the table")
    script = (
        "const sdEsc = s => String(s == null ? '' : s);\n" + _function("sdRecentTable") + "\n"
        + "const SD_RECENT_OPEN = new Set();\n"
        + "const rows = Array.from({length: 12}, (_, i) => `<tr><td>r${i}</td></tr>`);\n"
        + "const a = sdRecentTable('d:P0', ['原因'], rows); SD_RECENT_OPEN.add('d:P0');\n"
        + "console.log(JSON.stringify([a, sdRecentTable('d:P0', ['原因'], rows), sdRecentTable('d:P1', ['原因'], rows.slice(0, 3))]));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    folded, opened, short = json.loads(out.stdout)
    assert "最近 10 次" in folded and "r9" in folded and "r10" not in folded
    assert folded.index("r4") < folded.index('class="sd-recent-more"') < folded.index("r5")
    assert "展开更早 5 次" in folded and 'class="sd-recent"' in folded
    assert 'class="sd-recent open"' in opened and "收起" in opened
    assert "最近 3 次" in short and "sd-recent-more" not in short


def test_model_runs_give_each_run_its_increment_and_the_next_run() -> None:
    """User 2026-11-02 「增加每次模型run的增量变化。增加模型下一次run的当地时间」:
    per system, each run's calibrated centre against its run before, and the next
    run one shortest gap after the newest plus the median delay to first use."""

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT, stage TEXT,"
                 " state TEXT, decision_at_utc TEXT, lineage_json TEXT)")
    def p0(at, init, centre):
        lineage = {"run_init_utc_by_system_json": json.dumps({"S": init}),
                   "native_calibrated_system_centers_c_json": json.dumps({"S": centre})}
        conn.execute("INSERT INTO basket_probability_envelope VALUES ('ZUUU','2026-11-02','P0','COMMITTED',?,?)",
                     (at, json.dumps(lineage)))
    p0("2026-11-01T03:00:00Z", "2026-10-31T18:00:00Z", 20.0)   # met late: says nothing of the delay
    p0("2026-11-01T05:00:00Z", "2026-11-01T00:00:00Z", 20.4)
    p0("2026-11-01T11:00:00Z", "2026-11-01T06:00:00Z", 20.1)
    p0("2026-11-01T12:00:00Z", "2026-11-01T06:00:00Z", 20.2)   # the run's last centre counts
    runs = board._model_runs(conn, "ZUUU", "2026-11-02")["S"]
    assert [r["init_utc"][11:13] for r in runs["runs"]] == ["06", "00", "18"]
    assert runs["runs"][0]["change_c"] == pytest.approx(-0.2) and runs["runs"][1]["change_c"] == pytest.approx(0.4)
    assert runs["cycle_hours"] == 6 and runs["lag_hours"] == 5
    assert runs["next_init_utc"] == "2026-11-01T12:00:00Z" and runs["next_due_utc"] == "2026-11-01T17:00:00Z"


def test_weight_cause_names_the_daily_refit() -> None:
    """User 2026-11-02 「这里模型权重变化的原因是什么？」: the station mixture's daily
    refit (charter 199/242) moved the weights; the receipt says when and how
    many settled days it added."""

    before = {"fusion_blend_json": json.dumps({"state": "APPLIED", "params_id": "a", "seat_weights": {"S": 1.0},
                                               "params_computed_at_utc": "2026-11-01T00:20:29Z", "days_by_system": {"S": 78, "T": 7}})}
    now = {"fusion_blend_json": json.dumps({"state": "APPLIED", "params_id": "b", "seat_weights": {"S": 1.0},
                                            "params_computed_at_utc": "2026-11-02T00:13:28Z", "days_by_system": {"S": 79, "T": 8}})}
    cause = board._weight_cause(now, before)
    assert cause["refit"] and cause["days_added"] == [1] and not cause["seat_changed"]
    assert cause["params_computed_at_utc"] == "2026-11-02T00:13:28Z"
    assert board._weight_cause(now, None) is None


def test_ngr_weights_are_the_fits_family_weights_and_f_share() -> None:
    """User 2026-11-02 「最新的p0，确定性模型是否进入了p0」: the board and the model
    area show the weights P0 serves -- pi_f = exp(beta_f) renormalised over the
    seated families -- and F's share w0 + w1 * L, L from the floored hours."""

    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    start = DASHBOARD.index("const NGR_FAM_LABEL")
    script = (DASHBOARD[start:DASHBOARD.index("const NGR_BAND")]
              + "const T = {beta_ECMWF: 0, beta_ECMWF_AIFS: Math.log(3), beta_NOAA_GEFS: 0, beta_NOAA_AIGEFS: 0,"
              " beta_DWD_ICON_EPS: 0, beta_ECCC_GEM: 0, w0: 0.25, w1: 0.1};\n"
              + "console.log(JSON.stringify([ngrFamilyWeights(T), ngrFamilyWeights(T, ['ECMWF', 'ECMWF_AIFS']),"
              " ngrFWeight(T, 36.9), ngrFWeight(T, 12)]));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    all_six, two, at36, at12 = json.loads(out.stdout)
    assert all_six["ECMWF_AIFS"] == pytest.approx(3 / 8) and sum(all_six.values()) == pytest.approx(1)
    assert two == pytest.approx({"ECMWF": 0.25, "ECMWF_AIFS": 0.75})
    assert at36 == pytest.approx(0.25) and at12 == pytest.approx(0.25 - 0.1)
