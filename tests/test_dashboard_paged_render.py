"""The paged render (user ruling 2026-09-24: 「能分区分页渲染吗？不要一次性，找生产的空隙」).

The render used to be one read of the whole store.  The last one to finish
(2026-09-23 06:53Z) took 7,337 s, every run after 13:18Z was killed at the
1,500 s deadline, and two were killed by hand because their reads held the
WAL the resident needed truncated before it could reopen its writer.  The
page kept an eighteen-hour-old payload with red lamps on it.

What is pinned here: a section is a page of its own -- written when done,
reused by the next run, served past its lifetime once the run's budget is
spent -- it starts reading only in a production gap, and it gives its read
up when the WAL crosses the resident's maintenance line.  The sections where
a stale answer is a wrong answer are never paged.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402


def _safety_rule_sets() -> tuple[set[str], set[str]]:
    """The two sets the section cache's safety rules pin, read from that file."""

    import ast

    tree = ast.parse((ROOT / "tests" / "test_dashboard_section_cache.py").read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("HEALTH_BANNER_SOURCES", "LIVE_SECTIONS"):
                found[node.targets[0].id] = set(ast.literal_eval(node.value))
    return found["HEALTH_BANNER_SOURCES"], found["LIVE_SECTIONS"]


HEALTH_BANNER_SOURCES, LIVE_BOOK_SECTIONS = _safety_rule_sets()


@pytest.fixture()
def paged(tmp_path, monkeypatch):
    db = tmp_path / "ssot.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(C, "SECTION_CACHE_DIR", tmp_path / "sections")
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", True)
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"slow_moving": 3600})
    monkeypatch.setattr(C, "STATUS_JSON", tmp_path / "status.json")
    monkeypatch.setattr(C, "RENDER_GAP_POLL_SEC", 0.01)
    C._SECTION_CACHE_LOG.clear()
    C._paged_run_start(db)
    yield tmp_path
    C._PAGED_RUN.clear()
    C._SECTION_CACHE_LOG.clear()


def _collector(calls, value="v"):
    def collect(conn):
        calls.append(value)
        return {"value": value, "n": len(calls)}
    return collect


def _wal(tmp_path: Path, size: int) -> None:
    (tmp_path / "ssot.db-wal").write_bytes(b"\0" * size)


def _status(tmp_path: Path, **fields) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    (tmp_path / "status.json").write_text(
        json.dumps({"heartbeat_at": now, **fields}), encoding="utf-8"
    )


def test_a_section_without_a_lifetime_becomes_a_page_the_next_run_reuses(paged) -> None:
    calls: list[str] = []
    first = C._cached_section("analytics", _collector(calls), None)
    assert (paged / "sections" / "analytics.json").exists(), "the page must be written when done"
    C._SECTION_CACHE_LOG.clear()
    second = C._cached_section("analytics", _collector(calls), None)
    assert calls == ["v"], "the next run must resume from the page, not recompute"
    assert first == second
    assert C._SECTION_CACHE_LOG["analytics"]["reused"] is True


@pytest.mark.parametrize("name", sorted(C.RENDER_EVERY_RUN_SECTIONS))
def test_the_every_run_sections_are_never_paged(paged, name) -> None:
    calls: list[str] = []
    C._cached_section(name, _collector(calls), None)
    C._cached_section(name, _collector(calls), None)
    assert calls == ["v", "v"]
    assert not (paged / "sections" / f"{name}.json").exists()


def test_the_every_run_set_covers_both_safety_rules() -> None:
    """The banner's inputs and the live book, by their section names."""

    payload_to_section = {"truth": "truth_materialization"}
    banner = {
        payload_to_section.get(key, key) for key in HEALTH_BANNER_SOURCES
    } - {"worker"}  # read from the status file, not a section
    missing = (banner | set(LIVE_BOOK_SECTIONS)) - C.RENDER_EVERY_RUN_SECTIONS
    assert not missing, f"{sorted(missing)} would be served from a stale page"


def test_a_spent_budget_serves_the_last_page_and_says_so(paged, monkeypatch) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    # Past its lifetime, and the run's budget is gone.
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    C._SECTION_CACHE_LOG.clear()
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert calls == ["old"], "a spent budget must not start another read"
    assert value["value"] == "old"
    entry = C._SECTION_CACHE_LOG["slow_moving"]
    assert entry["stale"] is True and entry["reused"] is True
    assert entry["stale_reason"] == "budget"
    assert C._PAGED_RUN["stale"] == {"slow_moving": "budget"}


def test_a_page_is_never_served_across_a_code_change(paged, monkeypatch) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    # A code change moves both: the full key and the code's own.
    blob["fingerprint"] = "retired"
    blob["code_fingerprint"] = "retired"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new", "a page from other code has another shape"


def test_a_section_waits_for_the_production_cycle_to_finish(paged, monkeypatch) -> None:
    _status(paged, production_inflight={"elapsed_sec": 30})
    seen: list[str | None] = []
    real = C._production_gap_blocker

    def blocker(wal_line=None):
        verdict = real(wal_line)
        seen.append(verdict)
        if len(seen) == 3:
            _status(paged)  # the cycle finished
        return verdict

    monkeypatch.setattr(C, "_production_gap_blocker", blocker)
    calls: list[str] = []
    C._cached_section("analytics", _collector(calls), None)
    assert seen[:3] == ["production cycle in flight"] * 3 and seen[-1] is None
    assert calls == ["v"]


def test_a_stale_status_file_does_not_hold_the_render(paged) -> None:
    (paged / "status.json").write_text(json.dumps({
        "heartbeat_at": "2026-09-01T00:00:00Z",
        "production_inflight": {"elapsed_sec": 30},
    }))
    assert C._production_gap_blocker() is None


