"""The D0 observation model as a read-only station-board card (user
2026-10-15 「不要结案，作为只读，在前端"站点看板"增加只读卡片」)."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bayes_basket_target import station_day_board as board

ROOT = Path(__file__).resolve().parents[1]
NUMERIC = ["t_minus_fc", "warmed_gap", "momentum", "high_minus_base", "fc_rise", "dew_dep", "dew_minus_fc_dew",
           "wind", "cloud_low", "precip_now", "precip_any", "sun_pct", "cloud_gap", "rain_gap", "hours_to_peak"]
FLAGGED = ["cloud_gap", "dew_dep", "rain_gap", "sun_pct"]


def _model(tmp_path):
    nf = len(NUMERIC) + len(FLAGGED)
    beta = [0.0] * nf + [0.4] + [0.5]           # one station effect (+0.4), intercept +0.5
    beta[NUMERIC.index("t_minus_fc")] = 0.3      # placed before any indicator in the layout
    band = {"hours": [0, 24], "n": 1, "mu": [0.0] * nf, "sd": [1.0] * nf, "nf": nf, "beta": beta,
            "gamma": [0, 0, 0, 0, 0, 0.0], "smu": [0.0] * 5, "ssd": [1.0] * 5, "scale": 1.0,
            "walk_forward_mae": {"market": 0.59, "p1": 0.70, "d0": 0.80}}
    path = tmp_path / "d0.json"
    path.write_text(json.dumps({"schema": "d0_obs_model_v1", "numeric": NUMERIC, "missing_indicator_after": FLAGGED,
                                "stations": ["ZUUU"], "bands": {"all": band}, "window": ["2026-09-01", "2026-10-14"]}))
    return path


def test_the_card_applies_the_stored_fit_to_the_boards_own_readings(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(board, "D0_MODEL_PATHS", (tmp_path / "absent.json", _model(tmp_path)))
    zone = ZoneInfo("Asia/Shanghai")
    now = datetime.now(timezone.utc)
    day = now.astimezone(zone).date().isoformat()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_regional_point_run (ensemble_system_id TEXT, station_id TEXT,"
                 " target_local_date TEXT, knowable_at_utc TEXT, forecast_tmax_c REAL, run_init_utc TEXT)")
    for system, v in zip(board.D0_OFFICIAL, (24.0, 25.0, 26.0)):
        conn.execute("INSERT INTO calibration_regional_point_run VALUES (?,?,?,?,?,?)",
                     (system, "ZUUU", day, "2000-01-01T00:00:00Z", v, "2000-01-01T00:00:00Z"))
    at = lambda m: (now - timedelta(minutes=m)).isoformat().replace("+00:00", "Z")
    metar = [{"valid_at_utc": at(90), "temp_c": 20.0, "dew_c": 15.0, "wind_mps": 2.0, "cloud_cover": 0.5, "precip": False},
             {"valid_at_utc": at(10), "temp_c": 22.0, "dew_c": 15.0, "wind_mps": 2.0, "cloud_cover": 0.5, "precip": False}]
    rows = [{"hour": k, "past": True, "forecast": {"temp_c_d1": 21.0, "sw_d1": 0.0, "cloud_d1": 50.0}} for k in range(24)]
    axis = [{"bucket": f"{t}°C", "lo_c": t - 0.5, "hi_c": t + 0.5} for t in range(20, 30)]
    out = board._d0_reading(conn, "ZUUU", day, zone, axis, metar, {"rows": rows}, None, None)
    # base 25 (median) + 0.3 * (22 - 21) + 0.4 station + 0.5 intercept = 26.2
    assert out["state"] == "ok" and out["base_c"] == 25.0 and out["base_systems"] == 3
    assert out["centre_c"] == 26.2 and out["mode"] == "26°C"
    assert sum(out["vector"].values()) == pytest.approx(1.0, abs=1e-3)
    assert out["vector"]["20°C"] == pytest.approx(0.001, abs=5e-4)       # below the running high: floor only


def test_states_without_a_model_or_reports(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(board, "D0_MODEL_PATHS", (tmp_path / "absent.json",))
    assert board._d0_reading(None, "ZUUU", "2026-10-15", ZoneInfo("Asia/Shanghai"), [], [], {}, None, None) == {"state": "no_model"}


def test_the_card_renders() -> None:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required")
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    start = source.index("const SD_D0_FEATURE")
    end = source.index("function sdDeterministic(day){")
    day = {"d0": {"state": "ok", "band": "10-12", "base_c": 24.87, "base_systems": 3, "centre_c": 25.28, "sigma_c": 0.46,
                  "mode": "25°C", "mode_p": 0.68, "station_effect_c": 0.0, "window": ["2026-09-01", "2026-10-14"],
                  "as_of_utc": "2026-10-15T06:00:00Z",
                  "parts": [{"feature": "high_minus_base", "c": -0.53, "value": 0.13}],
                  "walk_forward_mae": {"market": 0.59, "p1": 0.70, "d0": 0.80}},
           "stages": {"P1": {"stats": {"mean_c": 25.1}}}, "market": {"stats": {"mean_c": 25.4}}}
    script = ('const sdEsc = s => String(s == null ? "" : s);\n'
              'const sdN = (v, d) => v == null || !isFinite(v) ? "—" : Number(v).toFixed(d == null ? 2 : d);\n'
              'const sdSg = (v, d) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d == null ? 2 : d);\n'
              'const sdPc = (v, d) => (100 * v).toFixed(d) + "%"; function sdLocal(t){ return String(t); }\n'
              + source[start:end] + f"\nconsole.log(sdD0({json.dumps(day, ensure_ascii=False)}).replace(/<[^>]+>/g, '|'));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    text = "|".join(x for x in out.stdout.strip().split("|") if x)
    for piece in ("25.28°C", "σ 0.46", "众数 25°C", "比P1 +0.18°C", "比市场 −0.12°C", "已报最高−基线 −0.53",
                  "市场 0.59 · P1 0.70 · D0 0.80°C"):
        assert piece in text, (piece, text)
    assert "${sdD0(day)}" in source and (ROOT / "config" / "ui" / "d0_obs_model_v1.json").exists()
