"""The page render reads no SSOT (user ruling 2026-10-15).

「渲染应该只读，不应该花这么久」 / 「页面渲染零查库，各节全搬到后端读数任务」:
the page drew by running every section's SQL against the store, so one slow
read (the refusal funnel's 1,098 s) took the whole render past its 1,500 s
timeout and left the board 57 minutes old.  Now the backend compute run
(``--compute-pages``) and the research run keep every section's value and
the page (``--html-out``) draws from those alone, with no connection at all.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


@pytest.fixture()
def read_only(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PAGE_READ_ONLY", True)
    monkeypatch.setattr(C, "PAGED_RENDER", False)
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path / "sections")
    monkeypatch.setattr(C, "STATUS_JSON", tmp_path / "absent_status.json")
    monkeypatch.setattr(C, "NATIVE_FORWARD_INBOX", tmp_path / "absent_inbox.json")
    C._SECTION_CACHE_LOG.clear()
    C._SECTION_CACHE_VALUES.clear()

    def refuse(*_a, **_k):
        raise AssertionError("the page render opened a database connection")

    monkeypatch.setattr(C, "_connect", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)
    return tmp_path


def test_the_page_draws_with_no_connection_on_an_empty_cache(read_only) -> None:
    payload = C.build_payload(read_only / "no_such.db")
    assert payload["render"]["read_only"] is True
    assert C._SECTION_CACHE_LOG["members"]["unavailable"] is True
    json.dumps(payload)  # an empty section serialises as {}


def test_the_page_serves_what_the_backend_kept(read_only) -> None:
    path = C.SECTION_CACHE_DIR / "station_markets.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "value": {"KLGA": {"slug": "x", "event_date": "2026-10-15"}},
        "fingerprint": "f",
        "code_fingerprint": C._section_fingerprint(C.collect_station_markets, ""),
        "computed_at_utc": C._now().isoformat().replace("+00:00", "Z"),
    }), encoding="utf-8")
    payload = C.build_payload(read_only / "no_such.db")
    assert payload["station_markets"] == {"KLGA": {"slug": "x", "event_date": "2026-10-15"}}
    assert C._SECTION_CACHE_LOG["station_markets"]["reused"] is True


def test_a_kept_value_from_other_code_is_not_served(read_only) -> None:
    path = C.SECTION_CACHE_DIR / "station_markets.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "value": {"OLD": {}}, "code_fingerprint": "retired",
        "computed_at_utc": C._now().isoformat().replace("+00:00", "Z"),
    }), encoding="utf-8")
    payload = C.build_payload(read_only / "no_such.db")
    assert payload["station_markets"] == {}


def test_any_use_of_the_stand_in_connection_names_the_bug() -> None:
    with pytest.raises(C.PageReadsNoSSOT):
        C._NoSSOT().execute("SELECT 1")


def test_the_sweep_draws_the_page_read_only_after_the_compute_run() -> None:
    sweep = (ROOT / "scripts" / "dashboard_render_sweep.sh").read_text()
    assert "--compute-pages" in sweep and "--html-out" in sweep
    assert sweep.index("--compute-pages;") < sweep.rindex("draw_page")
    for name in ("board_readings_sweep.sh", "dashboard_research_sweep.sh"):
        assert "calibration_dashboard.py --compute-pages" in (ROOT / "scripts" / name).read_text()
    assert C.PAGE_RENDER_PATTERN == "calibration_dashboard.py --compute-pages"


def test_the_live_switch_has_its_state_without_any_section(read_only) -> None:
    # 2026-10-15 「顶部交易开关不见了」: the strip hid while live_readiness had
    # no kept value.  Its first state is the intent file, read directly.
    payload = C.build_payload(read_only / "no_such.db")
    assert set(payload["live_intent"]) == {"live_enabled"}
    source = Path(C.__file__).read_text(encoding="utf-8")
    for name in ("function liveSwitch(){", "function liveControlStrip(){"):
        body = source.split(name, 1)[1].split("\n}\n", 1)[0]
        assert "if(!L.available) return" not in body, name
