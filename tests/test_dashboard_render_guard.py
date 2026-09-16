"""The board is never overwritten by older data, and redraws its template hot.

A full render takes twenty minutes and two run on different clocks: the
render timer and whoever renders by hand.  Until 2026-09-16 a render drew
its page with the template it imported when it started, so the guard had to
compare code vintages -- a sweep that began before an edit put pre-edit HTML
back (2026-08-26), and comparing write times let the older process win
whenever it finished last (2026-09-09).

User ruling 2026-09-16: 前端改为热更新.  Every page is now drawn with the
template on disk at the moment it is written, around the newest payload a
build kept (``reinflate``), so code can no longer regress and the guard
compares data vintages.  The page carries both stamps in its first line.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _write(target: Path, started_at: float, payload_started_at=None) -> None:
    target.write_text(
        C.render_html(
            {"generated_at_utc": "x"}, started_at=started_at,
            payload_started_at=payload_started_at,
        ),
        encoding="utf-8",
    )


def test_both_stamps_are_in_the_first_line(tmp_path) -> None:
    """The check must never have to read the megabytes behind it."""

    target = tmp_path / "board.html"
    _write(target, 2000.0, payload_started_at=1000.0)
    with target.open("r", encoding="utf-8") as handle:
        head = handle.read(300)
    assert C.RENDER_STAMP_RE.search(head) is not None
    assert C.PAYLOAD_STAMP_RE.search(head) is not None
    assert C._render_started_at(target) == pytest.approx(2000.0)
    assert C._payload_started_at(target) == pytest.approx(1000.0)


def test_a_page_from_before_the_data_stamp_reads_its_render_stamp(tmp_path) -> None:
    """A full render had one start for template and data alike."""

    target = tmp_path / "board.html"
    target.write_text(
        "<!doctype html><!--render-start:1500.000-->\n<html></html>",
        encoding="utf-8",
    )
    assert C._payload_started_at(target) == pytest.approx(1500.0)


def test_newer_data_on_the_page_is_not_overwritten(tmp_path) -> None:
    target = tmp_path / "board.html"
    _write(target, 2000.0)
    assert C._newer_payload_landed(target, 1500.0) is True


def test_a_build_that_began_earlier_does_not_win_by_finishing_last(tmp_path) -> None:
    """The 2026-09-09 regression, on the data vintage: the sweep started at
    14:34 and wrote at 15:15; the hand render started at 15:14 and must
    still be allowed to write."""

    target = tmp_path / "board.html"
    sweep_started, hand_started = 1000.0, 1600.0
    _write(target, sweep_started)
    assert target.stat().st_mtime > hand_started
    assert C._newer_payload_landed(target, hand_started) is False


def test_a_hot_update_does_not_make_the_next_build_look_old(tmp_path) -> None:
    """A redraw moves the page's template stamp, not its data stamp."""

    target = tmp_path / "board.html"
    _write(target, started_at=9000.0, payload_started_at=1000.0)
    assert C._newer_payload_landed(target, 1600.0) is False


def test_an_unstamped_file_falls_back_to_the_mtime_rule(tmp_path) -> None:
    target = tmp_path / "board.html"
    target.write_text("<!doctype html><html></html>", encoding="utf-8")
    mtime = target.stat().st_mtime
    assert C._newer_payload_landed(target, mtime - 100.0) is True
    assert C._newer_payload_landed(target, mtime + 100.0) is False


def test_a_missing_file_is_never_newer(tmp_path) -> None:
    assert C._newer_payload_landed(tmp_path / "absent.html", 1.0) is False


def test_no_stamp_survives_as_a_placeholder() -> None:
    """A token left unreplaced parses as a literal and every comparison
    silently falls back to mtime."""

    html = C.render_html({"generated_at_utc": "x"})
    for token in ("__RENDER_STARTED__", "__PAYLOAD_STARTED__", "__PAYLOAD__"):
        assert token not in html


# ------------------------------------------------------------- hot update


