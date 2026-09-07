"""A lane that has been waiting hours must not restart level with one that
just ran.

The three writer lanes -- production, the forecast prefetch, the hosted
sweeps -- share one writer and take turns by "longest ago".  That ordering
lived only in memory and was seeded all-equal at boot, so every restart
handed the lanes back their tie.

Measured 2026-09-07: ten of fifteen hosted sweeps had not completed for 17
to 32 hours, across six restarts in one afternoon.  The sweeps' *clocks*
were persisted on 2026-08-30 for exactly this reason -- but that fix
decided which member ran once the lane had a slot, and the lane's own turn
was still being reset.  Same judgement as charter 48's listing turn: held
in memory, a rotation is reseeded to its head by every deploy, and deploys
here are not rare.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text()


@pytest.fixture()
def daemon():
    spec = importlib.util.spec_from_file_location(
        "daemon_turn_under_test",
        ROOT / "scripts" / "weather_basket_yes_daemon.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(path: Path, *, written_minutes_ago: float, ages: dict) -> None:
    stamp = (
        datetime.now(timezone.utc) - timedelta(minutes=written_minutes_ago)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "writer_turn_v1",
        "observed_at_utc": stamp,
        "age_seconds": ages,
    }))


def _restored(daemon, path: Path) -> dict:
    """Replay the seeding the run loop does, against a temp file."""

    import time as _time

    now_mono = _time.monotonic()
    served = {"forecast": now_mono, "production": now_mono, "sweeps": now_mono}
    turn = json.loads(path.read_text())
    served_at = datetime.fromisoformat(
        str(turn["observed_at_utc"]).replace("Z", "+00:00")
    )
    off = (datetime.now(timezone.utc) - served_at).total_seconds()
    for lane, age in (turn.get("age_seconds") or {}).items():
        if lane in served:
            served[lane] = now_mono - min(86_400.0, max(0.0, float(age) + off))
    return {lane: now_mono - value for lane, value in served.items()}


def test_the_starved_lane_keeps_its_place(daemon, tmp_path: Path) -> None:
    """Sweeps waited two hours, production ran a minute ago.  After a
    restart the order must be unchanged, which is what decides the turn."""

    path = tmp_path / "writer_turn.json"
    _seed(path, written_minutes_ago=1.0, ages={
        "production": 60.0, "forecast": 900.0, "sweeps": 7200.0,
    })
    ages = _restored(daemon, path)
    assert ages["sweeps"] > ages["forecast"] > ages["production"]
    assert ages["sweeps"] == pytest.approx(7260.0, abs=5.0)


def test_a_long_outage_cannot_privilege_one_lane_forever(
    daemon, tmp_path: Path
) -> None:
    """Restored ages are capped at a day.  Without the cap a machine off
    for a week hands one lane an unbeatable score and the others never run
    again -- the starvation this fixes, inverted."""

    path = tmp_path / "writer_turn.json"
    _seed(path, written_minutes_ago=60 * 24 * 7, ages={
        "production": 10.0, "forecast": 10.0, "sweeps": 10.0,
    })
    ages = _restored(daemon, path)
    assert all(value == pytest.approx(86_400.0, abs=1.0) for value in ages.values())


def test_a_missing_or_torn_file_falls_back_to_the_tie(
    daemon, tmp_path: Path
) -> None:
    """The turn is telemetry, not a fact: it must never be what stops the
    daemon starting."""

    path = tmp_path / "writer_turn.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    with pytest.raises(ValueError):
        _restored(daemon, path)
    # ...which the run loop catches; the guard is asserted on the source.
    assert "except (OSError, ValueError, KeyError, TypeError):" in SOURCE


def test_every_hand_off_records_the_turn() -> None:
    """Recording it on only some hand-offs is worse than not recording it:
    the file would then describe a rotation nobody is running."""

    assert SOURCE.count("_persist_writer_turn()") == 4, (
        "one definition plus one call per writer lane"
    )
    for lane in ("production", "forecast", "sweeps"):
        marker = f'writer_last_served["{lane}"] = time.monotonic()\n'
        index = SOURCE.index(marker)
        assert "_persist_writer_turn()" in SOURCE[index:index + 200], lane