def test_an_oversized_wal_is_not_a_gap(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    _wal(paged, 4096)
    assert C._production_gap_blocker() == "wal 4096"


def test_a_read_yields_the_wal_back_mid_statement(paged, monkeypatch) -> None:
    """The progress handler interrupts; the last page is served, not cached over."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    crossing = {"now": False}

    # One collector for both runs: a page is only ever served to the code
    # that wrote it.
    def long_read(connection):
        if crossing["now"]:
            _wal(paged, 4096)  # the WAL crosses the line while we read
            C._PAGED_RUN["wal_checked"] = 0.0
        return {"value": connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 200000) SELECT count(*) FROM n"
            if not crossing["now"] else
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 50000000) SELECT count(*) FROM n"
        ).fetchone()[0]}

    C._cached_section("slow_moving", long_read, conn)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    crossing["now"] = True
    C._SECTION_CACHE_LOG.clear()
    value = C._cached_section("slow_moving", long_read, conn)
    assert value["value"] == 200000, "the last page, not a half-read"
    assert C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "yielded"
    assert C._PAGED_RUN["yields"] == 1
    assert C._PAGED_RUN["armed"] is False, "the handler must be disarmed after the section"
    kept = json.loads((paged / "sections" / "slow_moving.json").read_text())
    assert kept["value"]["value"] == 200000


def test_a_collector_that_swallows_the_interrupt_is_never_cached(paged, monkeypatch) -> None:
    """With no page to serve instead, the run steps aside -- it never shows a half-read."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)

    def degrading(connection):
        _wal(paged, 4096)
        C._PAGED_RUN["wal_checked"] = 0.0
        try:
            connection.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
                " WHERE i < 50000000) SELECT count(*) FROM n"
            ).fetchone()
        except sqlite3.OperationalError:
            return {"available": False}
        return {"available": True}

    with pytest.raises(C.RenderYielded):
        C._cached_section("analytics", degrading, conn)
    assert not (paged / "sections" / "analytics.json").exists(), (
        "a degraded value must never become a page"
    )


def _long_count(connection):
    return connection.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
        " WHERE i < 50000000) SELECT count(*) FROM n"
    ).fetchone()[0]


def test_an_every_run_section_never_starts_reading_past_the_line(paged, monkeypatch) -> None:
    """02:12Z: ``members`` read 254 s beside a truncate that came back busy."""

    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _wal(paged, 4096)
    calls: list[str] = []
    with pytest.raises(C.RenderYielded, match="members: wal 4096"):
        C._cached_section("members", _collector(calls), None)
    assert calls == [], "not one read past the line"


def test_an_every_run_section_gives_its_read_up_at_the_line(paged, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)

    def members(connection):
        _wal(paged, 4096)  # crosses mid-read
        C._PAGED_RUN["wal_checked"] = 0.0
        return _long_count(connection)

    with pytest.raises(C.RenderYielded, match="members: interrupted"):
        C._cached_section("members", members, conn)
    assert C._PAGED_RUN["armed"] is False


def test_a_cycle_in_flight_only_delays_an_every_run_section(paged, monkeypatch) -> None:
    """Disk contention is tolerable for a section with nothing else to serve."""

    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _status(paged, production_inflight={"elapsed_sec": 30})
    calls: list[str] = []
    C._cached_section("members", _collector(calls), None)
    assert calls == ["v"]