def _saved(tmp_path, payload, started_at) -> Path:
    target = tmp_path / "board.html"
    assert C._save_payload(target, payload, started_at) is True
    return target


def test_a_hot_update_draws_the_saved_payload_and_publishes_its_version(tmp_path) -> None:
    target = _saved(tmp_path, {"generated_at_utc": "kept"}, 1000.0)

    assert C.reinflate(target) == str(target)
    assert C._payload_started_at(target) == pytest.approx(1000.0)
    drawn = C._render_started_at(target)
    assert drawn > 1000.0
    assert C._payload_in_page(target) == ({"generated_at_utc": "kept"}, 1000.0)
    version = C._version_sidecar(target).read_text(encoding="utf-8")
    assert version == f'window.__BOARD_VERSION__ = "{drawn:.3f}";\n'


def test_an_older_build_never_replaces_the_kept_payload(tmp_path) -> None:
    target = _saved(tmp_path, {"generated_at_utc": "new"}, 2000.0)
    assert C._save_payload(target, {"generated_at_utc": "old"}, 1000.0) is False
    kept = json.loads(C._payload_sidecar(target).read_text(encoding="utf-8"))
    assert kept["payload"] == {"generated_at_utc": "new"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node checks the script")
def test_a_template_whose_script_does_not_parse_leaves_the_page_up(tmp_path) -> None:
    """A save in the middle of an edit must not blank the page."""

    target = _saved(tmp_path, {"generated_at_utc": "x"}, 1000.0)
    C.reinflate(target)
    before = target.read_text(encoding="utf-8")

    def broken(payload, started_at=None, payload_started_at=None):
        return C.render_html(payload, started_at, payload_started_at).replace(
            "function renderStamp(){", "function renderStamp({", 1
        )

    said = C.reinflate(target, renderer=broken)
    assert "does not parse" in said
    assert target.read_text(encoding="utf-8") == before


def test_a_page_drawn_before_payloads_were_kept_supplies_its_own(tmp_path) -> None:
    """Braces inside labels must not cut the recovered payload short."""

    target = tmp_path / "board.html"
    payload = {"generated_at_utc": "x", "label": "{均值桶, 尾侧一格}"}
    target.write_text(
        C.render_html(payload, started_at=1234.0), encoding="utf-8"
    )
    assert C.reinflate(target) == str(target)
    assert C._payload_in_page(target) == (payload, 1234.0)


def test_a_kept_payload_older_than_the_page_does_not_overwrite_it(tmp_path) -> None:
    target = _saved(tmp_path, {"generated_at_utc": "old"}, 1000.0)
    _write(target, started_at=3000.0)
    said = C.reinflate(target)
    assert "newer data" in said
    assert C._payload_started_at(target) == pytest.approx(3000.0)


def test_the_write_is_a_rename_so_a_reload_never_reads_half_a_page(tmp_path) -> None:
    target = _saved(tmp_path, {"generated_at_utc": "x"}, 1000.0)
    C.reinflate(target)
    inode = os.stat(target).st_ino
    C.reinflate(target)
    assert os.stat(target).st_ino != inode
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_the_watch_is_installed_on_the_template_and_its_directory() -> None:
    import plistlib  # noqa: PLC0415

    plist = plistlib.loads(
        (ROOT / "launchd" / "com.weather.dashboard_reinflate.plist").read_bytes()
    )
    script = ROOT / "scripts" / "dashboard_reinflate.sh"
    # Compared by tail: the plist names the installed checkout, and a test
    # may run from another one.
    assert plist["ProgramArguments"][-1].endswith("/scripts/dashboard_reinflate.sh")
    watched = plist["WatchPaths"]
    assert any(p.endswith("/scripts/calibration_dashboard.py") for p in watched)
    assert any(p.endswith("/scripts") for p in watched)
    assert os.access(script, os.X_OK)
    body = script.read_text(encoding="utf-8")
    assert "single_instance dashboard_reinflate" in body
    assert '--reinflate "$PAGE"' in body
