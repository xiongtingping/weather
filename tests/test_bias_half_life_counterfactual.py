"""The half-life comparison refits with production's functions, one constant apart.

``collect_bias_half_life`` corrects each settled day once per half-life,
refitted on the cases before that day, through ``native_ensemble_oos``'s
own loader, fit and centre resolver.  A station whose bias steps from +2C to
+5C part-way through pins what the reading must show: every arm corrects
towards truth (less error than the raw mean), a shorter half-life catches
the step sooner (H3 < H5 < H15), and the serving constant is back at five
days when the reading is done.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import native_ensemble_oos as OOS  # noqa: E402


@pytest.fixture(autouse=True)
def _own_cache_dir(tmp_path, monkeypatch):
    """The collector keeps its refits beside the section pages; a test must
    not write them into the live board's directory."""

    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path)


def _cases() -> list[dict]:
    start = date(2026, 7, 1)
    out = []
    for index in range(70):
        day = (start + timedelta(days=index)).isoformat()
        truth = 22.0 if index < 50 else 25.0
        out.append({"event_date": day, "values": [19.8, 20.0, 20.2],
                    "actual_tmax_c": truth})
    return out


def test_a_shorter_half_life_catches_a_step_sooner_and_every_arm_corrects(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_registry(station_id TEXT)")
    conn.execute("INSERT INTO calibration_station_registry VALUES ('KAAA')")
    cases = _cases()
    monkeypatch.setattr(
        OOS, "_load_cases",
        lambda conn, *, station_id, as_of_utc: {
            ("SYS", "PEAK", "h12_24", "00Z"): list(cases)
        },
    )

    result = C.collect_bias_half_life(conn)

    assert result["available"] is True
    assert OOS.BIAS_HALF_LIFE_DAYS == 5.0
    assert result["serving_half_life_days"] == 5.0
    assert result["window_to"] == cases[-1]["event_date"]
    mae = {arm["half_life_days"]: arm["mae_c"] for arm in result["arms"]}
    # Raw error is 2C before the step and 5C after; a correction applied the
    # wrong way round would add the bias instead.
    assert max(mae.values()) < 2.0
    assert mae[3.0] < mae[5.0] < mae[15.0]
    comparisons = {c["half_life_days"]: c for c in result["comparisons"]}
    assert comparisons[3.0]["mean_diff_c"] < 0 < comparisons[15.0]["mean_diff_c"]
    assert comparisons[15.0]["ci_low_c"] <= comparisons[15.0]["mean_diff_c"] <= (
        comparisons[15.0]["ci_high_c"])


def test_the_refits_are_kept_and_a_second_build_reads_them(monkeypatch) -> None:
    """2026-10-02: the fleet's refits were ~3,800 s a build, past the research
    timeout.  A settled cell's inputs do not move, so neither does its fit."""

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_station_registry (station_id TEXT)")
    conn.execute("INSERT INTO calibration_station_registry VALUES ('KAAA')")
    monkeypatch.setattr(OOS, "_load_cases", lambda _c, **_k: {("s",): _cases()})
    first = C.collect_bias_half_life(conn)
    fits = []
    real = OOS._fit_parameters
    monkeypatch.setattr(OOS, "_fit_parameters", lambda h: fits.append(1) or real(h))
    second = C.collect_bias_half_life(conn)
    assert fits == [], "every cell came from the kept refits"
    first.pop("measured_at_utc"), second.pop("measured_at_utc")
    assert first == second