def test_a_paged_section_with_no_page_yields_the_run_to_an_oversized_wal(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _wal(paged, 4096)
    calls: list[str] = []
    with pytest.raises(C.RenderYielded, match="analytics: wal 4096"):
        C._cached_section("analytics", _collector(calls), None)
    assert calls == []


def test_a_yielded_run_keeps_the_page_and_exits_clean(tmp_path, monkeypatch) -> None:
    db = tmp_path / "ssot.db"
    sqlite3.connect(db).close()
    out = tmp_path / "page.html"

    def yielding(_db):
        raise C.RenderYielded("members: wal 4096")

    monkeypatch.setattr(C, "build_payload", yielding)
    # main() sets these globals; registered first so teardown restores them.
    monkeypatch.setattr(C, "PAGED_RENDER", C.PAGED_RENDER)
    monkeypatch.setattr(C, "PAGE_READ_ONLY", C.PAGE_READ_ONLY)
    monkeypatch.setattr(C, "SECTION_CACHE_ENABLED", C.SECTION_CACHE_ENABLED)
    monkeypatch.setattr(sys, "argv", ["calibration_dashboard.py", "--db", str(db), "--html-out", str(out)])
    assert C.main() == 0
    assert not out.exists(), "a yielded run writes nothing; the previous page stays"


def test_a_section_fed_by_other_sections_is_keyed_on_them(paged) -> None:
    calls: list[str] = []

    def derived(conn, members):
        calls.append(members[0])
        return {"from": members[0]}

    assert C._cached_section("gaps_like", derived, None, ["a"])["from"] == "a"
    assert C._cached_section("gaps_like", derived, None, ["b"])["from"] == "b"
    assert C._cached_section("gaps_like", derived, None, ["b"])["from"] == "b"
    assert calls == ["a", "b"], "new inputs recompute; the same inputs reuse"


def test_the_every_run_sections_leave_room_for_themselves(paged, monkeypatch) -> None:
    """A paged section yields its turn to the every-run sections still to come."""

    C._PAGED_RUN["every_run_costs"] = {"live_days": 500.0}
    monkeypatch.setattr(C, "RENDER_EVERY_RUN_DEFAULT_COST_SEC", 0.0)
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", 400.0)
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    blob["build_sec"] = 10.0
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "old", "live_days' 500 s is spoken for"
    C._SECTION_CACHE_LOG["live_days"] = {"build_sec": 1.0}
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new", "once live_days has run, the room is back"


def test_only_the_timed_page_is_paged() -> None:
    """``--json`` and the server keep the one-pass build."""

    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    # 2026-10-15: the backend compute run is the paged build; the page
    # render draws read-only from what it kept.
    assert "PAGED_RENDER = bool(args.compute_pages) and SECTION_CACHE_ENABLED" in source
    assert "PAGE_READ_ONLY = bool(args.html_out) and SECTION_CACHE_ENABLED" in source
    assert C.PAGED_RENDER is False and C.PAGE_READ_ONLY is False


def test_every_run_costs_are_recorded_as_each_one_finishes(paged) -> None:
    """A run killed before its end must still leave the next one its reserve."""

    C._cached_section("members", lambda conn: {"ok": True}, None)
    saved = json.loads((paged / "sections" / C.RENDER_EVERY_RUN_COSTS).read_text())
    assert "members" in saved, "recorded when the section finished, not at the end"


def test_an_every_run_section_with_no_record_still_gets_room(paged) -> None:
    C._PAGED_RUN["every_run_costs"] = {}
    reserve = C._every_run_reserve_sec()
    assert reserve == C.RENDER_EVERY_RUN_DEFAULT_COST_SEC * len(C.RENDER_EVERY_RUN_SECTIONS)
    C._SECTION_CACHE_LOG["live_days"] = {"build_sec": 1.0}
    assert C._every_run_reserve_sec() == reserve - C.RENDER_EVERY_RUN_DEFAULT_COST_SEC


def test_a_section_with_a_page_never_waits_for_a_gap(paged, monkeypatch) -> None:
    """A cycle in flight: read through it without waiting (2026-10-02, the
    page render served 115 pages "no gap" against 62 reads while cycles ran
    back to back).  Only a WAL past the start line serves the page."""

    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    _status(paged, production_inflight={"elapsed_sec": 30})
    slept: list[float] = []
    monkeypatch.setattr(C.time, "sleep", lambda sec: slept.append(sec))
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new" and slept == []

    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    _wal(paged, 4096)
    value = C._cached_section("slow_moving", _collector(calls, "newer"), None)
    assert value["value"] == "new" and slept == []
    assert C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "no gap"


def test_a_section_with_a_page_reads_in_an_open_gap(paged) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    _status(paged)  # no cycle in flight
    value = C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert value["value"] == "new"


def test_a_new_section_that_declares_what_to_show_does_not_stop_the_page(paged, monkeypatch) -> None:
    """member_depth's first read under heavy writes (03:17Z) yielded the whole run."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)

    def depth(connection):
        _wal(paged, 4096)
        C._PAGED_RUN["wal_checked"] = 0.0
        return _long_count(connection)

    assert C._cached_section("member_depth_like", depth, conn, _unavailable={}) == {}
    entry = C._SECTION_CACHE_LOG["member_depth_like"]
    assert entry["unavailable"] is True and entry["cached"] is False
    assert not (paged / "sections" / "member_depth_like.json").exists()
    # Past the line before reading: not read at all.
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    calls: list[str] = []
    assert C._cached_section("other_new", _collector(calls), None, _unavailable=[]) == []
    assert calls == []


def test_an_every_run_section_cannot_declare_its_way_out(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    _wal(paged, 4096)
    with pytest.raises(C.RenderYielded):
        C._cached_section("members", _collector([]), None, _unavailable=[])


def test_the_run_shares_one_wait_budget(paged, monkeypatch) -> None:
    """03:30Z: 120 s per pageless section, no total, through back-to-back cycles."""

    _status(paged, production_inflight={"elapsed_sec": 30})
    monkeypatch.setattr(C, "RENDER_GAP_POLL_SEC", 50.0)
    slept: list[float] = []
    monkeypatch.setattr(C.time, "sleep", lambda sec: slept.append(sec))
    C._PAGED_RUN["gap_wait_left"] = 150.0
    C._cached_section("members", _collector([]), None)      # waits 120
    C._cached_section("gaps_like", _collector([]), None)    # 30 left
    C._cached_section("live_days", _collector([]), None)    # nothing left
    assert sum(slept) == pytest.approx(150.0)
    assert C._PAGED_RUN["gap_wait_left"] == 0.0


def test_a_page_whose_inputs_moved_is_still_a_page(paged, monkeypatch) -> None:
    """A date in the key: every morning it had no page and was read whole."""

    calls: list[str] = []
    collector = _collector(calls, "yesterday")
    C._cached_section("slow_moving", collector, None, _salt="2026-09-23")
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    value = C._cached_section("slow_moving", collector, None, _salt="2026-09-24")
    assert value["value"] == "yesterday" and calls == ["yesterday"]
    reason = C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"]
    assert reason == "budget, inputs moved"


def test_a_page_with_moved_inputs_is_never_fresh(paged) -> None:
    """Past the budget it is a stale page; inside the budget it is recomputed."""

    calls: list[str] = []
    collector = _collector(calls, "x")
    C._cached_section("slow_moving", collector, None, _salt="a")
    C._cached_section("slow_moving", collector, None, _salt="b")
    assert calls == ["x", "x"]


def test_a_paged_run_builds_the_live_book_once(paged, monkeypatch) -> None:
    builds: list[int] = []
    monkeypatch.setattr(C.live_book, "build", lambda conn: builds.append(1) or {"legs": []})
    first = C._render_live_book(None)
    second = C._render_live_book(None)
    assert builds == [1] and first is second


def test_outside_a_paged_run_the_live_book_is_the_modules_to_cache(monkeypatch) -> None:
    C._PAGED_RUN.clear()
    builds: list[int] = []
    monkeypatch.setattr(C.live_book, "build", lambda conn: builds.append(1) or {})
    C._render_live_book(None)
    C._render_live_book(None)
    assert builds == [1, 1]


def test_a_read_in_progress_runs_on_between_the_two_lines(paged, monkeypatch) -> None:
    """「提线」: no read STARTS past 256 MiB; one in progress gives way only at 384."""

    monkeypatch.setattr(C, "RENDER_MAX_WAL_BYTES", 1024)
    monkeypatch.setattr(C, "RENDER_YIELD_WAL_BYTES", 8192)
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)

    def members(connection):
        _wal(paged, 4096)  # past the start line, under the yield line
        C._PAGED_RUN["wal_checked"] = 0.0
        return connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 300000) SELECT count(*) FROM n"
        ).fetchone()[0]

    assert C._cached_section("members", members, conn) == 300000
    monkeypatch.setattr(C, "LIVE_SECTION_GAP_CAP_SEC", 0.0)
    # A section with nothing else to serve may also START under the yield
    # line (06:22Z: live_readiness at 305 MiB took the whole run down) ...
    calls: list[str] = []
    C._cached_section("chain", _collector(calls), None)
    assert calls == ["v"]
    # ... one with something to serve instead does not start past 256.
    assert C._cached_section("new_view", _collector(calls), None, _unavailable={}) == {}
    assert calls == ["v"]
    # And past the yield line nothing without a fallback starts at all.
    _wal(paged, 16384)
    with pytest.raises(C.RenderYielded, match="live_readiness: wal 16384"):
        C._cached_section("live_readiness", _collector(calls), None)


def test_the_yield_line_sits_under_productions_wall() -> None:
    from bayes_basket_target.resource_bounds import MAX_TARGET_WAL_BYTES

    assert C.RENDER_MAX_WAL_BYTES < C.RENDER_YIELD_WAL_BYTES < MAX_TARGET_WAL_BYTES


def test_the_truth_lamp_stands_when_its_table_is_unread(monkeypatch, tmp_path) -> None:
    """Its light is the retry engines'; an unread coverage table must not hide it."""

    monkeypatch.setattr(C, "TRUTH_SWEEP_STATUS", tmp_path / "missing.json")
    monkeypatch.setattr(C, "STATUS_JSON", tmp_path / "missing_status.json")
    out = C.collect_truth_materialization(
        sqlite3.connect(":memory:"), {"rows": [], "unavailable": True}
    )
    assert out["coverage"] == [] and out["coverage_unavailable"] is True
    assert out["health"] in ("amber", "red")
    source = (ROOT / "scripts" / "calibration_dashboard.py").read_text(encoding="utf-8")
    assert "(T.coverage || []).length || T.coverage_unavailable" in source


def test_live_readiness_takes_the_difficulty_it_is_handed(monkeypatch) -> None:
    """The lanes' money is the live book's; the two difficulty readings are a paged section."""

    def forbidden(conn):
        raise AssertionError("handed in; do not read every truth row again")

    monkeypatch.setattr(C, "collect_station_difficulty", forbidden)
    monkeypatch.setattr(C, "_collect_live_orders", lambda conn: {"pnl": {"by_station": []}})
    monkeypatch.setattr(C, "_render_live_book", lambda conn: {"settlements": []})
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth"
        " (station_id TEXT, local_date TEXT, official_tmax_c REAL)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_market_timezone_binding"
        " (station_id TEXT)"
    )
    conn.execute(
        "INSERT INTO calibration_station_market_timezone_binding VALUES ('KLGA')"
    )
    out = C.collect_live_readiness(
        conn, {"climate_jump": {"KLGA": 1.234}, "shape_gap": {}},
    )
    row = next(r for r in out["stations"] if r["station"] == "KLGA")
    assert row["climate_jump_c"] == 1.23 and row["shape_gap_buckets"] is None


