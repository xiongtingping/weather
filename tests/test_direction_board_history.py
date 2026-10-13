"""The direction snapshot keeps each station-day's distinct readings (user
2026-10-12 「隐藏细节，突出变化」): the board shows what changed and when."""

from __future__ import annotations

import json
from pathlib import Path

from bayes_basket_target import direction_signal as ds

ROOT = Path(__file__).resolve().parents[1]


def _doc(at: str, *, momentum: float = -6.02, mode: str = "26°C", q: float = 0.95,
         run: str = "2026-10-12T00:00:00Z", score: float = -0.16) -> dict:
    return {"schema": ds.BOARD_SCHEMA, "computed_at_utc": at, "stations": {"ZUUU": {"2026-10-12": {
        "side": -1 if momentum < 0 else 1, "momentum": momentum, "momentum_side": -1 if momentum < 0 else 1,
        "model_score": score, "model_side": -1,
        "momentum_components": {"aifs_ens": {"delta_c": -0.6, "newest_run_init_utc": run,
                                             "older_run_init_utc": "2026-10-11T18:00:00Z"}},
        "mode": {"state": "ok", "mode": mode, "mode_q": q}}}}}


def _history(doc: dict) -> list[dict]:
    return doc["stations"]["ZUUU"]["2026-10-12"]["history"]


def test_the_first_reading_is_a_baseline_and_an_unchanged_one_adds_nothing() -> None:
    first = ds.with_history(_doc("2026-10-12T08:00:00Z"), None)
    assert [h["at_utc"] for h in _history(first)] == ["2026-10-12T08:00:00Z"]
    assert _history(first)[0]["baseline"] is True
    # The modal probability and the model score move every snapshot; neither is a change.
    second = ds.with_history(_doc("2026-10-12T08:10:00Z", q=0.97, score=-0.17), first)
    assert [h["at_utc"] for h in _history(second)] == ["2026-10-12T08:00:00Z"]


def test_a_new_run_a_new_momentum_or_a_new_mode_is_a_change() -> None:
    doc = ds.with_history(_doc("2026-10-12T08:00:00Z"), None)
    doc = ds.with_history(_doc("2026-10-12T12:00:00Z", momentum=-2.4, run="2026-10-12T06:00:00Z"), doc)
    doc = ds.with_history(_doc("2026-10-12T12:30:00Z", momentum=-2.4, run="2026-10-12T06:00:00Z", mode="25°C"), doc)
    hist = _history(doc)
    assert [h["at_utc"] for h in hist] == ["2026-10-12T08:00:00Z", "2026-10-12T12:00:00Z", "2026-10-12T12:30:00Z"]
    assert hist[1]["momentum"] == -2.4 and hist[1]["newest_run_utc"] == "2026-10-12T06:00:00Z"
    assert hist[2]["mode"] == "25°C" and "baseline" not in hist[2]


def test_the_history_is_bounded_and_survives_through_the_file(tmp_path) -> None:
    path = tmp_path / "board.json"
    for i in range(ds.BOARD_HISTORY_MAX + 3):
        doc = ds.with_history(_doc(f"2026-10-12T{i:02d}:00:00Z", momentum=-1.0 - i), ds._read_board(path))
        ds.write_board_snapshot(doc, path)
    hist = _history(json.loads(path.read_text(encoding="utf-8")))
    assert len(hist) == ds.BOARD_HISTORY_MAX and hist[-1]["momentum"] == -1.0 - (ds.BOARD_HISTORY_MAX + 2)


def test_the_resident_job_writes_the_history_and_the_card_shows_the_change() -> None:
    source = (ROOT / "bayes_basket_target" / "direction_signal.py").read_text(encoding="utf-8")
    assert "write_board_snapshot(with_history(doc, _read_board(board_path)), board_path)" in source
    dashboard = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "sdDirChange(hist[hist.length-1]" in dashboard and "<summary>细节</summary>" in dashboard


def test_the_card_grades_momentum_in_five_tiers() -> None:
    """User 2026-10-13: M shown as 很弱/弱/中/强/很强 (|M| cuts 1/2/3/5)."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node not installed")
    dashboard = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    fn = dashboard[dashboard.index("function sdMomTier"):dashboard.index("function sdDirRun")]
    probe = fn + 'console.log([null,-0.12,1,-2.5,3,4.99,5].map(m=>sdMomTier(m).replace(/<[^>]+>/g,"")).join(","))'
    out = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    assert out == ",很弱,弱,中,强,强,很强"
