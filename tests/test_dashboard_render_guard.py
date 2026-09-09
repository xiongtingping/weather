"""The board refuses to be overwritten by an OLDER build, not a slower one.

A full render takes minutes and two of them run on different clocks: the
hosted sweep on a timer, and whoever just edited a section by hand.  The
guard exists so the sweep cannot put pre-edit HTML back on the page.

It compared file mtimes against the start of the current build, which asks
"did anyone write after I started" -- a different question.  On 2026-09-09 a
sweep that began at 14:34 finished at 15:15 and a hand render that began at
15:14, carrying the edit, finished at 15:28; the guard discarded the newer
code because the older process had touched the file more recently.  The
audit panel the edit was for stayed stale on the page and read exactly like
an edit that never landed.

A process runs whatever was on disk when it imported, so its START is its
code vintage.  These tests pin that comparison.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _write(target: Path, started_at: float) -> None:
    target.write_text(
        C.render_html({"generated_at_utc": "x"}, started_at=started_at),
        encoding="utf-8",
    )


def test_the_stamp_is_in_the_first_line(tmp_path) -> None:
    """The check must never have to read the seven megabytes behind it."""

    target = tmp_path / "board.html"
    _write(target, 1000.0)
    with target.open("r", encoding="utf-8") as handle:
        assert C.RENDER_STAMP_RE.search(handle.read(200)) is not None
    assert C._render_started_at(target) == pytest.approx(1000.0)


def test_a_build_that_began_later_is_not_overwritten(tmp_path) -> None:
    """The case the guard is for: someone else's newer build already landed."""

    target = tmp_path / "board.html"
    _write(target, 2000.0)
    assert C._newer_render_landed(target, 1500.0) is True


def test_a_build_that_began_earlier_does_not_win_by_finishing_last(tmp_path) -> None:
    """The regression.  The sweep started at 14:34 and wrote at 15:15; the
    hand render started at 15:14 and must still be allowed to write, because
    it imported the newer code."""

    target = tmp_path / "board.html"
    sweep_started, hand_started = 1000.0, 1600.0
    _write(target, sweep_started)          # the sweep lands first...
    # ...and its file is now the most recently modified thing on disk, which
    # is all the old rule looked at.  Assert that the old rule really would
    # have skipped here, so this test fails if the scenario stops being the
    # one that bit -- and that the new rule does not.
    assert target.stat().st_mtime > hand_started
    assert C._newer_render_landed(target, hand_started) is False


def test_an_unstamped_file_falls_back_to_the_mtime_rule(tmp_path) -> None:
    """Files written before the stamp existed are older code by
    construction, so the conservative old test is the right one for them."""

    target = tmp_path / "board.html"
    target.write_text("<!doctype html><html></html>", encoding="utf-8")
    mtime = target.stat().st_mtime
    assert C._newer_render_landed(target, mtime - 100.0) is True
    assert C._newer_render_landed(target, mtime + 100.0) is False


def test_a_missing_file_is_never_a_newer_render(tmp_path) -> None:
    assert C._newer_render_landed(tmp_path / "absent.html", 1.0) is False


def test_the_stamp_does_not_survive_as_a_placeholder(tmp_path) -> None:
    """A template token left unreplaced would parse as the literal string and
    every later comparison would silently fall back to mtime."""

    html = C.render_html({"generated_at_utc": "x"})
    assert "__RENDER_STARTED__" not in html
    assert "__PAYLOAD__" not in html