def test_live_selection_reads_the_runs_one_book(paged, monkeypatch) -> None:
    builds: list[int] = []
    monkeypatch.setattr(C.live_book, "build", lambda conn: builds.append(1) or {"legs": []})
    seen: list[object] = []
    monkeypatch.setattr(
        C.live_selection, "build",
        lambda conn, book=None, closed=None: seen.append(book) or {},
    )
    C._render_live_book(None)
    C._build_live_selection(None)
    assert builds == [1] and seen and seen[0] is C._PAGED_RUN["live_book"]


def test_a_closed_station_days_market_slug_is_read_once(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry (station_id TEXT,"
        " event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES"
        " ('KLGA','2026-09-01','nyc-sep-1','2026-09-01T00:00:00Z')"
    )
    rows = [{"station_id": "KLGA", "event_date": "2026-09-01"}]
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db") == {"KLGA|2026-09-01": "nyc-sep-1"}
    conn.execute("DELETE FROM basket_target_market_registry")
    # Kept: the registry is not asked again for a settled day.
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db") == {"KLGA|2026-09-01": "nyc-sep-1"}


def test_an_open_station_days_slug_is_always_read(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry (station_id TEXT,"
        " event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    today = C._now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?,?)",
        ("KLGA", today, "old", "2026-09-01T00:00:00Z"),
    )
    rows = [{"station_id": "KLGA", "event_date": today}]
    C._live_market_slugs(conn, rows, archive=paged / "none.db")
    conn.execute("UPDATE basket_target_market_registry SET slug='relisted'")
    C._PAGED_RUN.clear()
    C._paged_run_start(paged / "ssot.db")  # the next run
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db")[f"KLGA|{today}"] == "relisted"


def test_past_the_budget_a_pageless_section_with_a_stand_in_is_not_read(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", -1.0)
    calls: list[str] = []
    assert C._cached_section("slow_moving", _collector(calls), None, _unavailable={}) == {}
    assert calls == [] and C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "budget"
    # Without a stand-in it still has to be read.
    C._cached_section("other_new", _collector(calls), None)
    assert calls == ["v"]


def test_a_live_baskets_plan_is_found_in_its_window_or_by_the_whole_day() -> None:
    """Window first (the plans compiled just before the decision), day second."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions (decision_id TEXT, station_id TEXT,"
        " event_date TEXT, details_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan (station_id TEXT,"
        " event_date TEXT, plan_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_p0_preselection_created_idx"
        " ON basket_p0_preselection_plan(created_at)"
    )

    def plan(sha, center, at):
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?)",
            ("KLGA", "2026-09-10", json.dumps({"compiled_plan": {
                "production_plan_sha256": sha, "plan": {"center_bucket": center},
            }}), at),
        )

    def decision(did, sha, at):
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?)",
            (did, "KLGA", "2026-09-10",
             json.dumps({"production_plan_sha256": sha}), at),
        )

    plan("near", "70", "2026-09-09T11:50:00Z")
    decision("d1", "near", "2026-09-09T12:00:00Z")
    plan("far", "72", "2026-09-09T02:00:00Z")      # hours before its decision
    decision("d2", "far", "2026-09-09T12:00:00Z")
    state = C._live_basket_plan_state(conn, [{"decision_id": "d1"}, {"decision_id": "d2"}])
    assert state["d1"]["center"] == "70"
    assert state["d2"]["center"] == "72", "outside every window: the whole day is read"


def test_a_pageless_section_that_will_not_fit_shows_its_stand_in(paged, monkeypatch) -> None:
    """08:36Z: 432 s left, a 531 s section with no page read into the kill."""

    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["fingerprint"] = blob["code_fingerprint"] = "older code"
    blob["build_sec"] = 250.0   # under the research lane's line
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", 200.0)
    monkeypatch.setattr(C, "RENDER_EVERY_RUN_DEFAULT_COST_SEC", 0.0)
    assert C._cached_section("slow_moving", _collector(calls, "new"), None, _unavailable={}) == {}
    assert calls == ["old"]
    monkeypatch.setattr(C, "RENDER_SOFT_BUDGET_SEC", 1200.0)
    assert C._cached_section("slow_moving", _collector(calls, "new"), None, _unavailable={})["value"] == "new"


def test_a_closed_days_plan_reading_is_kept_found_or_not(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions (decision_id TEXT, station_id TEXT,"
        " event_date TEXT, details_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan (station_id TEXT,"
        " event_date TEXT, plan_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_p0_preselection_created_idx"
        " ON basket_p0_preselection_plan(created_at)"
    )
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES (?,?,?,?)",
        ("KLGA", "2026-09-01", json.dumps({"compiled_plan": {
            "production_plan_sha256": "p", "plan": {"center_bucket": "70"},
        }}), "2026-08-31T11:50:00Z"),
    )
    for did, sha in (("found", "p"), ("missing", "q")):
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?)",
            (did, "KLGA", "2026-09-01",
             json.dumps({"production_plan_sha256": sha}), "2026-08-31T12:00:00Z"),
        )
    rows = [{"decision_id": "found"}, {"decision_id": "missing"}]
    first = C._live_basket_plan_state(conn, rows)
    assert set(first) == {"found"}
    conn.execute("DELETE FROM basket_p0_preselection_plan")
    conn.execute("DELETE FROM auto_trade_decisions")
    assert C._live_basket_plan_state(conn, rows) == first, "a settled day is not read again"


def test_a_closed_station_day_with_no_listed_market_is_not_asked_again(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry (station_id TEXT,"
        " event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    rows = [{"station_id": "KLGA", "event_date": "2026-09-01"}]
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db") == {}
    # Listed afterwards: a lookup would now find it, so an empty answer
    # means the kept miss was used.
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES"
        " ('KLGA','2026-09-01','late','2026-09-02T00:00:00Z')"
    )
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db") == {}
    kept = json.loads((paged / "sections" / C.RENDER_CLOSED_SLUGS).read_text())
    assert kept == {"KLGA|2026-09-01": ""}


def test_an_open_station_days_slug_is_read_once_per_run(paged) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry (station_id TEXT,"
        " event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    today = C._now().strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?,?)",
        ("KLGA", today, "first", "2026-09-01T00:00:00Z"),
    )
    rows = [{"station_id": "KLGA", "event_date": today}]
    C._live_market_slugs(conn, rows, archive=paged / "none.db")
    conn.execute("UPDATE basket_target_market_registry SET slug='relisted'")
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db")[f"KLGA|{today}"] == "first"
    C._PAGED_RUN.clear()
    C._paged_run_start(paged / "ssot.db")
    assert C._live_market_slugs(conn, rows, archive=paged / "none.db")[f"KLGA|{today}"] == "relisted", (
        "the next run reads an open day again"
    )


def _truth_landing_fixture() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_market_timezone_binding"
        " (station_id TEXT, settlement_timezone TEXT)"
    )
    conn.execute(
        "INSERT INTO calibration_station_market_timezone_binding VALUES ('KLGA','America/New_York')"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope (station_id TEXT, event_date TEXT,"
        " stage TEXT, decision_at_utc TEXT, probabilities_json TEXT,"
        " posterior_version TEXT)"
    )
    conn.execute(
        "CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
        " FROM basket_probability_envelope"
    )
    conn.execute(
        "CREATE TABLE basket_station_day_settlement_label (station_id TEXT,"
        " event_date TEXT, truth_revision_no INTEGER, settled_bucket TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot (station_id TEXT, event_date TEXT,"
        " observed_at TEXT, quote_expires_at TEXT, buckets_json TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_official_ws_snapshot_day_expiry_idx"
        " ON basket_official_ws_snapshot(event_date, quote_expires_at)"
    )
    for i, day in enumerate(("2026-08-20", "2026-08-21", "2026-08-22")):
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES ('KLGA',?,'P0',?,?,'v')",
            (day, f"{day}T01:00:00Z", json.dumps({"70": 0.6, "71": 0.4})),
        )
        if i < 2:   # the third day is not settled yet
            conn.execute(
                "INSERT INTO basket_station_day_settlement_label VALUES ('KLGA',?,0,'70')",
                (day,),
            )
        for hour, bids in ((2, (0.5, 0.4)), (3, (0.55, 0.35)), (23, (0.9 - 0.1 * i, 0.05))):
            conn.execute(
                "INSERT INTO basket_official_ws_snapshot VALUES ('KLGA',?,?,?,?)",
                (day, f"{day}T{hour:02d}:00:00Z", "x", json.dumps([
                    {"bucket": "70", "ask_levels": [{"price": 0.62}], "bid_price": bids[0]},
                    {"bucket": "71", "ask_levels": [{"price": 0.41}], "bid_price": bids[1]},
                ])),
            )
    return conn


def test_the_truth_landing_caught_up_in_chunks_is_the_one_read(paged, monkeypatch) -> None:
    """A far-behind cache reads settled days a chunk a run; the answer is the same."""

    monkeypatch.setattr(C, "TRUTH_LANDING_SINCE", "2026-08-20")
    monkeypatch.setattr(C, "TRUTH_LANDING_CATCHUP_SNAPSHOTS", 1)
    # Pinned to the fixture's days: on the wall clock the catch-up walked
    # every day since and ran past ten chunks (red from 2026-10-15).
    monkeypatch.setattr(C, "_now", lambda: C.datetime(2026, 8, 26, 12, tzinfo=C.timezone.utc))
    conn = _truth_landing_fixture()
    saved = dict(C._PAGED_RUN)
    C._PAGED_RUN.clear()
    whole = C.collect_truth_landing(conn)          # not paged: one read
    C._PAGED_RUN.update(saved)
    chunks = 0
    while True:
        try:
            paged_answer = C.collect_truth_landing(conn)
            break
        except C.SectionNotReady:
            chunks += 1
            assert chunks < 10
    assert chunks >= 2, "the three settled days came a chunk at a time"
    assert paged_answer == whole
    assert whole["settled_station_days"] == 2   # the third day has no settled bucket yet


def test_a_section_not_ready_serves_its_stand_in_and_caches_nothing(paged) -> None:
    def building(conn):
        raise C.SectionNotReady("halfway")

    assert C._cached_section("slow_moving", building, None, _unavailable={}) == {}
    assert "catching up" in C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"]
    assert not (paged / "sections" / "slow_moving.json").exists()


def test_h5_readiness_caught_up_in_chunks_is_the_one_read(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "H5_FROM_UTC", "2026-08-27T06:48:00Z")
    monkeypatch.setattr(C, "H5_CATCHUP_DAYS", 2)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth"
        " (station_id TEXT, local_date TEXT, qc_status TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_system_artifact"
        " (ensemble_system_id TEXT, station_id TEXT, content_sha256 TEXT,"
        " valid_from_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_system_artifact VALUES"
        " ('ECMWF','KLGA',?,?)",
        [("old", "2026-08-20T00:00:00Z"), ("new", "2026-08-28T00:00:00Z")],
    )
    conn.execute(
        "CREATE VIEW calibration_native_ensemble_system AS SELECT DISTINCT"
        " ensemble_system_id FROM calibration_native_ensemble_system_artifact"
    )
    conn.execute(
        "CREATE VIEW calibration_station_registry AS SELECT DISTINCT station_id"
        " FROM calibration_native_ensemble_system_artifact"
    )
    conn.execute(
        "CREATE TABLE basket_probability_envelope"
        " (stage TEXT, state TEXT, event_date TEXT, lineage_json TEXT)"
    )
    for i in range(6):
        day = f"2026-08-{28 + i:02d}"
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES ('KLGA',?,'ACCEPTED')", (day,),
        )
        for sha in (["old"], ["new"], ["new"], ["unknown"])[: 2 + i % 3]:
            conn.execute(
                "INSERT INTO basket_probability_envelope VALUES ('P0','COMMITTED',?,?)",
                (day, json.dumps({"system_artifact_shas_json": json.dumps({"ECMWF": sha[0]})})),
            )
    saved = dict(C._PAGED_RUN)
    C._PAGED_RUN.clear()
    whole = C.collect_h5_readiness(conn)
    C._PAGED_RUN.update(saved)
    chunks = 0
    while True:
        try:
            paged_answer = C.collect_h5_readiness(conn)
            break
        except C.SectionNotReady:
            chunks += 1
            assert chunks < 10
    assert chunks >= 2 and paged_answer == whole and len(whole["days"]) == 6


def _heavy_page(paged, name, collector, cost, computed_at="2026-09-01T00:00:00Z"):
    calls: list[str] = []
    C._cached_section(name, collector, None)
    blob = json.loads((paged / "sections" / f"{name}.json").read_text())
    blob["computed_at_utc"] = computed_at
    blob["build_sec"] = cost
    (paged / "sections" / f"{name}.json").write_text(json.dumps(blob))
    C._SECTION_CACHE_LOG.clear()
    return calls


def test_a_heavy_expired_section_is_left_to_the_research_render(paged) -> None:
    calls: list[str] = []
    collector = _collector(calls, "old")
    _heavy_page(paged, "slow_moving", collector, 1249.0)
    value = C._cached_section("slow_moving", collector, None)
    assert value["value"] == "old" and calls == ["old"], "the page render does not read it"
    assert C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "research lane"


_PROBE_CALLS: list[str] = []


def _heavy_probe(conn):
    """A module-level collector, as the research render needs to call one."""

    _PROBE_CALLS.append("read")
    return {"value": "fresh", "n": len(_PROBE_CALLS)}


def test_the_research_render_computes_the_most_overdue_heavy_section(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"slow_moving": 3600, "other": 3600})
    _heavy_page(paged, "slow_moving", _heavy_probe, 900.0, "2026-09-01T00:00:00Z")
    _heavy_page(paged, "other", _heavy_probe, 900.0, C._now().isoformat().replace("+00:00", "Z"))
    C._save_section_specs()
    specs = json.loads((paged / "sections" / C.RENDER_SECTION_SPECS).read_text())
    assert specs["slow_moving"]["collector"] == "_heavy_probe"
    C._PAGED_RUN.clear()
    before = len(_PROBE_CALLS)
    out = C.refresh_research_section(paged / "ssot.db")
    assert "slow_moving computed" in out, out
    assert len(_PROBE_CALLS) == before + 1, "only the overdue one; 'other' is fresh"
    page = json.loads((paged / "sections" / "slow_moving.json").read_text())
    assert page["value"]["n"] == len(_PROBE_CALLS)
    C._paged_run_start(paged / "ssot.db")


def test_the_research_render_does_nothing_when_nothing_is_due(paged) -> None:
    C._PAGED_RUN.clear()
    assert C.refresh_research_section(paged / "ssot.db") == "research render: nothing due"
    C._paged_run_start(paged / "ssot.db")


def test_a_killed_page_render_still_leaves_its_calls(paged, monkeypatch) -> None:
    """Specs are on disk as each section is met, not only at a run's end."""

    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"slow_moving": 3600})
    C._cached_section("slow_moving", _heavy_probe, None)
    specs = json.loads((paged / "sections" / C.RENDER_SECTION_SPECS).read_text())
    assert specs["slow_moving"]["collector"] == "_heavy_probe"


# The hard deadline (2026-10-01: three runs in a row killed at 1,500 s on
# every-run sections slowed tenfold by swap, nothing written, the whole board
# frozen at the 10:54Z page for three hours).


def test_an_every_run_section_keeps_its_last_value_but_is_still_never_paged(paged) -> None:
    calls: list[str] = []
    C._cached_section("regional", _collector(calls), None)
    assert not (paged / "sections" / "regional.json").exists()
    assert list((paged / "sections" / C.EVERY_RUN_LAST_DIR_NAME).glob("regional.*.json"))
    C._SECTION_CACHE_LOG.clear()
    C._cached_section("regional", _collector(calls), None)
    assert calls == ["v", "v"], "before the deadline it is read every run"


def test_past_the_deadline_an_every_run_section_serves_its_last_value_marked_overdue(paged, monkeypatch) -> None:
    calls: list[str] = []
    first = C._cached_section("regional", _collector(calls, "old"), None)
    C._SECTION_CACHE_LOG.clear()
    monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)
    value = C._cached_section("regional", _collector(calls, "new"), None)
    assert calls == ["old"], "past the deadline nothing more is read"
    assert value == first
    entry = C._SECTION_CACHE_LOG["regional"]
    assert entry["stale"] is True and entry["stale_reason"] == "deadline"
    alerts = C._render_deadline_alerts()
    assert len(alerts) == 1 and alerts[0]["health"] == "amber" and "regional" in alerts[0]["note"]


