"""The render redraws when the template moved under it (2026-09-20).

A full render imports the template once and takes twenty minutes to an hour
and a half to write the page.  A template edit that lands inside that window
is overwritten when it writes -- and ``dashboard_reinflate`` will not put it
back, because its WatchPaths fire on changes to the source and the source
has not changed since.  On 2026-09-20 the board reverted to the previous
day's layout and stayed there.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / "scripts" / "dashboard_render_sweep.sh"


def test_the_render_redraws_when_the_template_moved_under_it() -> None:
    text = SWEEP.read_text(encoding="utf-8")
    assert 'BEFORE="$(stat -f \'%m:%z\' "$SOURCE")"' in text, (
        "the signature has to be taken before the render, not after"
    )
    before = text.index('BEFORE=')
    render = text.index("--html-out tmp/cal_dashboard.html")
    check = text.index('if [ "$(stat -f \'%m:%z\' "$SOURCE")" != "$BEFORE" ]')
    redraw = text.index("--reinflate tmp/cal_dashboard.html")
    assert before < render < check < redraw, (
        "signature, render, compare, redraw -- in that order"
    )
    # The same signature the hot-update watcher keys on, so the two agree
    # about what "the template changed" means.
    watcher = (ROOT / "scripts" / "dashboard_reinflate.sh").read_text(encoding="utf-8")
    assert "stat -f '%m:%z'" in watcher


def test_the_redraw_cannot_fail_the_render() -> None:
    text = SWEEP.read_text(encoding="utf-8")
    line = [l for l in text.split("\n") if "--reinflate" in l][0]
    assert line.rstrip().endswith("|| true"), (
        "set -e is on; a redraw that cannot parse the template must not make "
        "the render look like it failed after it already wrote the page"
    )
