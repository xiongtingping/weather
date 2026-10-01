"""Each section names its own stale inputs (user 2026-10-01: 把最下方的数据过期
诊断放到各个板块，而不是单独在下面).

The build records, by object identity, which earlier cache fed each cache and
which payload keys each cache landed in; the page reads those to put a cache's
age inside every section that reads it, and the footer keeps only the rest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path / "sections")
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", True)
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"up": 3600, "down": 3700})
    C._SECTION_CACHE_LOG.clear()
    C._SECTION_CACHE_VALUES.clear()
    yield
    C._SECTION_CACHE_LOG.clear()
    C._SECTION_CACHE_VALUES.clear()


def _collect_up(conn):
    return {"rows": [1, 2], "summary": {"n": 2}}


def _collect_down(conn, feed):
    return {"total": len(feed["rows"])}


def test_a_cache_fed_another_cache_says_so_and_its_destinations_are_found(cache_dir) -> None:
    up = C._cached_section("up", _collect_up, None)
    down = C._cached_section("down", _collect_down, None, up)
    assert C._SECTION_CACHE_LOG["down"]["fed_by"] == ["up"]
    assert "fed_by" not in C._SECTION_CACHE_LOG["up"]
    payload = {"up_rows": up["rows"], "down": down, "unrelated": {"x": 1}, "section_cache": {}}
    log = C._section_cache_destinations(payload)
    assert log["up"]["payload_keys"] == ["up_rows"]       # a member of the cached dict
    assert log["down"]["payload_keys"] == ["down"]        # the cached dict itself
    # A second build reuses both from disk and still records the edge.
    C._SECTION_CACHE_LOG.clear(); C._SECTION_CACHE_VALUES.clear()
    up2 = C._cached_section("up", _collect_up, None)
    C._cached_section("down", _collect_down, None, up2)
    assert C._SECTION_CACHE_LOG["up"]["reused"] and C._SECTION_CACHE_LOG["down"]["fed_by"] == ["up"]


def test_the_page_reads_staleness_per_section_and_keeps_the_payload_marker() -> None:
    source = C.HTML_TEMPLATE
    # The payload line stays byte-identical: reinflate finds the payload by it.
    assert "\nconst D = __PAYLOAD__;\n" in source
    assert "function sectionStaleness(reads)" in source
    assert "SEC_ATTRIBUTED.has(k)" in source            # the footer keeps only the rest
    sec = source[source.index("const sec = (fn, name, id) => {"):]
    assert "sectionStaleness(reads)" in sec[:1200]


def test_the_overview_is_one_charter_240_board() -> None:
    source = C.HTML_TEMPLATE
    assert '[modelAccuracyBoard,"模型准不准 · 总检"]' in source
    assert '[scorecard,"模型校准总检"]' not in source
    assert '"模型准不准 · 总检": {layer:"概率层 · 读数"' in source
    assert "LogScore 主评分 → CRPS 结构验证 → Brier 护栏（宪章 240）" in source