def test_an_every_run_read_in_hand_is_interrupted_at_the_deadline(paged, monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)
    slow = {"on": False}

    def members(connection):
        if not slow["on"]:
            return {"value": "old"}
        monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)  # passes mid-read
        return {"value": _long_count(connection)}

    C._cached_section("members", members, conn)
    C._SECTION_CACHE_LOG.clear()
    slow["on"] = True
    value = C._cached_section("members", members, conn)
    assert value["value"] == "old"
    assert C._SECTION_CACHE_LOG["members"]["stale_reason"] == "deadline"
    assert C._PAGED_RUN["armed"] is False


def test_past_the_deadline_with_no_last_value_an_every_run_section_is_still_read(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)
    calls: list[str] = []
    # No interrupt without a progress handler on this stand-in connection.
    C._cached_section("members", _collector(calls), None)
    assert calls == ["v"]


def test_a_last_value_is_never_served_across_a_code_change(paged, monkeypatch) -> None:
    C._cached_section("members", _collector([], "old"), None)
    C._SECTION_CACHE_LOG.clear()
    monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)
    calls: list[str] = []

    def members(conn):  # another body, another fingerprint
        calls.append("edited")
        return {"value": "edited"}

    assert C._cached_section("members", members, None) == {"value": "edited"}
    assert calls == ["edited"]


