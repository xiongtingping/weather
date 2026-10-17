"""Each due realisation stage is refitted on its own, the stalest first
(2026-10-16: one process and one timeout for all three left P0-M's artifact
at 10-12 and P1's at 10-10 while P0 used the whole budget)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "_refit_realisation_layer", ROOT / "scripts" / "refit_realisation_layer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_due_stages_are_the_stale_ones_stalest_first(monkeypatch, tmp_path) -> None:
    script = _script()
    monkeypatch.setattr(script, "ATTEMPT_DIR", tmp_path)   # no killed-run marks from the live machine
    store = script.store
    artifacts = {
        "P0": {"fitted_at_utc": "2026-10-16T07:17:50Z"},   # current
        "P0-M": {"fitted_at_utc": "2026-10-12T04:19:51Z"},
        "P1": {"fitted_at_utc": "2026-10-10T07:02:04Z"},
    }
    monkeypatch.setattr(store, "load_active", lambda stage="P0", **_: artifacts.get(stage))
    monkeypatch.setattr(store, "artifact_is_current", lambda a, **_: a is artifacts["P0"])
    assert script.due_stages() == ["P1", "P0-M"]
    artifacts["P0-M"] = None  # never fitted: before any that has a fit
    assert script.due_stages() == ["P0-M", "P1"]


def test_the_sweep_calls_each_due_stage_with_its_own_timeout() -> None:
    text = (ROOT / "scripts" / "board_readings_sweep.sh").read_text(encoding="utf-8")
    loop = text[text.index('for stage in $("$PY" scripts/refit_realisation_layer.py --list-due)'):]
    loop = loop[:loop.index("done")]
    assert "run_with_timeout" in loop and '--stage "$stage"' in loop


def test_a_stage_killed_by_its_timeout_is_not_offered_again_for_six_hours(tmp_path, monkeypatch) -> None:  # noqa: D103
    """2026-10-17: P0 and P1 ran their full 1,800 s, were killed, were due again
    the next hour, and held the shared SSOT reader turn ~1.5 h an hour."""

    import os
    import time
    import refit_realisation_layer as R

    monkeypatch.setattr(R, "ATTEMPT_DIR", tmp_path)
    mark = tmp_path / "P0.started"
    mark.touch()
    assert not R._killed_recently("P0"), "a stage still inside its timeout is running, not killed"
    old = time.time() - 30 * 60
    os.utime(mark, (old, old))
    assert R._killed_recently("P0")
    ancient = time.time() - 7 * 3600
    os.utime(mark, (ancient, ancient))
    assert not R._killed_recently("P0"), "after the backoff it is offered again"
    assert not R._killed_recently("P1"), "no mark, no backoff"