def test_past_the_deadline_a_page_backed_section_serves_its_page(paged, monkeypatch) -> None:
    calls: list[str] = []
    C._cached_section("slow_moving", _collector(calls, "old"), None)
    blob = json.loads((paged / "sections" / "slow_moving.json").read_text())
    blob["computed_at_utc"] = "2026-09-01T00:00:00Z"
    (paged / "sections" / "slow_moving.json").write_text(json.dumps(blob))
    C._SECTION_CACHE_LOG.clear()
    monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)
    C._cached_section("slow_moving", _collector(calls, "new"), None)
    assert calls == ["old"]
    assert C._SECTION_CACHE_LOG["slow_moving"]["stale_reason"] == "deadline"


def test_the_research_render_never_latches_the_deadline(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)
    C._PAGED_RUN["force"] = "slow_moving"
    assert C._paged_past_deadline() is False


def test_the_hard_deadline_sits_between_the_soft_budget_and_the_kill() -> None:
    sweep = (ROOT / "scripts" / "dashboard_render_sweep.sh").read_text(encoding="utf-8")
    kill = int(sweep.split('PM_DASHBOARD_RENDER_TIMEOUT_SEC:-', 1)[1].split("}", 1)[0])
    assert C.RENDER_SOFT_BUDGET_SEC < C.RENDER_HARD_DEADLINE_SEC <= kill - 120


def test_with_no_last_value_a_read_in_hand_runs_on_past_the_deadline(paged, monkeypatch) -> None:
    """2026-10-01 14:10Z, the first run under the deadline: live_days had no
    last value yet, was cut at 1,380 s with nothing in its place, and the
    run wrote no page.  With nothing to serve instead it reads to the end
    and keeps a last value for the next run."""

    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(C._render_yield_check, 1_000)

    def live_days(connection):
        monkeypatch.setattr(C, "RENDER_HARD_DEADLINE_SEC", -1.0)  # passes mid-read
        return {"n": connection.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n"
            " WHERE i < 200000) SELECT count(*) FROM n").fetchone()[0]}

    assert C._cached_section("live_days", live_days, conn) == {"n": 200000}
    assert list((paged / "sections" / C.EVERY_RUN_LAST_DIR_NAME).glob("live_days.*.json"))


def test_a_new_last_value_replaces_the_one_from_older_code(paged) -> None:
    C._cached_section("members", _collector([], "old"), None)

    def members(conn):
        return {"value": "edited"}

    C._cached_section("members", members, None)
    kept = list((paged / "sections" / C.EVERY_RUN_LAST_DIR_NAME).glob("members.*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text())["value"] == {"value": "edited"}


_FILE_PROBE_CALLS: list[str] = []


def _file_probe():
    """A collector that reads files and takes no connection (fusion_candidates)."""

    _FILE_PROBE_CALLS.append("read")
    return {"value": "files"}


_BROKEN = {"on": False}


def _broken_probe(conn):
    if _BROKEN["on"]:
        raise TypeError("a bug in one section")
    return {"value": "ok"}


def _old_page(paged, name, collector, *args):
    """``_heavy_page`` for a call with its own arguments (none, for a file reader)."""

    C._cached_section(name, collector, *args)
    blob = json.loads((paged / "sections" / f"{name}.json").read_text())
    blob["computed_at_utc"] = "2026-08-01T00:00:00Z"
    blob["build_sec"] = 900.0
    (paged / "sections" / f"{name}.json").write_text(json.dumps(blob))
    C._SECTION_CACHE_LOG.clear()


def test_the_research_render_passes_the_connection_only_where_the_call_took_one(paged, monkeypatch) -> None:
    """2026-10-16 12:02Z: fusion_candidates takes no connection, the research
    render passed one anyway, and its TypeError ended every run from then --
    truth_landing, catching up a week a run, stayed off the board."""

    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"files": 3600, "slow_moving": 3600})
    monkeypatch.setattr(C, "_page_render_running", lambda: False)   # the live machine may be rendering
    _old_page(paged, "files", _file_probe)
    C._save_section_specs()
    specs = json.loads((paged / "sections" / C.RENDER_SECTION_SPECS).read_text())
    assert specs["files"]["conn"] is False and specs["files"]["args"] == []
    C._PAGED_RUN.clear()
    out = C.refresh_research_section(paged / "ssot.db")
    assert "files computed" in out and _FILE_PROBE_CALLS, out
    C._paged_run_start(paged / "ssot.db")
    # A spec written before ``conn`` was recorded is read off the signature.
    assert C._spec_takes_conn({"args": []}, _file_probe) is False
    assert C._spec_takes_conn({"args": []}, _heavy_probe) is True


def test_one_broken_section_does_not_end_the_research_run(paged, monkeypatch) -> None:
    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"broken": 3600, "slow_moving": 3600})
    monkeypatch.setattr(C, "_page_render_running", lambda: False)   # the live machine may be rendering
    _BROKEN["on"] = False
    _old_page(paged, "broken", _broken_probe, None)
    _heavy_page(paged, "slow_moving", _heavy_probe, 900.0, "2026-09-01T00:00:00Z")
    C._save_section_specs()
    _BROKEN["on"] = True
    C._PAGED_RUN.clear()
    out = C.refresh_research_section(paged / "ssot.db")
    assert "broken failed (TypeError: a bug in one section)" in out, out
    assert "slow_moving computed" in out, out
    _BROKEN["on"] = False
    C._paged_run_start(paged / "ssot.db")


def test_a_section_larger_than_the_run_goes_alone_and_only_when_ripe(paged, monkeypatch) -> None:
    """2026-10-17: scorecard (2,133 s, budget 1,800 s, no page) went first every
    research run and starved 37 cheaper sections for days."""

    monkeypatch.setattr(C, "SECTION_CACHE_TTL_SEC", {"slow_moving": 3600, "other": 3600})
    monkeypatch.setattr(C, "_page_render_running", lambda: False)
    _heavy_page(paged, "slow_moving", _heavy_probe, 900.0, "2026-09-01T00:00:00Z")
    _heavy_page(paged, "other", _heavy_probe, 5000.0, "2026-08-01T00:00:00Z")   # more overdue, oversize
    C._save_section_specs()
    C._PAGED_RUN.clear()
    first = C.refresh_research_section(paged / "ssot.db")
    assert "other computed" in first and "slow_moving" not in first, "a ripe oversize section runs alone"
    C._paged_run_start(paged / "ssot.db")
    # Make both due again: the oversize one is not ripe for six hours, so the
    # section that fits takes the run.
    _heavy_page(paged, "slow_moving", _heavy_probe, 900.0, "2026-09-01T00:00:00Z")
    _heavy_page(paged, "other", _heavy_probe, 5000.0, "2026-08-01T00:00:00Z")
    C._PAGED_RUN.clear()
    second = C.refresh_research_section(paged / "ssot.db")
    assert "slow_moving computed" in second and "other" not in second, second
    C._paged_run_start(paged / "ssot.db")
